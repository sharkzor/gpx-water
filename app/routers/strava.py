"""Strava-integratie: OAuth-koppeling, routes tonen en verwerken."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from app.config import get_settings
from app.models.schemas import (
    StravaAthlete,
    StravaProcessedRoute,
    StravaRouteInfo,
    StravaStatus,
)
from app.routers.core import (
    legality_context,
    parse_ride_date,
    run_processing,
    validate_options,
)
from app.services import processing, strava_service, token_store
from app.services.strava_service import StravaAuthError, StravaError
from app.web import templates

logger = logging.getLogger(__name__)
settings = get_settings()


def _require_feature() -> None:
    """Blokkeer de hele Strava-module wanneer STRAVA_ENABLED uit staat.

    Alle endpoints gedragen zich dan alsof ze niet bestaan, zodat een publieke
    installatie geen enkel Strava-oppervlak heeft.
    """
    if not settings.strava_feature_enabled:
        raise HTTPException(status_code=404, detail="Strava-integratie is uitgeschakeld")


router = APIRouter(dependencies=[Depends(_require_feature)])

_STATE_MAX_AGE = 600


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(token_store.get_secret(), salt=salt)


def _read_session_id(request: Request) -> str | None:
    cookie = request.cookies.get(settings.session_cookie)
    if not cookie:
        return None
    try:
        return _serializer("session").loads(cookie, max_age=settings.session_ttl_seconds)
    except (BadSignature, SignatureExpired):
        return None


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        settings.session_cookie,
        _serializer("session").dumps(session_id),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _require_session(request: Request) -> tuple[str, dict]:
    session_id = _read_session_id(request) or ""
    try:
        return session_id, strava_service.valid_session(session_id)
    except StravaAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except StravaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/strava", response_class=HTMLResponse)
async def strava_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "strava.html",
        {
            "radii": settings.allowed_radii,
            "default_radius": settings.default_radius_m,
            "strava_enabled": settings.strava_enabled,
            "strava_visible": True,
            "roadworks_enabled": settings.roadworks_enabled,
            **legality_context(),
            "today": date.today().isoformat(),
            "oauth_available": settings.strava_oauth_enabled,
            "routeboek_visible": settings.routeboek_enabled,
            "active": "strava",
        },
    )


@router.get("/api/strava/status", response_model=StravaStatus)
def status(request: Request) -> StravaStatus:
    found = strava_service.stored_session(_read_session_id(request))
    session = found[1] if found else None
    athlete = (session or {}).get("athlete") or None
    return StravaStatus(
        configured=settings.strava_enabled,
        connected=bool(session),
        athlete=StravaAthlete(**athlete) if athlete else None,
        scope=(session or {}).get("scope"),
        mode=("env" if (session or {}).get("origin") == "env" else "oauth")
        if session
        else None,
        oauth_available=settings.strava_oauth_enabled,
    )


@router.get("/strava/connect")
def connect(request: Request) -> RedirectResponse:
    """Start de OAuth-koppeling met Strava."""
    session_id = _read_session_id(request) or token_store.new_session_id()
    try:
        url = strava_service.authorize_url(_serializer("oauth-state").dumps(session_id))
    except StravaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response = RedirectResponse(url, status_code=307)
    _set_session_cookie(response, session_id)
    return response


@router.get("/strava/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    scope: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Callback-URL die je in de Strava-app instelt als 'Authorization Callback Domain'."""
    if error:
        logger.warning("Strava-koppeling geweigerd: %s", error)
        return RedirectResponse(f"/strava?error={error}", status_code=303)
    if not code or not state:
        return RedirectResponse("/strava?error=ontbrekende_code", status_code=303)

    try:
        session_id = _serializer("oauth-state").loads(state, max_age=_STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        logger.warning("Ongeldige OAuth-state ontvangen")
        return RedirectResponse("/strava?error=ongeldige_state", status_code=303)

    cookie_session = _read_session_id(request)
    if cookie_session and cookie_session != session_id:
        return RedirectResponse("/strava?error=ongeldige_state", status_code=303)

    try:
        session = strava_service.exchange_code(code)
    except StravaError as exc:
        logger.error("Tokenuitwisseling mislukt: %s", exc)
        return RedirectResponse("/strava?error=koppeling_mislukt", status_code=303)

    if scope and not session.get("scope"):
        session["scope"] = scope
    token_store.put(session_id, session)

    response = RedirectResponse("/strava?connected=1", status_code=303)
    _set_session_cookie(response, session_id)
    return response


@router.post("/api/strava/disconnect")
def disconnect(request: Request) -> dict[str, str]:
    session_id = _read_session_id(request)
    if session_id:
        token_store.delete(session_id)
    if settings.strava_token_configured:
        return {
            "status": "ok",
            "note": "Er blijft een vast token uit de environment variables actief.",
        }
    return {"status": "ok"}


@router.get("/api/strava/routes", response_model=list[StravaRouteInfo])
def routes(request: Request) -> list[StravaRouteInfo]:
    _, session = _require_session(request)
    try:
        return strava_service.list_routes(session)
    except StravaAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except StravaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class StravaProcessRequest(BaseModel):
    route_ids: list[str] = Field(default_factory=list, max_length=25)
    radius: int | None = None
    source: str = processing.SOURCE_AUTO
    roadworks: bool = False
    ride_date: str | None = None
    legality: bool = False


@router.post("/api/strava/process", response_model=list[StravaProcessedRoute])
def process_routes(
    request: Request, payload: StravaProcessRequest
) -> list[StravaProcessedRoute]:
    """Download de gekozen Strava-routes en verrijk ze met drinkwaterpunten."""
    _, session = _require_session(request)
    if not payload.route_ids:
        raise HTTPException(status_code=400, detail="Selecteer minimaal één route")
    radius_m = validate_options(payload.radius, payload.source)
    day = parse_ride_date(payload.ride_date)

    known = {route.id: route for route in strava_service.list_routes(session)}
    results: list[StravaProcessedRoute] = []

    for route_id in payload.route_ids:
        info = known.get(str(route_id))
        name = info.name if info else f"Route {route_id}"
        try:
            raw = strava_service.export_gpx(session, str(route_id))
            original_job = processing.store_file(raw)
            result = run_processing(
                raw,
                processing.safe_filename(name),
                radius_m,
                payload.source,
                payload.roadworks,
                day,
                payload.legality,
            )
            result.filename = processing.safe_filename(name, "_waterpunten")
            results.append(
                StravaProcessedRoute(
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
                StravaProcessedRoute(route_id=str(route_id), name=name, error=str(exc.detail))
            )
        except StravaError as exc:
            logger.warning("Route %s mislukt: %s", route_id, exc)
            results.append(
                StravaProcessedRoute(route_id=str(route_id), name=name, error=str(exc))
            )

    return results
