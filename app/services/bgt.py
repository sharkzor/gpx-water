"""BGT (Basisregistratie Grootschalige Topografie) via PDOK-vectortegels.

De BGT is de landelijke, op 20 cm nauwkeurige kaart van Nederland. Gemeenten,
provincies, waterschappen en het Rijk tekenen er als *bronhouder* elk wegdeel
in, met een `functie`: rijbaan, fietspad, voetpad, ruiterpad, … Natuurbeheerders
als Staatsbosbeheer zijn geen bronhouder; hun paden worden door de gemeente of
provincie ingetekend (en ontbreken in bossen vaak).

De BGT zegt dus wat een pad *is*, niet wie er mag komen: een verbod staat er
niet in. Daarom levert deze bron alleen "let op"-meldingen op, en vooral een
bevestiging van wat OpenStreetMap al zegt.

Ophalen gaat per vectortegel (zoomniveau 17, ~190 m breed, 0,1–0,2 s per
stuk). De `/items`-API van PDOK kan niet op functie filteren en kost in de stad
enkele seconden per 200 m; tegels zijn tien keer sneller en worden gecachet.
Per tegel bewaren we alleen de laag `wegdeel` (functie + polygonen), in
tegelcoördinaten: dat is een fractie van de volledige tegel.
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import requests

from app.config import get_settings
from app.services import mvt

logger = logging.getLogger(__name__)

SOURCE_NAME = "BGT"
ZOOM = 17
_CACHE_VERSION = 1


@dataclass(slots=True)
class Area:
    """Een wegdeel uit de BGT: functie plus polygoon (buitenring + gaten)."""

    functie: str
    exterior: list[tuple[float, float]]  # (lat, lon)
    holes: list[list[tuple[float, float]]]


# -- Tegelrekenwerk ----------------------------------------------------------


def tile_of(lat: float, lon: float, zoom: int = ZOOM) -> tuple[int, int]:
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n)
    return x, y


def _to_latlon(x: int, y: int, gx: float, gy: float, extent: int) -> tuple[float, float]:
    n = 2**ZOOM
    lon = (x + gx / extent) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + gy / extent) / n))))
    return lat, lon


def tiles_for(points: Iterable[tuple[float, float]], margin_m: float) -> set[tuple[int, int]]:
    """Alle tegels binnen `margin_m` van de punten."""
    tiles: set[tuple[int, int]] = set()
    for lat, lon in points:
        dlat = margin_m / 111_320.0
        dlon = dlat / max(0.2, math.cos(math.radians(lat)))
        for la in (lat - dlat, lat + dlat):
            for lo in (lon - dlon, lon + dlon):
                tiles.add(tile_of(la, lo))
        tiles.add(tile_of(lat, lon))
    return tiles


# -- Cache -------------------------------------------------------------------


def _cache_dir() -> Path:
    return get_settings().cache_dir / f"bgt_v{_CACHE_VERSION}"


def _cache_path(x: int, y: int) -> Path:
    return _cache_dir() / str(x // 256) / f"{x}_{y}.json"


def prune_cache() -> int:
    """Verwijder tegels ouder dan de TTL; geeft het aantal verwijderde terug."""
    cutoff = time.time() - get_settings().bgt_cache_ttl_seconds
    removed = 0
    root = _cache_dir()
    if not root.exists():
        return 0
    for path in root.glob("*/*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:  # pragma: no cover - best effort
            pass
    return removed


def _compact_tile(data: bytes) -> dict:
    """Alleen de wegdelen: [[functie, extent, [ring, ...]], ...] in tegelcoördinaten."""
    layers = mvt.decode(data, {"wegdeel"})
    layer = layers.get("wegdeel")
    if layer is None:
        return {"extent": 4096, "areas": []}
    areas = []
    for feature in layer.features:
        functie = feature.properties.get("functie")
        if feature.type != mvt.POLYGON or not isinstance(functie, str):
            continue
        rings = [[c for point in ring for c in point] for ring in feature.rings if len(ring) >= 4]
        if rings:
            areas.append([functie, rings])
    return {"extent": layer.extent, "areas": areas}


def _download(x: int, y: int) -> dict:
    settings = get_settings()
    url = settings.bgt_tiles_url.format(x=x, y=y, z=ZOOM)
    for attempt in range(2):
        response = requests.get(
            url, headers={"User-Agent": settings.user_agent}, timeout=20
        )
        if response.status_code in (204, 404):
            return {"extent": 4096, "areas": []}
        if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(1.0)
            continue
        response.raise_for_status()
        return _compact_tile(response.content)
    raise RuntimeError("PDOK gaf geen antwoord")  # pragma: no cover


def _load_tile(tile: tuple[int, int]) -> dict:
    x, y = tile
    path = _cache_path(x, y)
    ttl = get_settings().bgt_cache_ttl_seconds
    try:
        if time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    compact = _download(x, y)
    # Elke schrijver een eigen tijdelijk bestand: twee verzoeken over dezelfde
    # streek mogen elkaars tegel niet half overschrijven.
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(compact, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover - cache is een bijzaak
        logger.debug("BGT-tegel %s niet gecachet: %s", tile, exc)
        tmp.unlink(missing_ok=True)
    return compact


def _areas_of(tile: tuple[int, int], compact: dict) -> list[Area]:
    x, y = tile
    extent = int(compact.get("extent") or 4096)
    areas: list[Area] = []
    for functie, rings in compact.get("areas", []):
        # Volgens de MVT-specificatie begint elke buitenring met positieve
        # oppervlakte; de negatieve ringen erna zijn gaten.
        current: Area | None = None
        for flat in rings:
            ring = list(zip(flat[::2], flat[1::2]))
            coords = [_to_latlon(x, y, gx, gy, extent) for gx, gy in ring]
            if mvt.signed_area(ring) > 0 or current is None:
                current = Area(functie, coords, [])
                areas.append(current)
            else:
                current.holes.append(coords)
    return areas


def load_areas(
    tiles: Sequence[tuple[int, int]],
) -> tuple[list[Area], int]:
    """Wegdelen in de opgegeven tegels; geeft (wegdelen, aantal mislukte tegels)."""
    settings = get_settings()
    areas: list[Area] = []
    failed = 0

    def fetch(tile: tuple[int, int]) -> tuple[tuple[int, int], dict | None]:
        try:
            return tile, _load_tile(tile)
        except Exception as exc:  # noqa: BLE001 - één tegel mag de rest niet blokkeren
            logger.debug("BGT-tegel %s mislukt: %s", tile, exc)
            return tile, None

    with ThreadPoolExecutor(max_workers=max(1, settings.bgt_workers)) as pool:
        for tile, compact in pool.map(fetch, tiles):
            if compact is None:
                failed += 1
                continue
            areas.extend(_areas_of(tile, compact))
    return areas, failed
