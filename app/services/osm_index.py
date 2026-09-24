"""Lokale kopie van de Nederlandse wegenkaart uit OpenStreetMap.

Overgenomen uit routeboek (`backend/app/services/osm_index.py`). Aanvullingen
hier: bron-URL en verversingstermijn komen uit de config, en de automatische
opbouw wacht op voldoende vrij geheugen en start met een willekeurige
vertraging, omdat op dezelfde server meerdere installaties een eigen kaart
bouwen (zie `ensure_fresh_in_background()`).

Waarom lokaal en niet via Overpass
----------------------------------
De routecontrole (`services/legality.py`) haalde de wegen eerst per route op
bij de publieke Overpass API. Dat werkte functioneel, maar was in de praktijk
onbruikbaar: één controle kostte drie tot vijf minuten, waarvan vrijwel alles
wachten op die servers, en na een handvol controles blokkeerden alle drie de
publieke instances dit IP-adres. Dat is ook terecht — die dienst is niet
bedoeld om er routes mee te scannen.

Met een eigen kopie van de kaart is dezelfde controle een lokale
databasequery: milliseconden in plaats van minuten, altijd volledig, en
zonder iemand anders' server te belasten.

Hoe de kaart wordt opgebouwd
----------------------------
Eén keer per maand (zie `refresh()`):

1. Het Nederland-extract van Geofabrik downloaden (~1,4 GB, dagelijks vers).
2. `osmium tags-filter` houdt alleen de wegen over (1,4 GB -> ~180 MB).
3. `osmium export` maakt daar regel-per-regel GeoJSON van, mét geometrie.
4. Dit bestand wordt ingelezen in een SQLite-bestand met een R*Tree-index.

`osmium` doet het zware werk in C++; Python leest alleen het resultaat.
Stap 2 is de zwaarste: osmium houdt daar de node-verwijzingen van heel
Nederland in het geheugen, goed voor een piek van ongeveer 2,5 GB. Dat is de
enige echte systeemeis van deze module. Een schijfgebaseerde index is
geprobeerd en hielp niet: dan piekt osmium op dezelfde hoeveelheid én duurt
het vier keer zo lang.

Waarom SQLite
-------------
De R*Tree-module zit standaard in SQLite, dus dit vraagt geen extra
dependency, geen PostGIS en geen tweede database. Het resultaat is één
bestand dat je kunt kopiëren of meeverhuizen naar een andere server.

Waarom de tags worden bewaard en niet het oordeel
-------------------------------------------------
We slaan de OSM-tags op, niet de uitkomst van `classify()`. De regels over
wat wel en niet mag veranderen vaker dan de kaart zelf, en zo kost een
regelwijziging geen nieuwe download van 1,4 GB.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Alleen deze tags hebben we nodig om een weg te beoordelen; de rest (surface,
#: maxspeed, source, ...) gooien we weg. Dat scheelt ruwweg de helft aan
#: schijfruimte. Houd dit gelijk aan wat `legality.classify()` uitleest.
KEEP_TAGS = (
    "highway",
    "bicycle",
    "access",
    "vehicle",
    "foot",
    "footway",
    "motorroad",
    "service",
    "name",
    "designation",
)

#: Coördinaten worden opgeslagen als graden x 1e7 in een int32, precies zoals
#: OSM ze zelf bewaart. Dat is ruim 1 cm nauwkeurig en half zo groot als een
#: double.
COORD_SCALE = 10_000_000

#: Zoveel wegen per keer naar SQLite schrijven.
BATCH = 20_000


def _db_path() -> Path:
    return get_settings().data_dir / "osm" / "netherlands.sqlite"


def _work_dir() -> Path:
    return get_settings().data_dir / "osm" / "work"


# -- Opslagformaat -----------------------------------------------------------


def pack_coords(coords: Sequence[Sequence[float]]) -> bytes:
    """Zet [[lon, lat], ...] om naar een compacte blob."""
    flat: list[int] = []
    for lon, lat in coords:
        flat.append(int(round(lon * COORD_SCALE)))
        flat.append(int(round(lat * COORD_SCALE)))
    return struct.pack(f"<{len(flat)}i", *flat)


def unpack_coords(blob: bytes) -> list[tuple[float, float]]:
    """Lees een blob terug als [(lat, lon), ...].

    Let op de omgekeerde volgorde: OSM en GeoJSON werken met lon/lat, de rest
    van deze applicatie met lat/lon.
    """
    values = struct.unpack(f"<{len(blob) // 4}i", blob)
    return [
        (values[i + 1] / COORD_SCALE, values[i] / COORD_SCALE)
        for i in range(0, len(values), 2)
    ]


# -- Bouwen ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE way (
    id     INTEGER PRIMARY KEY,
    tags   TEXT NOT NULL,
    coords BLOB NOT NULL
);
CREATE VIRTUAL TABLE way_bbox USING rtree_i32(id, minx, maxx, miny, maxy);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _run(cmd: list[str]) -> None:
    """Draai een extern commando en laat de foutuitvoer niet verdwijnen."""
    logger.info("osm-index: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} mislukt: "
            + (tail[-1] if tail else f"exitcode {result.returncode}")
        )


def _download(target: Path, progress: Callable[[str, float], None]) -> None:
    """Haal het Nederland-extract op, met voortgang per procent."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    # Geofabrik ververst dagelijks; `-latest` is een redirect, die volgt requests.
    with requests.get(get_settings().osm_pbf_url, stream=True, timeout=120) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        done = 0
        last = 0.0
        with part.open("wb") as handle:
            for block in response.iter_content(chunk_size=1 << 20):
                handle.write(block)
                done += len(block)
                if total and time.monotonic() - last > 2:
                    last = time.monotonic()
                    progress("Kaartgegevens downloaden", done / total)
    part.replace(target)


def _features(path: Path) -> Iterator[tuple[int, str, bytes, tuple[int, ...]]]:
    """Lees het GeoJSON-bestand van osmium regel voor regel.

    Elke regel is één weg. Wegen zonder bruikbare geometrie of zonder
    `highway`-tag slaan we over.
    """
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            # osmium mag een record-separator (RS) voor de regel zetten.
            line = line.lstrip("\x1e").strip()
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError:
                continue
            props = feature.get("properties") or {}
            if "highway" not in props:
                continue
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            # osmium zet het OSM-id als `@id` in de eigenschappen (`-a id`).
            way_id = props.get("@id")
            if not isinstance(way_id, int):
                continue

            tags = {k: v for k, v in props.items() if k in KEEP_TAGS}
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            bbox = (
                int(min(lons) * COORD_SCALE),
                int(max(lons) * COORD_SCALE) + 1,
                int(min(lats) * COORD_SCALE),
                int(max(lats) * COORD_SCALE) + 1,
            )
            yield way_id, json.dumps(tags, separators=(",", ":")), pack_coords(
                coords
            ), bbox


def _build_sqlite(
    geojson: Path, db_path: Path, progress: Callable[[str, float], None]
) -> int:
    """Zet het GeoJSON-bestand om in een SQLite-bestand met R*Tree."""
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        # Duurzaamheid is hier niet interessant: bij een fout bouwen we het
        # bestand gewoon opnieuw, en de oude kaart blijft tot het eind staan.
        connection.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA
        )
        rows: list[tuple[int, str, bytes]] = []
        boxes: list[tuple[int, int, int, int, int]] = []
        total = 0
        for way_id, tags, coords, bbox in _features(geojson):
            rows.append((way_id, tags, coords))
            boxes.append((way_id, *bbox))
            if len(rows) >= BATCH:
                _flush(connection, rows, boxes)
                total += len(rows)
                rows.clear()
                boxes.clear()
                progress(f"Wegen indexeren ({total:,} gevonden)".replace(",", "."), -1)
        if rows:
            _flush(connection, rows, boxes)
            total += len(rows)
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('way_count', ?)", (str(total),)
        )
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('built_at', ?)",
            (str(int(time.time())),),
        )
        connection.commit()
        return total
    finally:
        connection.close()


def _flush(
    connection: sqlite3.Connection,
    rows: list[tuple[int, str, bytes]],
    boxes: list[tuple[int, int, int, int, int]],
) -> None:
    connection.executemany("INSERT OR REPLACE INTO way VALUES (?, ?, ?)", rows)
    connection.executemany("INSERT OR REPLACE INTO way_bbox VALUES (?, ?, ?, ?, ?)", boxes)


def refresh(progress: Callable[[str, float], None] | None = None) -> dict[str, object]:
    """Bouw de lokale kaart opnieuw op.

    De nieuwe kaart wordt eerst volledig opgebouwd en pas op het laatste
    moment op zijn plaats gezet. Gaat er onderweg iets mis, dan blijft de
    bestaande kaart gewoon in gebruik.
    """
    report: Callable[[str, float], None] = progress or (lambda _m, _p: None)
    work = _work_dir()
    work.mkdir(parents=True, exist_ok=True)
    pbf = work / "netherlands.osm.pbf"
    highways = work / "highways.osm.pbf"
    geojson = work / "highways.geojsonseq"
    staging = work / "netherlands.sqlite"
    started = time.monotonic()

    try:
        report("Kaartgegevens downloaden", 0.0)
        _download(pbf, report)

        report("Wegen uit de kaart filteren", -1)
        _run(
            [
                "osmium", "tags-filter", str(pbf), "w/highway",
                "-o", str(highways), "--overwrite",
            ]
        )

        report("Geometrie uitpakken", -1)
        _run(
            [
                "osmium", "export", str(highways),
                "-f", "geojsonseq",
                "--geometry-types=linestring",
                "-a", "id",
                "-i", "sparse_file_array",
                "-o", str(geojson), "--overwrite",
            ]
        )

        report("Wegen indexeren", -1)
        count = _build_sqlite(geojson, staging, report)
        if count == 0:
            raise RuntimeError("geen wegen gevonden in de kaartgegevens")

        destination = _db_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Binnen dezelfde map, dus dit is een atomaire vervanging.
        os.replace(staging, destination)
        _reader_cache.clear()

        size_mb = destination.stat().st_size / (1 << 20)
        logger.info(
            "osm-index: %d wegen, %.0f MB, %.0f s", count, size_mb, time.monotonic() - started
        )
        return {
            "way_count": count,
            "size_mb": round(size_mb, 1),
            "seconds": round(time.monotonic() - started),
        }
    finally:
        # De tussenbestanden zijn samen ruim 2 GB; die willen we niet houden.
        shutil.rmtree(work, ignore_errors=True)


# -- Lezen -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexStatus:
    available: bool
    way_count: int = 0
    built_at: float | None = None
    size_mb: float = 0.0

    @property
    def age_days(self) -> float | None:
        if self.built_at is None:
            return None
        return (time.time() - self.built_at) / 86400

    @property
    def stale(self) -> bool:
        age = self.age_days
        return age is not None and age > get_settings().osm_max_age_days


def status() -> IndexStatus:
    path = _db_path()
    if not path.exists():
        return IndexStatus(available=False)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        finally:
            connection.close()
    except sqlite3.Error:
        return IndexStatus(available=False)
    return IndexStatus(
        available=True,
        way_count=int(meta.get("way_count", 0)),
        built_at=float(meta["built_at"]) if "built_at" in meta else None,
        size_mb=round(path.stat().st_size / (1 << 20), 1),
    )


#: Verbindingen zijn per thread; SQLite staat delen tussen threads niet toe.
_reader_cache: dict[int, sqlite3.Connection] = {}


def _connection() -> sqlite3.Connection:
    key = threading.get_ident()
    existing = _reader_cache.get(key)
    if existing is not None:
        return existing
    path = _db_path()
    if not path.exists():
        raise FileNotFoundError("de lokale kaart is nog niet opgebouwd")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    _reader_cache[key] = connection
    return connection


def ways_in_bbox(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> list[tuple[int, dict[str, str], list[tuple[float, float]]]]:
    """Alle wegen waarvan de omhullende rechthoek dit vak raakt."""
    connection = _connection()
    rows = connection.execute(
        """
        SELECT w.id, w.tags, w.coords
        FROM way_bbox b JOIN way w ON w.id = b.id
        WHERE b.maxx >= ? AND b.minx <= ? AND b.maxy >= ? AND b.miny <= ?
        """,
        (
            int(min_lon * COORD_SCALE),
            int(max_lon * COORD_SCALE),
            int(min_lat * COORD_SCALE),
            int(max_lat * COORD_SCALE),
        ),
    ).fetchall()
    return [(row[0], json.loads(row[1]), unpack_coords(row[2])) for row in rows]


# -- Verversen op de achtergrond ---------------------------------------------

# Het opbouwen duurt enkele minuten en piekt rond 2,5 GB geheugen, dus draait
# het nooit binnen een HTTP-verzoek en nooit twee keer tegelijk.


@dataclass
class RefreshJob:
    state: str = "running"  # running | done | error
    message: str = "Bezig met voorbereiden"
    progress: float = -1.0  # -1 betekent: duur onbekend
    started_at: float = 0.0
    finished_at: float | None = None
    error: str | None = None
    result: dict[str, object] | None = None


_job_lock = threading.Lock()
_job: RefreshJob | None = None


def current_job() -> RefreshJob | None:
    return _job


def start_refresh() -> RefreshJob:
    """Begin met verversen, of geef de lopende taak terug."""
    global _job
    with _job_lock:
        if _job is not None and _job.state == "running":
            return _job
        job = RefreshJob(started_at=time.time())
        _job = job

    def run() -> None:
        def progress(message: str, value: float) -> None:
            job.message = message
            job.progress = value

        try:
            job.result = refresh(progress)
            job.state = "done"
            job.message = "De kaart is bijgewerkt"
            job.progress = 1.0
        except Exception as exc:  # noqa: BLE001 - alles moet in de status landen
            logger.exception("osm-index: verversen mislukt")
            job.state = "error"
            job.error = str(exc)
            job.message = "Het bijwerken is mislukt"
        finally:
            job.finished_at = time.time()

    threading.Thread(target=run, name="osm-refresh", daemon=True).start()
    return job


def free_memory_mb() -> int | None:
    """Beschikbaar geheugen volgens de kernel, of None als dat onbekend is."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def refresh_if_needed() -> str:
    """Start het opbouwen als de kaart ontbreekt of verouderd is.

    Geeft terug wat er gebeurde: "fresh", "started", "running" of "low_memory".
    """
    job = current_job()
    if job is not None and job.state == "running":
        return "running"
    state = status()
    if state.available and not state.stale:
        return "fresh"
    needed = get_settings().osm_min_free_mb
    free = free_memory_mb()
    if free is not None and free < needed:
        logger.warning(
            "osm-index: kaart %s, maar slechts %d MB vrij (%d nodig); later opnieuw",
            "ontbreekt" if not state.available else "is verouderd",
            free,
            needed,
        )
        return "low_memory"
    logger.info(
        "osm-index: kaart %s, verversen",
        "ontbreekt" if not state.available else "is verouderd",
    )
    start_refresh()
    return "started"


def ensure_fresh_in_background() -> None:
    """Ververs zodra de kaart ontbreekt of ouder is dan `OSM_MAX_AGE_DAYS`.

    Anders dan in routeboek wordt er elk uur gekeken in plaats van dagelijks,
    zodat een opbouw die wegens geheugengebrek is uitgesteld niet een hele dag
    blijft liggen. De eerste controle wacht een willekeurige tijd, zodat twee
    installaties die tegelijk starten niet samen 2 x 2,5 GB gaan gebruiken. De
    controle zelf blijft gewoon werken met de oude kaart terwijl dit loopt.
    """
    # Restanten van een afgebroken opbouw (container gestopt) zijn ruim 2 GB.
    shutil.rmtree(_work_dir(), ignore_errors=True)
    jitter = max(0, get_settings().osm_start_jitter_seconds)

    def loop() -> None:
        time.sleep(random.uniform(0, jitter) if jitter else 0)
        while True:
            try:
                refresh_if_needed()
            except Exception:  # noqa: BLE001
                logger.exception("osm-index: automatische controle mislukt")
            time.sleep(3600)

    threading.Thread(target=loop, name="osm-refresh-scheduler", daemon=True).start()
