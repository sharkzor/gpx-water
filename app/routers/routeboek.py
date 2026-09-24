"""Routeboek.cc-integratie: routes van de clubpagina tonen en verwerken."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.models.schemas import RouteboekProcessedRoute, RouteboekRouteInfo
from app.routers.core import (
    legality_context,
    parse_ride_date,
    parse_weather,
    require_any_check,
    run_processing,
    validate_options,
    weather_context,
)
from app.services import processing, routeboek_service
from app.services.routeboek_service import RouteboekError
from app.web import templates

logger = logging.getLogger(__name__)
settings = get_settings()


def _require_feature() -> None:
    """Blokkeer de hele routeboek-module wanneer ROUTEBOEK_ENABLED uit staat."""
    if not settings.routeboek_enabled:
        raise HTTPException(status_code=404, detail="Routeboek.cc-integratie is uitgeschakeld")


router = APIRouter(dependencies=[Depends(_require_feature)])


@router.get("/routeboek", response_class=HTMLResponse)
async def routeboek_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "routeboek.html",
        {
            "radii": settings.allowed_radii,
            "default_radius": settings.default_radius_m,
            "strava_enabled": settings.strava_enabled,
            "strava_visible": settings.strava_feature_enabled,
            "roadworks_enabled": settings.roadworks_enabled,
            **legality_context(),
            **weather_context(),
            "today": date.today().isoformat(),
            "club_slug": settings.routeboek_club_slug,
            "active": "routeboek",
        },
    )


@router.get("/api/routeboek/routes", response_model=list[RouteboekRouteInfo])
def routes(refresh: bool = False) -> list[RouteboekRouteInfo]:
    try:
        return routeboek_service.list_routes(force_refresh=refresh)
    except RouteboekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class RouteboekProcessRequest(BaseModel):
    route_ids: list[str] = Field(default_factory=list, max_length=25)
    water: bool = True
    radius: int | None = None
    source: str = processing.SOURCE_AUTO
    roadworks: bool = False
    ride_date: str | None = None
    legality: bool = False
    weather: bool = False
    departure: str | None = None
    speed_kmh: float | None = None


@router.post("/api/routeboek/process", response_model=list[RouteboekProcessedRoute])
def process_routes(payload: RouteboekProcessRequest) -> list[RouteboekProcessedRoute]:
    """Download de gekozen routeboek.cc-routes en voer de gekozen controles uit."""
    if not payload.route_ids:
        raise HTTPException(status_code=400, detail="Selecteer minimaal één route")
    require_any_check(payload.water, payload.roadworks, payload.legality, payload.weather)
    radius_m = validate_options(payload.radius, payload.source)
    day = parse_ride_date(payload.ride_date)
    weather_request = parse_weather(payload.weather, payload.departure, payload.speed_kmh)

    known = {route.id: route for route in routeboek_service.list_routes()}
    results: list[RouteboekProcessedRoute] = []

    for route_id in payload.route_ids:
        info = known.get(str(route_id))
        name = info.name if info else f"Route {route_id}"
        if not info:
            results.append(
                RouteboekProcessedRoute(
                    route_id=str(route_id), name=name, error="Route niet (meer) gevonden."
                )
            )
            continue
        try:
            raw = routeboek_service.export_gpx(info.slug)
            original_job = processing.store_file(raw)
            result = run_processing(
                raw,
                processing.safe_filename(name),
                radius_m,
                payload.source,
                payload.roadworks,
                day,
                payload.legality,
                weather_request,
                payload.water,
            )
            result.filename = processing.safe_filename(
                name, processing.output_suffix(payload.water)
            )
            results.append(
                RouteboekProcessedRoute(
                    route_id=str(route_id),
                    name=name,
                    original_job_id=original_job,
                    original_filename=processing.safe_filename(name),
                    result=result,
                )
            )
        except HTTPException as exc:
            logger.warning("Route %s mislukt: %s", route_id, exc.detail)
            results.append(
                RouteboekProcessedRoute(route_id=str(route_id), name=name, error=str(exc.detail))
            )
        except RouteboekError as exc:
            logger.warning("Route %s mislukt: %s", route_id, exc)
            results.append(
                RouteboekProcessedRoute(route_id=str(route_id), name=name, error=str(exc))
            )

    return results
