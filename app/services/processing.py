"""Orkestratie: van geuploade GPX naar resultaat met waterpunten."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.models.schemas import (
    LegalitySegmentOut,
    ProcessResult,
    RoadWorkOut,
    WaterPointOut,
)
from app.services import (
    gpx_service,
    legality,
    osm_index,
    osm_service,
    roadworks_nl,
    route_service,
    waterpoints_nl,
)
from app.services.geo import bounding_box, in_netherlands, nl_share

logger = logging.getLogger(__name__)

SOURCE_AUTO = "auto"
SOURCE_NL = "nl"
SOURCE_OSM = "osm"


def _pick_source(share: float, requested: str) -> str:
    if requested == SOURCE_NL:
        return waterpoints_nl.SOURCE_NAME
    if requested == SOURCE_OSM:
        return osm_service.SOURCE_NAME
    threshold = get_settings().nl_share_threshold
    return (
        waterpoints_nl.SOURCE_NAME if share >= threshold else osm_service.SOURCE_NAME
    )


def cleanup_jobs() -> None:
    """Verwijder verlopen tijdelijke bestanden."""
    settings = get_settings()
    settings.ensure_dirs()
    cutoff = time.time() - settings.job_ttl_seconds
    for path in settings.tmp_dir.glob("*.gpx"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:  # pragma: no cover - best effort
            logger.debug("Kon %s niet opruimen", path)


def output_path(job_id: str) -> Path:
    return get_settings().tmp_dir / f"{job_id}.gpx"


def store_file(raw: bytes) -> str:
    """Bewaar een GPX (bijvoorbeeld het origineel van Strava) en geef het job-id."""
    settings = get_settings()
    settings.ensure_dirs()
    job_id = uuid.uuid4().hex
    output_path(job_id).write_bytes(raw)
    return job_id


def safe_filename(name: str, suffix: str = "") -> str:
    """Maak een veilige bestandsnaam van een routenaam."""
    stem = Path(name or "route").stem or "route"
    safe = "".join(c if (c.isalnum() or c in "-_ ") else "-" for c in stem).strip()
    safe = "-".join(filter(None, safe.replace(" ", "-").split("-")))[:80]
    return f"{safe or 'route'}{suffix}.gpx"


def _collect_road_works(
    index: route_service.RouteIndex,
    coords: list[tuple[float, float]],
    day: date,
) -> tuple[list, str | None]:
    """Zoek wegwerkzaamheden langs de route; geeft (treffers, foutmelding).

    De NDW-feed dekt alleen Nederland, dus buitenlandse routes leveren niets op.
    Een storing bij NDW mag de rest van de verwerking nooit blokkeren.
    """
    settings = get_settings()
    if not any(in_netherlands(lat, lon) for lat, lon in coords):
        return [], "Wegwerkzaamheden zijn alleen beschikbaar voor Nederlandse routes."
    try:
        candidates = roadworks_nl.load_road_works_near(
            coords, settings.roadworks_radius_m + 1000, day
        )
    except Exception as exc:
        logger.warning("Wegwerkzaamheden niet beschikbaar: %s", exc)
        return [], f"Wegwerkzaamheden konden niet worden opgehaald: {exc}"
    matched = route_service.attach_road_works(
        index, candidates, settings.roadworks_radius_m
    )
    return matched, None


def _collect_legality(
    coords: list[tuple[float, float]],
) -> tuple[list[legality.Segment], str | None]:
    """Controleer de route op verboden paden; geeft (segmenten, foutmelding).

    Net als bij de wegwerkzaamheden mag dit de waterpunten nooit blokkeren.
    """
    if not any(in_netherlands(lat, lon) for lat, lon in coords):
        return [], "De controle op verboden paden is alleen beschikbaar voor Nederland."
    if not osm_index.status().available:
        job = osm_index.current_job()
        if job is not None and job.state == "running":
            return [], (
                "De wegenkaart wordt nog opgebouwd "
                f"({job.message.lower()}); probeer het over een paar minuten opnieuw."
            )
        return [], "De wegenkaart is nog niet beschikbaar; die wordt automatisch opgebouwd."
    try:
        report = legality.check_route(coords)
    except Exception as exc:
        logger.warning("Controle op verboden paden mislukt: %s", exc)
        return [], f"De controle op verboden paden is mislukt: {exc}"
    logger.info(
        "Verboden paden: %d verboden, %d let op",
        report.forbidden_count,
        report.warning_count,
    )
    return report.segments, None


def process_gpx(
    raw: bytes,
    filename: str,
    radius_m: int,
    requested_source: str = SOURCE_AUTO,
    check_roadworks: bool = False,
    ride_date: date | None = None,
    check_legality: bool = False,
) -> ProcessResult:
    """Verwerk een GPX-upload en schrijf de nieuwe GPX naar de tijdelijke map."""
    settings = get_settings()
    settings.ensure_dirs()

    gpx = gpx_service.parse_gpx(raw)
    route_points = gpx_service.extract_route_points(gpx)
    coords = [(p.lat, p.lon) for p in route_points]
    share = nl_share(coords)
    source = _pick_source(share, requested_source)
    logger.info(
        "Route '%s': %d punten, %.0f%% in NL, bron=%s, radius=%d m",
        filename,
        len(coords),
        share * 100,
        source,
        radius_m,
    )

    if source == waterpoints_nl.SOURCE_NAME:
        candidates = waterpoints_nl.load_water_points_near(coords, radius_m + 1000)
    else:
        candidates = osm_service.load_water_points_near(coords, radius_m)

    index = route_service.RouteIndex(route_points)
    matched = route_service.attach_to_route(index, candidates, radius_m)
    matched = route_service.deduplicate(matched, index.projection)

    road_works: list = []
    roadworks_error: str | None = None
    day = ride_date or date.today()
    if check_roadworks and settings.roadworks_enabled:
        road_works, roadworks_error = _collect_road_works(index, coords, day)
    elif check_roadworks:
        roadworks_error = "Controle op wegwerkzaamheden is uitgeschakeld."

    segments: list[legality.Segment] = []
    legality_error: str | None = None
    if check_legality and settings.legality_enabled:
        segments, legality_error = _collect_legality(coords)
    elif check_legality:
        legality_error = "Controle op verboden paden is uitgeschakeld."

    has_elevation = any(p.ele is not None for p in route_points)
    stats = route_service.build_stats(index, matched, has_elevation)

    job_id = uuid.uuid4().hex
    xml = gpx_service.build_output_gpx(gpx, matched, road_works, segments)
    output_path(job_id).write_text(xml, encoding="utf-8")
    cleanup_jobs()

    min_lat, min_lon, max_lat, max_lon = bounding_box(coords)
    return ProcessResult(
        job_id=job_id,
        filename=_output_filename(filename),
        source=source,
        radius_m=radius_m,
        nl_share=round(share, 3),
        stats=stats,
        route=[[p.lat, p.lon] for p in route_points],
        water_points=[
            WaterPointOut(
                lat=wp.lat,
                lon=wp.lon,
                name=wp.name,
                operator=wp.operator,
                opening_hours=wp.opening_hours,
                website=wp.website,
                source=wp.source,
                distance_to_route_m=wp.distance_to_route_m,
                along_route_km=wp.along_route_km,
            )
            for wp in matched
        ],
        bounds=[[min_lat, min_lon], [max_lat, max_lon]],
        roadworks_checked=check_roadworks,
        roadworks_date=day.isoformat() if check_roadworks else None,
        roadworks_error=roadworks_error,
        road_works=[
            RoadWorkOut(
                lat=w.lat,
                lon=w.lon,
                start=w.start,
                end=w.end,
                cause=w.cause,
                authority=w.authority,
                detour=w.detour,
                lines=w.lines,
                distance_to_route_m=w.distance_to_route_m,
                along_route_km=w.along_route_km,
            )
            for w in road_works
        ],
        legality_checked=check_legality,
        legality_error=legality_error,
        legality_segments=[
            LegalitySegmentOut(
                severity=seg.severity,
                code=seg.code,
                label=seg.label,
                way_name=seg.way_name,
                highway=seg.highway,
                start_km=seg.start_km,
                end_km=seg.end_km,
                length_m=seg.length_m,
                coordinates=[[round(lat, 6), round(lon, 6)] for lat, lon in seg.coordinates],
            )
            for seg in segments
        ],
    )


def _output_filename(filename: str) -> str:
    stem = Path(filename or "route").stem or "route"
    safe = "".join(c for c in stem if c.isalnum() or c in ("-", "_", " ")).strip()
    return f"{safe or 'route'}-water.gpx"
