"""Routeboek.cc-integratie: routes van een clubpagina scrapen en downloaden.

Routeboek.cc is een oudere ASP.NET-site zonder publieke API. De routelijst
staat volledig in de HTML van de clubpagina; de GPX-link staat alleen op de
detailpagina van elke route. We lezen daarom met gerichte reguliere
expressies (geen extra HTML-parserbibliotheek nodig, zie "houd afhankelijkheden
beperkt") in plaats van een officiële API te gebruiken.

De routelijst wordt kort in het geheugen gecached (``ROUTEBOEK_CACHE_TTL_SECONDS``,
standaard 15 minuten) zodat een paginaverversing niet steeds opnieuw scrapt.
Er is bewust geen periodieke achtergrondsync van alle GPX-bestanden: dat zou
onnodig veel media downloaden en extra belasting op routeboek.cc geven,
terwijl gebruikers meestal maar een handjevol routes verwerken. GPX-bestanden
worden dus alleen opgehaald op het moment dat een route geselecteerd wordt,
net als bij de Strava-integratie.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from urllib.parse import urljoin

import gpxpy
import gpxpy.gpx
import requests

from app.config import get_settings
from app.models.schemas import RouteboekRouteInfo

logger = logging.getLogger(__name__)

_TIMEOUT = 30

_ITEM_RE = re.compile(
    r'route_(?P<id>\d+)"\s+class="routeitem">(?P<body>.*?)'
    r'(?=route_\d+"\s+class="routeitem"|\Z)',
    re.S,
)
_LINK_RE = re.compile(r'<a href="([^"]+)">([^<]+)</a>')
_DISTANCE_RE = re.compile(r'dataitem distance">.*?</span>([\d,.]+)\s*km', re.S)
_ELEVATION_RE = re.compile(r'dataitem hoogte">.*?</span>(\d+)', re.S)
_RATING_RE = re.compile(r'class="stars" style="width:\s*(\d+)px"')
_GPX_LINK_RE = re.compile(r'<a href="([^"]+)"[^>]*id="[^"]*_lnkGPX"')
_COORD_RE = re.compile(r"\{\s*lat:\s*(-?\d+(?:\.\d+)?),\s*lng:\s*(-?\d+(?:\.\d+)?)\s*\}")
_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.S)

_lock = threading.Lock()
_cache: list[RouteboekRouteInfo] = []
_cache_at: float = 0.0


class RouteboekError(RuntimeError):
    """Fout bij communicatie met of het lezen van routeboek.cc."""


def _club_url() -> str:
    settings = get_settings()
    return f"{settings.routeboek_base_url.rstrip('/')}/club/{settings.routeboek_club_slug}"


def _get(url: str) -> str:
    settings = get_settings()
    try:
        response = requests.get(
            url, timeout=_TIMEOUT, headers={"User-Agent": settings.user_agent}
        )
    except requests.RequestException as exc:
        raise RouteboekError(f"routeboek.cc niet bereikbaar: {exc}") from exc
    if response.status_code >= 400:
        raise RouteboekError(f"routeboek.cc-fout {response.status_code} bij {url}")
    return response.text


def _nl_float(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _parse_overview(document: str) -> list[RouteboekRouteInfo]:
    club_url = _club_url()
    routes: list[RouteboekRouteInfo] = []
    seen: set[int] = set()
    for match in _ITEM_RE.finditer(document):
        route_id = int(match.group("id"))
        if route_id in seen:
            continue
        seen.add(route_id)
        body = match.group("body")

        link = _LINK_RE.search(body)
        if not link:
            continue
        slug = link.group(1).rstrip("/").rsplit("/", 1)[-1]
        name = html.unescape(link.group(2)).strip()

        distance = _DISTANCE_RE.search(body)
        elevation = _ELEVATION_RE.search(body)
        rating = _RATING_RE.search(body)

        routes.append(
            RouteboekRouteInfo(
                id=str(route_id),
                slug=slug,
                name=name or f"Route {route_id}",
                distance_km=_nl_float(distance.group(1)) if distance else None,
                elevation_m=int(elevation.group(1)) if elevation else None,
                rating=round(int(rating.group(1)) / 20, 1) if rating else None,
                url=f"{club_url}/route/{slug}",
            )
        )
    return routes


def list_routes(force_refresh: bool = False) -> list[RouteboekRouteInfo]:
    """Geef de routes van de club, uit cache tenzij die verlopen of leeg is."""
    global _cache_at
    settings = get_settings()
    with _lock:
        age = time.time() - _cache_at
        if not force_refresh and _cache and age < settings.routeboek_cache_ttl_seconds:
            return list(_cache)

    document = _get(_club_url())
    routes = _parse_overview(document)
    if not routes:
        raise RouteboekError(
            "Geen routes gevonden op routeboek.cc; is de clubnaam "
            f"'{settings.routeboek_club_slug}' juist en de pagina publiek?"
        )
    with _lock:
        _cache.clear()
        _cache.extend(routes)
        _cache_at = time.time()
    logger.info("%d routes gescraped van routeboek.cc", len(routes))
    return list(routes)


def _gpx_from_map(document: str, slug: str) -> bytes | None:
    """Bouw een GPX uit de kaartcoördinaten op de detailpagina.

    Terugval voor routes waarvan het GPX-bestand op routeboek.cc ontbreekt
    (gemeten: 1 van de 166 Stampers-routes). Zonder hoogtegegevens.
    """
    points = [(float(lat), float(lon)) for lat, lon in _COORD_RE.findall(document)]
    if len(points) < 2:
        return None
    title = _TITLE_RE.search(document)
    name = html.unescape(title.group(1)).split(" - ")[-1].strip() if title else slug
    gpx = gpxpy.gpx.GPX()
    gpx.creator = "gpx-waterpoints (routeboek.cc-kaart)"
    gpx.name = name
    gpx.description = (
        "Opgebouwd uit de kaart op routeboek.cc; het originele GPX-bestand "
        "ontbreekt daar. Geen hoogtegegevens."
    )
    track = gpxpy.gpx.GPXTrack(name=name)
    segment = gpxpy.gpx.GPXTrackSegment(
        [gpxpy.gpx.GPXTrackPoint(lat, lon) for lat, lon in points]
    )
    track.segments.append(segment)
    gpx.tracks.append(track)
    logger.warning(
        "GPX van '%s' ontbreekt op routeboek.cc; opgebouwd uit %d kaartpunten",
        slug,
        len(points),
    )
    return gpx.to_xml(version="1.1").encode("utf-8")


def _download(url: str) -> bytes | None:
    """Download een GPX; ``None`` als het bestand niet bestaat."""
    settings = get_settings()
    try:
        response = requests.get(
            url, timeout=_TIMEOUT, headers={"User-Agent": settings.user_agent}
        )
    except requests.RequestException as exc:
        raise RouteboekError(f"GPX-download mislukt: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise RouteboekError(f"GPX-download mislukt ({response.status_code})")
    content = response.content
    if b"<gpx" not in content[:2000].lower():
        return None
    return content


def export_gpx(slug: str) -> bytes:
    """Download de originele GPX van een route via de detailpagina.

    Ontbreekt dat bestand, dan wordt de route opgebouwd uit de kaart op de
    detailpagina.
    """
    if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
        raise RouteboekError(f"Ongeldige route-slug: {slug}")
    detail_url = f"{_club_url()}/route/{slug}"
    document = _get(detail_url)
    match = _GPX_LINK_RE.search(document)
    content = None
    if match:
        content = _download(urljoin(detail_url, html.unescape(match.group(1))))
    if content is None:
        content = _gpx_from_map(document, slug)
    if content is None:
        raise RouteboekError("Deze route heeft geen GPX en geen kaart op routeboek.cc.")
    logger.info("GPX van routeboek.cc-route '%s' opgehaald (%d bytes)", slug, len(content))
    return content
