"""Strava API-integratie: OAuth2, routes ophalen en GPX exporteren."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests

from app.config import get_settings
from app.models.schemas import StravaRouteInfo
from app.services import token_store

logger = logging.getLogger(__name__)

_TIMEOUT = 30

#: Sessie-id voor de koppeling die uit environment variables komt.
ENV_SESSION_ID = "__env__"


class StravaError(RuntimeError):
    """Fout bij communicatie met Strava."""


class StravaAuthError(StravaError):
    """Koppeling ontbreekt of is niet meer geldig."""


def authorize_url(state: str) -> str:
    """URL waar de gebruiker naartoe gestuurd wordt om toegang te geven."""
    settings = get_settings()
    if not settings.strava_oauth_enabled:
        raise StravaError(
            "Strava is niet geconfigureerd: stel STRAVA_CLIENT_ID, "
            "STRAVA_CLIENT_SECRET en STRAVA_REDIRECT_URI in."
        )
    query = urlencode(
        {
            "client_id": settings.strava_client_id,
            "redirect_uri": settings.strava_redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": settings.strava_scope,
            "state": state,
        }
    )
    return f"{settings.strava_oauth_base}/authorize?{query}"


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    settings = get_settings()
    body = {
        "client_id": settings.strava_client_id,
        "client_secret": settings.strava_client_secret,
        **payload,
    }
    try:
        response = requests.post(
            f"{settings.strava_oauth_base}/token",
            data=body,
            timeout=_TIMEOUT,
            headers={"User-Agent": settings.user_agent},
        )
    except requests.RequestException as exc:
        raise StravaError(f"Strava niet bereikbaar: {exc}") from exc
    if response.status_code >= 400:
        raise StravaAuthError(
            f"Strava weigerde de aanvraag ({response.status_code}): {response.text[:200]}"
        )
    return response.json()


def _to_session(token: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    athlete = token.get("athlete") or (previous or {}).get("athlete") or {}
    return {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token")
        or (previous or {}).get("refresh_token", ""),
        "expires_at": float(token.get("expires_at", 0)),
        "scope": token.get("scope") or (previous or {}).get("scope", ""),
        "athlete": {
            "id": athlete.get("id"),
            "firstname": athlete.get("firstname"),
            "lastname": athlete.get("lastname"),
            "username": athlete.get("username"),
        },
    }


def exchange_code(code: str) -> dict[str, Any]:
    """Wissel de OAuth-code om voor tokens."""
    token = _post_token({"code": code, "grant_type": "authorization_code"})
    logger.info("Strava-koppeling gemaakt voor athlete %s", (token.get("athlete") or {}).get("id"))
    return _to_session(token)


def refresh(session: dict[str, Any]) -> dict[str, Any]:
    """Vernieuw een verlopen access token."""
    if not session.get("refresh_token"):
        raise StravaAuthError("Geen refresh token beschikbaar; koppel opnieuw.")
    try:
        token = _post_token(
            {"refresh_token": session["refresh_token"], "grant_type": "refresh_token"}
        )
    except StravaAuthError as exc:
        if session.get("origin") == "env":
            raise StravaAuthError(
                "Strava weigert het ingestelde token. Controleer "
                "STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET en STRAVA_REFRESH_TOKEN "
                f"in je .env. ({exc})"
            ) from exc
        raise
    logger.info("Strava access token vernieuwd")
    return _to_session(token, session)


def env_session() -> dict[str, Any] | None:
    """Bouw een sessie uit de environment variables (STRAVA_REFRESH_TOKEN)."""
    settings = get_settings()
    if not settings.strava_token_configured:
        return None
    stored = token_store.get(ENV_SESSION_ID)
    if stored and stored.get("origin_refresh_token") == settings.strava_refresh_token:
        return stored
    session = {
        "access_token": settings.strava_access_token,
        "refresh_token": settings.strava_refresh_token,
        "origin_refresh_token": settings.strava_refresh_token,
        "expires_at": settings.strava_expires_at,
        "scope": settings.strava_scope,
        "athlete": {},
        "origin": "env",
    }
    token_store.put(ENV_SESSION_ID, session)
    logger.info("Strava-sessie opgezet vanuit environment variables")
    return session


def _ensure_athlete(session_id: str, session: dict[str, Any]) -> dict[str, Any]:
    """Vul de atleetgegevens aan als die nog ontbreken (bij vaste tokens)."""
    if (session.get("athlete") or {}).get("id"):
        return session
    try:
        athlete = _api_get(session, "/athlete").json()
    except StravaError as exc:
        logger.debug("Atleetgegevens niet opgehaald: %s", exc)
        return session
    session["athlete"] = {
        "id": athlete.get("id"),
        "firstname": athlete.get("firstname"),
        "lastname": athlete.get("lastname"),
        "username": athlete.get("username"),
    }
    token_store.put(session_id, session)
    return session


def stored_session(session_id: str | None) -> tuple[str, dict[str, Any]] | None:
    """Geef de opgeslagen sessie van de browser, of anders die uit de environment."""
    if session_id:
        session = token_store.get(session_id)
        if session and session.get("access_token"):
            return session_id, session
    env = env_session()
    if env:
        return ENV_SESSION_ID, env
    return None


def valid_session(session_id: str) -> dict[str, Any]:
    """Geef een sessie met geldig access token; ververst automatisch."""
    found = stored_session(session_id)
    if not found:
        raise StravaAuthError("Nog niet met Strava verbonden.")
    session_id, session = found
    if not session.get("access_token") or float(session.get("expires_at", 0)) - 120 <= time.time():
        origin = session.get("origin")
        origin_token = session.get("origin_refresh_token")
        session = refresh(session)
        if origin:
            session["origin"] = origin
            session["origin_refresh_token"] = origin_token
        token_store.put(session_id, session)
    return _ensure_athlete(session_id, session)


def _api_get(session: dict[str, Any], path: str, **params: Any) -> requests.Response:
    settings = get_settings()
    try:
        response = requests.get(
            f"{settings.strava_api_base}{path}",
            params=params or None,
            headers={
                "Authorization": f"Bearer {session['access_token']}",
                "User-Agent": settings.user_agent,
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StravaError(f"Strava niet bereikbaar: {exc}") from exc
    if response.status_code in (401, 403):
        raise StravaAuthError(
            "Strava-toegang geweigerd. Koppel opnieuw en geef toestemming voor "
            "'read_all' zodat ook privéroutes gelezen mogen worden."
        )
    if response.status_code == 429:
        raise StravaError("Strava rate limit bereikt; probeer het straks opnieuw.")
    if response.status_code >= 400:
        raise StravaError(f"Strava-fout {response.status_code}: {response.text[:200]}")
    return response


def _route_info(item: dict[str, Any]) -> StravaRouteInfo:
    route_id = str(item.get("id_str") or item.get("id"))
    elevation = item.get("elevation_gain")
    return StravaRouteInfo(
        id=route_id,
        name=item.get("name") or f"Route {route_id}",
        distance_km=round(float(item.get("distance") or 0) / 1000.0, 2),
        elevation_gain_m=None if elevation in (None, "") else round(float(elevation)),
        type={1: "Ride", 2: "Run"}.get(item.get("type"), None),
        private=bool(item.get("private")),
        url=f"https://www.strava.com/routes/{route_id}",
    )


def list_routes(session: dict[str, Any], limit: int | None = None) -> list[StravaRouteInfo]:
    """Haal de routes van de ingelogde atleet op (met paginering)."""
    settings = get_settings()
    limit = limit or settings.strava_max_routes
    per_page = 50
    routes: list[StravaRouteInfo] = []
    page = 1
    while len(routes) < limit:
        payload = _api_get(session, "/athlete/routes", page=page, per_page=per_page).json()
        if not isinstance(payload, list) or not payload:
            break
        routes.extend(_route_info(item) for item in payload)
        if len(payload) < per_page:
            break
        page += 1
    logger.info("%d Strava-routes opgehaald", len(routes))
    return routes[:limit]


def export_gpx(session: dict[str, Any], route_id: str) -> bytes:
    """Download de originele GPX van een route."""
    if not str(route_id).isdigit():
        raise StravaError(f"Ongeldig route-id: {route_id}")
    response = _api_get(session, f"/routes/{route_id}/export_gpx")
    content = response.content
    if b"<gpx" not in content[:2000].lower():
        raise StravaError("Strava gaf geen GPX terug voor deze route.")
    logger.info("GPX van Strava-route %s opgehaald (%d bytes)", route_id, len(content))
    return content
