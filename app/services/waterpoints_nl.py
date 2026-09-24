"""Nederlandse drinkwaterpunten van drinkwaterpunten.nl, met lokale 24-uurs cache."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import requests

from app.config import get_settings
from app.models.schemas import WaterPoint
from app.services.geo import bounding_box
from app.services.gpx_service import parse_gpx, parse_waypoints

logger = logging.getLogger(__name__)

SOURCE_NAME = "drinkwaterpunten.nl"
_LOCK = threading.Lock()


def _cache_file() -> Path:
    return get_settings().cache_dir / "publieke_drinkwaterpunten_nl.gpx"


def cache_age_seconds() -> float | None:
    path = _cache_file()
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _download() -> str:
    settings = get_settings()
    logger.info("Download Nederlandse drinkwaterpunten van %s", settings.nl_gpx_url)
    response = requests.get(
        settings.nl_gpx_url,
        timeout=60,
        headers={"User-Agent": settings.user_agent},
    )
    response.raise_for_status()
    text = response.text
    if "<gpx" not in text.lower():
        raise ValueError("Antwoord van drinkwaterpunten.nl is geen GPX")
    return text


def get_cached_gpx(force_refresh: bool = False) -> str:
    """Geef de GPX-inhoud terug; ververst maximaal eens per TTL (standaard 24 uur)."""
    settings = get_settings()
    settings.ensure_dirs()
    path = _cache_file()

    with _LOCK:
        age = cache_age_seconds()
        if not force_refresh and age is not None and age < settings.nl_cache_ttl_seconds:
            logger.debug("Cache gebruikt (leeftijd %.0f s)", age)
            return path.read_text(encoding="utf-8")

        try:
            text = _download()
        except Exception as exc:
            if path.exists():
                logger.warning(
                    "Verversen mislukt (%s); verouderde cache wordt gebruikt", exc
                )
                return path.read_text(encoding="utf-8")
            raise RuntimeError(
                f"Kan drinkwaterpunten.nl niet bereiken en er is geen cache: {exc}"
            ) from exc

        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        logger.info("Cache vernieuwd: %s (%d bytes)", path, len(text))
        return text


def load_water_points(force_refresh: bool = False) -> list[WaterPoint]:
    """Alle Nederlandse drinkwaterpunten."""
    gpx = parse_gpx(get_cached_gpx(force_refresh=force_refresh))
    points = parse_waypoints(gpx, SOURCE_NAME)
    logger.info("%d Nederlandse drinkwaterpunten geladen", len(points))
    return points


def load_water_points_near(
    route_coords: list[tuple[float, float]], margin_m: float
) -> list[WaterPoint]:
    """Nederlandse drinkwaterpunten binnen de bounding box van de route."""
    min_lat, min_lon, max_lat, max_lon = bounding_box(route_coords, margin_m)
    return [
        wp
        for wp in load_water_points()
        if min_lat <= wp.lat <= max_lat and min_lon <= wp.lon <= max_lon
    ]
