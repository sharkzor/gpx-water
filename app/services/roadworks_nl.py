"""Wegwerkzaamheden voor fietsers uit de NDW/Melvin planningsfeed.

De landelijke feed is een DATEX II-bestand van ~15 MB gzip (~170 MB XML) met
tienduizenden meldingen. Gemeenten voeren hier hun eigen werkzaamheden in, wat
deze bron juist bruikbaar maakt voor regionale fietspaden.

Omdat het bestand groot is, wordt het gestreamd geparseerd en teruggebracht tot
een compacte JSON-cache met alleen de meldingen die fietsers raken en die een
coordinaat hebben. Die cache wordt maximaal eens per 24 uur vernieuwd.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests

from app.config import get_settings
from app.models.schemas import RoadWork
from app.services.geo import bounding_box

logger = logging.getLogger(__name__)

SOURCE_NAME = "NDW/Melvin"
_LOCK = threading.Lock()

#: Voertuigsoorten die betekenen dat de melding fietsers raakt.
_BIKE_VEHICLES = {"bicycle", "moped", "motorscooter"}

_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

#: Alleen dit recordtype beschrijft het afgesloten stuk. Het omleidingsrecord
#: (ReroutingManagement) heeft ook lijnen, maar dat is juist de weg die open is.
_CLOSURE_RECORD = "RoadOrCarriagewayOrLaneManagement"

def _local(tag: str) -> str:
    """Strip de XML-namespace van een tagnaam."""
    return re.sub(r"\{.*\}", "", tag)


#: Ophogen als het cacheformaat wijzigt; v2 voegde de afgesloten stukken toe.
_CACHE_VERSION = 2


def _cache_file() -> Path:
    return get_settings().cache_dir / f"wegwerkzaamheden_fiets_v{_CACHE_VERSION}.json"


def _remove_old_caches() -> None:
    current = _cache_file()
    for old in current.parent.glob("wegwerkzaamheden_fiets*.json"):
        if old != current:
            old.unlink(missing_ok=True)


def cache_age_seconds() -> float | None:
    path = _cache_file()
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _feed_path() -> Path:
    return get_settings().cache_dir / "ndw_planningsfeed.xml.gz"


def _download() -> Path:
    """Haal de gzip-feed op en bewaar hem; geeft het pad terug."""
    settings = get_settings()
    logger.info("Download NDW planningsfeed van %s", settings.roadworks_url)
    path = _feed_path()
    tmp = path.with_suffix(".part")
    with requests.get(
        settings.roadworks_url,
        timeout=settings.roadworks_timeout,
        headers={"User-Agent": settings.user_agent},
        stream=True,
    ) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
    tmp.replace(path)
    logger.info("Feed opgehaald: %.1f MB", path.stat().st_size / 1e6)
    return path


def _values_under(element: ET.Element) -> str | None:
    """Lees de tekst uit een DATEX II <values><value lang="nl">…</value></values>."""
    for sub in element.iter():
        if _local(sub.tag) == "value" and (sub.text or "").strip():
            return _clean(sub.text)
    return _clean(element.text)


def _clean(value: str | None) -> str | None:
    """Haal lege komma-restanten weg die Melvin regelmatig oplevert."""
    if not value:
        return None
    text = re.sub(r"[\s,]+$", "", value.strip())
    text = re.sub(r",\s*,", ",", text)
    return text or None


def _first_text(node: ET.Element, tag: str) -> str | None:
    for child in node.iter():
        if _local(child.tag) == tag:
            value = _values_under(child)
            if value:
                return value
    return None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10] if len(value) >= 10 else None


#: Melvin vult de omleidingstekst soms met een kaal volgnummer ("Omleiding 5").
_EMPTY_DETOUR = re.compile(r"^omleiding\s*\d*$", re.IGNORECASE)


def _useful_detour(value: str | None) -> str | None:
    if not value or _EMPTY_DETOUR.match(value.strip()):
        return None
    return value


def _closure_lines(situation: ET.Element) -> list[list[list[float]]]:
    """De afgesloten stukken als lijnen, uit de afsluitingsrecords.

    Records die expliciet alleen ander verkeer noemen slaan we over; noemt een
    record geen voertuigsoort, dan geldt de afsluiting voor iedereen.
    """
    lines: list[list[list[float]]] = []
    for record in situation:
        if _local(record.tag) != "situationRecord":
            continue
        if record.get(_XSI_TYPE, "").split(":")[-1] != _CLOSURE_RECORD:
            continue
        vehicles = {
            (c.text or "").strip()
            for c in record.iter()
            if _local(c.tag) == "vehicleType"
        }
        if vehicles and not vehicles & _BIKE_VEHICLES:
            continue
        for node in record.iter():
            if _local(node.tag) != "posList" or not node.text:
                continue
            values = [float(v) for v in node.text.split()]
            line = [
                [round(values[i], 6), round(values[i + 1], 6)]
                for i in range(0, len(values) - 1, 2)
            ]
            if len(line) >= 2:
                lines.append(line)
    return lines


def _parse_situation(situation: ET.Element) -> dict | None:
    """Zet een DATEX II <situation> om naar een compacte dict.

    Een situatie bundelt meerdere situationRecords: de afsluiting zelf en de
    bijbehorende omleiding. Alleen het omleidingsrecord bevat coordinaten,
    terwijl de oorzaak vaak in het afsluitingsrecord staat. Door op
    situatieniveau te lezen krijgen we beide compleet, en verdwijnen de
    dubbelingen tussen die records vanzelf.
    """
    vehicles = {
        (c.text or "").strip()
        for c in situation.iter()
        if _local(c.tag) == "vehicleType"
    }
    if not vehicles & _BIKE_VEHICLES:
        return None

    lat = lon = None
    for child in situation.iter():
        if _local(child.tag) != "pointCoordinates":
            continue
        for sub in child:
            tag = _local(sub.tag)
            if tag == "latitude" and sub.text:
                lat = float(sub.text)
            elif tag == "longitude" and sub.text:
                lon = float(sub.text)
        if lat is not None and lon is not None:
            break
    lines = _closure_lines(situation)
    if (lat is None or lon is None) and lines:
        lat, lon = lines[0][0]
    if lat is None or lon is None:
        return None

    starts = [
        _iso_date(c.text)
        for c in situation.iter()
        if _local(c.tag) == "overallStartTime" and c.text
    ]
    ends = [
        _iso_date(c.text)
        for c in situation.iter()
        if _local(c.tag) == "overallEndTime" and c.text
    ]

    entry = {
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        # Ruimste venster: liever een waarschuwing te veel dan een gemiste omleiding.
        "start": min([s for s in starts if s], default=None),
        "end": max([e for e in ends if e], default=None),
        "cause": _first_text(situation, "causeDescription"),
        "authority": _first_text(situation, "sourceName"),
        "detour": _useful_detour(_first_text(situation, "reroutingItineraryDescription")),
        "lines": lines or None,
    }
    return {k: v for k, v in entry.items() if v is not None}


def _parse_feed(path: Path) -> list[dict]:
    """Stream de gzip-XML en houd alleen fietsrelevante situaties over."""
    works: list[dict] = []
    total = 0
    with gzip.open(path, "rb") as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if _local(element.tag) != "situation":
                continue
            total += 1
            try:
                entry = _parse_situation(element)
            except (TypeError, ValueError):
                entry = None
            if entry:
                works.append(entry)
            element.clear()
    logger.info(
        "NDW-feed verwerkt: %d situaties, %d relevant voor fietsers", total, len(works)
    )
    return works


def refresh_cache() -> list[dict]:
    """Download en verwerk de feed; schrijft de compacte cache."""
    settings = get_settings()
    settings.ensure_dirs()
    feed = _download()
    try:
        works = _parse_feed(feed)
    finally:
        feed.unlink(missing_ok=True)

    path = _cache_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(works), encoding="utf-8")
    tmp.replace(path)
    _remove_old_caches()
    logger.info(
        "Cache wegwerkzaamheden vernieuwd: %d meldingen, %d met afgesloten stuk",
        len(works),
        sum(1 for w in works if w.get("lines")),
    )
    return works


def get_road_works(force_refresh: bool = False) -> list[RoadWork]:
    """Alle fietsrelevante wegwerkzaamheden; ververst maximaal eens per TTL."""
    settings = get_settings()
    settings.ensure_dirs()
    path = _cache_file()

    with _LOCK:
        age = cache_age_seconds()
        fresh = age is not None and age < settings.roadworks_cache_ttl_seconds
        if force_refresh or not fresh:
            try:
                raw = refresh_cache()
            except Exception as exc:
                if not path.exists():
                    raise RuntimeError(
                        f"Kan de NDW-wegwerkzaamheden niet ophalen: {exc}"
                    ) from exc
                logger.warning(
                    "Verversen wegwerkzaamheden mislukt (%s); oude cache gebruikt", exc
                )
                raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = json.loads(path.read_text(encoding="utf-8"))

    return [RoadWork(**entry) for entry in raw]


def is_active_on(work: RoadWork, day: date) -> bool:
    """Geldt deze melding op de opgegeven dag?

    Meldingen zonder begin- of einddatum worden meegenomen: liever een
    waarschuwing te veel dan een gemiste omleiding.
    """
    iso = day.isoformat()
    if work.start and work.start > iso:
        return False
    if work.end and work.end < iso:
        return False
    return True


def load_road_works_near(
    route_coords: list[tuple[float, float]],
    margin_m: float,
    day: date | None = None,
) -> list[RoadWork]:
    """Wegwerkzaamheden binnen de bounding box van de route, actief op `day`.

    Een melding valt binnen het vak als het punt of een deel van het afgesloten
    stuk erin ligt: bij lange werkvakken staat het punt soms kilometers verderop.
    """
    day = day or datetime.now().date()
    min_lat, min_lon, max_lat, max_lon = bounding_box(route_coords, margin_m)

    def inside(lat: float, lon: float) -> bool:
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    return [
        work
        for work in get_road_works()
        if is_active_on(work, day)
        and (
            inside(work.lat, work.lon)
            or any(inside(lat, lon) for line in work.lines for lat, lon in line)
        )
    ]
