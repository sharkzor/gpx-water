"""Drinkwaterpunten uit OpenStreetMap via de Overpass API.

De route wordt opgeknipt in kleine bounding boxes. Dat is voor Overpass veel
goedkoper dan één grote `around`-query en voorkomt timeouts bij lange routes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import requests

from app.config import get_settings
from app.models.schemas import WaterPoint
from app.services.geo import bounding_box, haversine_m

logger = logging.getLogger(__name__)

SOURCE_NAME = "openstreetmap"
_POINTS_PER_CHUNK = 40

TAG_FILTERS: tuple[str, ...] = (
    '["amenity"="drinking_water"]',
    '["man_made"="water_tap"]',
    '["drinking_water"="yes"]',
)


def _thin(
    coords: Sequence[tuple[float, float]], min_step_m: float
) -> list[tuple[float, float]]:
    """Verdun de route zodat de query compact blijft."""
    if not coords:
        return []
    kept = [coords[0]]
    for lat, lon in coords[1:]:
        if haversine_m(kept[-1][0], kept[-1][1], lat, lon) >= min_step_m:
            kept.append((lat, lon))
    if kept[-1] != coords[-1]:
        kept.append(coords[-1])
    return kept


def _chunk_boxes(
    coords: Sequence[tuple[float, float]], radius_m: int
) -> list[tuple[float, float, float, float]]:
    """Deel de route op in aaneensluitende bounding boxes met marge."""
    thinned = _thin(coords, max(radius_m * 0.5, 200.0))
    boxes: list[tuple[float, float, float, float]] = []
    for start in range(0, len(thinned), _POINTS_PER_CHUNK):
        chunk = thinned[start : start + _POINTS_PER_CHUNK + 1]
        if chunk:
            boxes.append(bounding_box(chunk, margin_m=radius_m + 50))
    return boxes


def _build_query(box: tuple[float, float, float, float]) -> str:
    settings = get_settings()
    bbox = ",".join(f"{v:.5f}" for v in box)
    clauses = "\n  ".join(f"nwr{flt}({bbox});" for flt in TAG_FILTERS)
    return (
        f"[out:json][timeout:{settings.overpass_timeout}];\n"
        f"(\n  {clauses}\n);\nout center tags;"
    )


def _endpoints() -> list[str]:
    return [u.strip() for u in get_settings().overpass_url.split(",") if u.strip()]


def _run_query(query: str) -> dict:
    """Voer de query uit; probeer elk geconfigureerd endpoint."""
    settings = get_settings()
    last_error: Exception | None = None
    for url in _endpoints():
        try:
            response = requests.post(
                url,
                data={"data": query},
                timeout=(10, settings.overpass_timeout),
                headers={"User-Agent": settings.user_agent},
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("Overpass endpoint %s faalde: %s", url, exc)
            last_error = exc
    raise RuntimeError(f"Overpass API niet bereikbaar: {last_error}")


def _element_to_point(element: dict) -> WaterPoint | None:
    tags: dict[str, str] = element.get("tags", {}) or {}
    if tags.get("drinking_water") == "no" or tags.get("access") in {"private", "no"}:
        return None
    lat = element.get("lat")
    lon = element.get("lon")
    if lat is None or lon is None:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None

    return WaterPoint(
        lat=float(lat),
        lon=float(lon),
        name=tags.get("name") or tags.get("description"),
        operator=tags.get("operator"),
        opening_hours=tags.get("opening_hours"),
        website=tags.get("website") or tags.get("contact:website"),
        source=SOURCE_NAME,
        description=tags.get("description"),
        extra={
            k: v
            for k, v in tags.items()
            if k in {"amenity", "man_made", "fee", "seasonal", "bottle"}
        },
    )


def load_water_points_near(
    route_coords: Sequence[tuple[float, float]], radius_m: int
) -> list[WaterPoint]:
    """Zoek drinkwaterpunten rond de route via Overpass."""
    boxes = _chunk_boxes(route_coords, radius_m)
    logger.info("Overpass: %d deelquery's voor de route", len(boxes))

    found: dict[tuple[str, int], WaterPoint] = {}
    errors: list[str] = []
    for box in boxes:
        try:
            payload = _run_query(_build_query(box))
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        for element in payload.get("elements", []):
            point = _element_to_point(element)
            if point is None:
                continue
            found[(element.get("type", "node"), int(element.get("id", 0)))] = point

    if errors and not found:
        raise RuntimeError(errors[0])
    if errors:
        logger.warning("%d van %d deelquery's mislukt", len(errors), len(boxes))

    logger.info("%d OSM-drinkwaterpunten gevonden", len(found))
    return list(found.values())
