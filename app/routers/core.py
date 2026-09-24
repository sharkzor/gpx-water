"""Endpoints voor upload, verwerking en download van GPX-bestanden."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.config import get_settings
from app.models.schemas import ProcessResult
from app.services import osm_index, processing, roadworks_nl, waterpoints_nl
from app.services.gpx_service import GpxError
from app.web import templates

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


def validate_options(radius: int | None, source: str) -> int:
    """Valideer zoekradius en bronkeuze; geeft de te gebruiken radius terug."""
    radius_m = radius or settings.default_radius_m
    if radius_m not in settings.allowed_radii:
        raise HTTPException(
            status_code=400,
            detail=f"Ongeldige zoekradius. Kies uit: {list(settings.allowed_radii)}",
        )
    if source not in (
        processing.SOURCE_AUTO,
        processing.SOURCE_NL,
        processing.SOURCE_OSM,
    ):
        raise HTTPException(status_code=400, detail="Ongeldige bronkeuze")
    return radius_m


def parse_ride_date(value: str | None) -> date | None:
    """Lees de gekozen rijdatum; leeg betekent vandaag."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Ongeldige datum, gebruik JJJJ-MM-DD"
        ) from exc


def legality_context() -> dict[str, object]:
    """Template-variabelen voor de optie "Controleer op verboden paden"."""
    return {
        "legality_enabled": settings.legality_enabled,
        "legality_ready": settings.legality_enabled and osm_index.status().available,
    }


def run_processing(
    raw: bytes,
    filename: str,
    radius_m: int,
    source: str,
    check_roadworks: bool = False,
    ride_date: date | None = None,
    check_legality: bool = False,
) -> ProcessResult:
    """Voer de waterpuntenverwerking uit en vertaal fouten naar HTTP-antwoorden."""
    try:
        return processing.process_gpx(
            raw, filename, radius_m, source, check_roadworks, ride_date, check_legality
        )
    except GpxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Databron niet beschikbaar")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - vangnet
        logger.exception("Onverwachte fout bij verwerken")
        raise HTTPException(
            status_code=500, detail=f"Verwerking mislukt: {exc}"
        ) from exc


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "radii": settings.allowed_radii,
            "default_radius": settings.default_radius_m,
            "gap_warning_km": settings.gap_warning_km,
            "roadworks_enabled": settings.roadworks_enabled,
            **legality_context(),
            "today": date.today().isoformat(),
            "strava_enabled": settings.strava_enabled,
            "strava_visible": settings.strava_feature_enabled,
            "routeboek_visible": settings.routeboek_enabled,
            "active": "upload",
        },
    )


@router.get("/api/health")
async def health() -> JSONResponse:
    age = waterpoints_nl.cache_age_seconds()
    rw_age = roadworks_nl.cache_age_seconds() if settings.roadworks_enabled else None
    osm = _osm_status() if settings.legality_enabled else None
    return JSONResponse(
        {
            "status": "ok",
            "nl_cache_age_seconds": None if age is None else round(age),
            "roadworks_enabled": settings.roadworks_enabled,
            "roadworks_cache_age_seconds": None if rw_age is None else round(rw_age),
            "legality_enabled": settings.legality_enabled,
            "osm_map_available": osm["available"] if osm else None,
            "osm_map_age_days": osm["age_days"] if osm else None,
            "data_dir": str(settings.data_dir),
            "strava_enabled": settings.strava_feature_enabled,
            "strava_configured": settings.strava_enabled,
        }
    )


@router.post("/api/process", response_model=ProcessResult)
async def process(
    file: UploadFile = File(...),
    radius: int = Form(default=None),
    source: str = Form(default=processing.SOURCE_AUTO),
    roadworks: bool = Form(default=False),
    ride_date: str = Form(default=None),
    legality: bool = Form(default=False),
) -> ProcessResult:
    radius_m = validate_options(radius, source)
    day = parse_ride_date(ride_date)

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Leeg bestand ontvangen")
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"Bestand is groter dan {settings.max_upload_mb} MB",
        )
    return run_processing(
        raw, file.filename or "route.gpx", radius_m, source, roadworks, day, legality
    )


@router.get("/api/download/{job_id}")
async def download(job_id: str, name: str | None = None) -> FileResponse:
    if not job_id.isalnum():
        raise HTTPException(status_code=400, detail="Ongeldig job-id")
    path = processing.output_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bestand niet (meer) beschikbaar")
    filename = name if name and name.endswith(".gpx") else "route-water.gpx"
    return FileResponse(
        path, media_type="application/gpx+xml", filename=filename.replace("/", "_")
    )


@router.post("/api/roadworks/refresh")
async def refresh_roadworks() -> JSONResponse:
    """Forceer het verversen van de NDW-wegwerkzaamheden (duurt ~1 minuut)."""
    if not settings.roadworks_enabled:
        raise HTTPException(
            status_code=404, detail="Controle op wegwerkzaamheden is uitgeschakeld"
        )
    try:
        works = roadworks_nl.get_road_works(force_refresh=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse({"status": "ok", "count": len(works)})


@router.post("/api/cache/refresh")
async def refresh_cache() -> JSONResponse:
    """Forceer het verversen van de Nederlandse drinkwaterpunten-cache."""
    try:
        points = waterpoints_nl.load_water_points(force_refresh=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse({"status": "ok", "count": len(points)})


def _osm_status() -> dict[str, object]:
    state = osm_index.status()
    job = osm_index.current_job()
    return {
        "available": state.available,
        "way_count": state.way_count,
        "size_mb": state.size_mb,
        "age_days": None if state.age_days is None else round(state.age_days, 1),
        "stale": state.stale,
        "refresh": None
        if job is None
        else {
            "state": job.state,
            "message": job.message,
            "progress": job.progress,
            "error": job.error,
            "result": job.result,
        },
    }


@router.get("/api/osm/status")
async def osm_status() -> JSONResponse:
    """Status van de lokale wegenkaart voor de controle op verboden paden."""
    if not settings.legality_enabled:
        raise HTTPException(status_code=404, detail="Controle op verboden paden is uitgeschakeld")
    return JSONResponse(_osm_status())


@router.post("/api/osm/refresh")
async def osm_refresh() -> JSONResponse:
    """Bouw de wegenkaart opnieuw op (duurt enkele minuten, op de achtergrond)."""
    if not settings.legality_enabled:
        raise HTTPException(status_code=404, detail="Controle op verboden paden is uitgeschakeld")
    free = osm_index.free_memory_mb()
    job = osm_index.current_job()
    running = job is not None and job.state == "running"
    if not running and free is not None and free < settings.osm_min_free_mb:
        raise HTTPException(
            status_code=503,
            detail=f"Te weinig vrij geheugen ({free} MB, {settings.osm_min_free_mb} nodig)",
        )
    osm_index.start_refresh()
    return JSONResponse(_osm_status(), status_code=202)
