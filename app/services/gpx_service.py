"""Lezen en schrijven van GPX-bestanden."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable
from xml.sax.saxutils import escape

import gpxpy
import gpxpy.gpx

from app.config import get_settings
from app.models.schemas import RoadWork, RoutePoint, WaterPoint

if TYPE_CHECKING:
    from app.services.legality import Segment

logger = logging.getLogger(__name__)

#: Bronvermelding in de omschrijving van werkzaamheden-waypoints.
SOURCE_NDW = "NDW/Melvin"
SOURCE_OSM = "OpenStreetMap"


class GpxError(ValueError):
    """Ongeldig of onbruikbaar GPX-bestand."""


def parse_gpx(raw: bytes | str) -> gpxpy.gpx.GPX:
    """Parse GPX-inhoud naar een gpxpy object."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    else:
        text = raw
    try:
        return gpxpy.parse(text)
    except Exception as exc:  # gpxpy gooit diverse excepties
        raise GpxError(f"Kan GPX niet lezen: {exc}") from exc


def extract_route_points(gpx: gpxpy.gpx.GPX) -> list[RoutePoint]:
    """Haal routepunten uit tracks en/of routes, in bestandsvolgorde.

    Hoogte wordt behouden waar aanwezig.
    """
    points: list[RoutePoint] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                points.append(RoutePoint(p.latitude, p.longitude, p.elevation))
    if not points:
        for route in gpx.routes:
            for p in route.points:
                points.append(RoutePoint(p.latitude, p.longitude, p.elevation))
    if len(points) < 2:
        raise GpxError(
            "Geen bruikbare route gevonden: het bestand bevat geen track of route "
            "met minimaal twee punten."
        )
    return _dedupe_consecutive(points)


def _dedupe_consecutive(points: list[RoutePoint]) -> list[RoutePoint]:
    cleaned: list[RoutePoint] = []
    for p in points:
        if cleaned and cleaned[-1].lat == p.lat and cleaned[-1].lon == p.lon:
            continue
        cleaned.append(p)
    return cleaned


def parse_waypoints(gpx: gpxpy.gpx.GPX, source: str) -> list[WaterPoint]:
    """Zet GPX-waypoints om naar WaterPoint objecten."""
    result: list[WaterPoint] = []
    for wpt in gpx.waypoints:
        result.append(
            WaterPoint(
                lat=wpt.latitude,
                lon=wpt.longitude,
                name=(wpt.name or None),
                description=(wpt.description or wpt.comment or None),
                website=(wpt.link or None),
                ele=wpt.elevation,
                source=source,
            )
        )
    return result


def _xml_attr(value: str) -> str:
    """gpxpy schrijft attributen ongeescaped weg; doe dat hier zelf."""
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _describe(wp: WaterPoint) -> str:
    parts: list[str] = []
    if wp.name:
        parts.append(wp.name)
    if wp.operator:
        parts.append(f"Beheerder: {wp.operator}")
    if wp.opening_hours:
        parts.append(f"Open: {wp.opening_hours}")
    if wp.website:
        parts.append(wp.website)
    if wp.description and wp.description not in parts:
        parts.append(wp.description)
    parts.append(f"Afstand tot route: {wp.distance_to_route_m:.0f} m")
    parts.append(f"Bron: {wp.source}")
    return " | ".join(parts)


def _sanitize_links(gpx: gpxpy.gpx.GPX) -> None:
    """Escape link-URL's uit het originele bestand zodat de output geldige XML blijft."""
    holders: list[object] = [gpx, *gpx.waypoints, *gpx.routes, *gpx.tracks]
    for holder in holders:
        link = getattr(holder, "link", None)
        if isinstance(link, str):
            holder.link = _xml_attr(link)  # type: ignore[attr-defined]
    if isinstance(getattr(gpx, "author_link", None), str):
        gpx.author_link = _xml_attr(gpx.author_link)


def _describe_road_work(work: RoadWork) -> str:
    parts: list[str] = []
    if work.cause:
        parts.append(work.cause)
    if work.authority:
        parts.append(work.authority)
    if work.start or work.end:
        parts.append(f"Periode: {work.start or '?'} t/m {work.end or '?'}")
    if work.detour:
        parts.append(work.detour)
    parts.append(f"Afstand tot route: {work.distance_to_route_m:.0f} m")
    parts.append(f"Bron: {SOURCE_NDW}")
    return " | ".join(parts)


def _describe_segment(segment: "Segment") -> str:
    parts = [segment.label]
    if segment.way_name:
        parts.append(segment.way_name)
    length = (
        f"{segment.length_m / 1000:.1f} km".replace(".", ",")
        if segment.length_m >= 1000
        else f"{segment.length_m:.0f} m"
    )
    parts.append(f"km {segment.start_km:.1f} t/m {segment.end_km:.1f} ({length})")
    parts.append(f"Bron: {SOURCE_OSM}")
    return " | ".join(parts)


def build_output_gpx(
    original: gpxpy.gpx.GPX,
    water_points: Iterable[WaterPoint],
    road_works: Iterable[RoadWork] = (),
    segments: Iterable["Segment"] = (),
) -> str:
    """Voeg drinkwater-, werkzaamheden- en verboden-pad-waypoints toe aan de GPX.

    Een verboden stuk wordt een waypoint op het beginpunt, zodat de Wahoo
    waarschuwt voordat je het pad op rijdt.

    De originele tracks, routes, hoogtes en metadata blijven behouden.
    Waypoints krijgen sym/type die de Wahoo ELEMNT ROAM herkent.
    """
    settings = get_settings()
    gpx = original
    gpx.version = "1.1"
    if not gpx.creator or "gpxpy" in (gpx.creator or ""):
        gpx.creator = "gpx-waterpoints"
    _sanitize_links(gpx)

    for wp in water_points:
        name = settings.waypoint_prefix
        if settings.waypoint_with_km:
            name = f"{name} - {wp.along_route_km:.0f} km"
        point = gpxpy.gpx.GPXWaypoint(
            latitude=round(wp.lat, 6),
            longitude=round(wp.lon, 6),
            elevation=wp.ele,
            name=name,
            description=_describe(wp),
            symbol=settings.waypoint_sym,
            type=settings.waypoint_type,
        )
        point.comment = wp.name or settings.waypoint_prefix
        if wp.website:
            point.link = _xml_attr(wp.website)
            point.link_text = "info"
        gpx.waypoints.append(point)

    for work in road_works:
        name = settings.roadworks_prefix
        if settings.waypoint_with_km:
            name = f"{name} - {work.along_route_km:.0f} km"
        point = gpxpy.gpx.GPXWaypoint(
            latitude=round(work.lat, 6),
            longitude=round(work.lon, 6),
            name=name,
            description=_describe_road_work(work),
            symbol=settings.roadworks_sym,
            type=settings.roadworks_type,
        )
        point.comment = work.cause or settings.roadworks_prefix
        gpx.waypoints.append(point)

    for segment in segments:
        forbidden = segment.severity == "forbidden"
        name = (
            settings.legality_prefix_forbidden
            if forbidden
            else settings.legality_prefix_warning
        )
        if settings.waypoint_with_km:
            name = f"{name} - {segment.start_km:.0f} km"
        lat, lon = segment.coordinates[0]
        point = gpxpy.gpx.GPXWaypoint(
            latitude=round(lat, 6),
            longitude=round(lon, 6),
            name=name,
            description=_describe_segment(segment),
            symbol=settings.legality_sym,
            type="Forbidden" if forbidden else "Caution",
        )
        point.comment = segment.label
        gpx.waypoints.append(point)

    xml = gpx.to_xml(version="1.1")
    logger.info("GPX gegenereerd met %d waypoints", len(gpx.waypoints))
    return xml
