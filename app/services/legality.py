"""Routes controleren op paden waar fietsen niet mag (alleen Nederland).

Overgenomen uit routeboek (`backend/app/services/legality.py`): regels,
bemonstering en het samenvoegen van meldingen zijn gelijk. In deze app draait
de controle binnen de gewone GPX-verwerking (1 a 2 seconden per 100 km), dus
de achtergrondtaken en de rapportcache van routeboek zijn weggelaten.

De bron is OpenStreetMap. Voor Nederland is die zeer nauwkeurig getagd op
toegankelijkheid (`bicycle`, `access`, `highway`), en het is de enige gratis
bron zonder API-sleutel die dit landsdekkend biedt.

De wegen komen uit een lokale kopie van de kaart (`services/osm_index.py`),
niet uit de Overpass API. Die eerste opzet is geprobeerd en weer verlaten:
één controle kostte drie tot vijf minuten — vrijwel volledig wachttijd — en
na een handvol controles blokkeerden alle drie de publieke Overpass-servers
dit IP-adres. Terecht ook: zo'n gedeelde gratis dienst is niet bedoeld om
routes mee te scannen. Voer die aanpak dus niet opnieuw in.

Met de lokale kaart is het ophalen een R*Tree-query van enkele milliseconden
per stuk route. De hele controle is daarmee sneller dan één Overpass-verzoek
vroeger was, en het antwoord is altijd volledig.

Hoe de controle werkt
---------------------
1. De route wordt om de ~20 m bemonsterd.
2. Per stuk route halen we alle wegen op waarvan de omhullende rechthoek in
   de buurt ligt, en splitsen die in "hier mag je niet fietsen" en de rest.
3. Elk monster dat op een verboden weg ligt én *geen* toegestane weg vlakbij
   heeft, wordt gemarkeerd. Die tweede voorwaarde is de belangrijkste rem op
   valse meldingen: een route over gewone wegen heeft altijd zo'n weg
   vlakbij, terwijl een echt verboden pad op zichzelf staat.
4. Opeenvolgende markeringen worden samengevoegd tot segmenten; te korte
   segmenten vallen af als ruis.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from app.services import osm_index
from app.services.geo import LocalProjection, haversine_m

logger = logging.getLogger(__name__)

# -- Afstemming --------------------------------------------------------------

#: Zoekvakken van deze grootte langs de route. Klein genoeg om de route
#: strak te volgen, groot genoeg om niet onnodig veel query's te doen.
MAX_BOX_KM = 3.0

#: Marge rond elk zoekvak, ruim boven SNAP_RADIUS_M en ALLOWED_NEARBY_M.
BOX_MARGIN_M = 60.0

#: Om de hoeveel meter we een punt op de route beoordelen.
SAMPLE_SPACING_M = 20.0

#: Maximale afstand waarop een route nog "op" een weg ligt.
SNAP_RADIUS_M = 12.0

#: Ligt er binnen zoveel meter óók een gewone, toegestane weg, dan melden we
#: niets. Een route over legale wegen heeft altijd zo'n weg vlakbij; alleen een
#: echt verboden pad staat op zichzelf. Dit is de belangrijkste rem op valse
#: meldingen: opgeslagen routepunten liggen soms 100 m uit elkaar, en de rechte
#: lijn daartussen snijdt bochten af tot vlak langs een parallel voetpad.
ALLOWED_NEARBY_M = 20.0

#: Een melding moet minstens zo lang zijn; korter is vrijwel altijd ruis
#: doordat de GPS-lijn even naar een parallel pad "overspringt".
MIN_SEGMENT_M = 35.0
MIN_SEGMENT_POINTS = 3

#: Zoveel schone monsters mogen een segment onderbreken zonder het te splitsen.
GAP_TOLERANCE = 2

#: Twee meldingen met dezelfde reden die minder dan zoveel meter uit elkaar
#: liggen, worden tot één melding samengevoegd.
#:
#: Dit is nodig omdat `ALLOWED_NEARBY_M` een doorlopende overtreding aan
#: flarden schiet: langs een dijk of polderweg ligt om de paar honderd meter
#: een inrit (`service`), een landbouwpad (`track`) of een kort stuk dat in OSM
#: net anders getagd is. Elk daarvan onderdrukt een handvol monsters, waardoor
#: één verboden dijk van 8 km als zeven losse meldingen in de lijst kwam. Zie
#: `_merge_runs()`.
#:
#: 250 m is ruim genoeg voor inritten en kruisingen, maar klein genoeg om twee
#: echt losse overtredingen niet aan elkaar te plakken.
MERGE_GAP_M = 250.0

# -- Regels ------------------------------------------------------------------

#: Waarden van `bicycle` waarmee fietsen expliciet is toegestaan.
_BICYCLE_OK = frozenset(
    {"yes", "designated", "permissive", "official", "destination", "dismount"}
)

_ACCESS_BLOCKED = frozenset({"no", "private"})


def classify(tags: dict[str, str]) -> tuple[str, str, str] | None:
    """Beoordeel een OSM-weg: (severity, code, label) of None als fietsen mag.

    `severity` is "forbidden" (fietsen mag hier niet) of "warning" (mag wel,
    maar niet zonder meer: afstappen, gedoogd, of juridisch onduidelijk).
    """
    highway = tags.get("highway")
    bicycle = tags.get("bicycle")

    # Stoepen en zebrapaden liggen per definitie tegen de rijbaan aan. Ze als
    # overtreding melden zou elke route door de bebouwde kom rood kleuren.
    if highway == "footway" and tags.get("footway") in ("sidewalk", "crossing"):
        return None

    # 1. Expliciete uitspraak over fietsers gaat altijd voor.
    if bicycle == "no":
        return ("forbidden", "bicycle_no", "Fietsen verboden")
    if bicycle == "dismount":
        return ("warning", "dismount", "Afstappen verplicht")
    if bicycle == "use_sidepath":
        return ("warning", "use_sidepath", "Verplicht fietspad ernaast")

    # 2. Autosnelweg en autoweg zijn nooit toegestaan, ook niet met bicycle=yes.
    if highway in ("motorway", "motorway_link") or tags.get("motorroad") == "yes":
        return ("forbidden", "motorway", "Autosnelweg of autoweg")

    explicitly_allowed = bicycle in _BICYCLE_OK

    # 3. Afgesloten terrein, tenzij er voor fietsers een uitzondering staat.
    if not explicitly_allowed:
        if tags.get("access") in _ACCESS_BLOCKED:
            return ("forbidden", "access_private", "Privéterrein of afgesloten")
        if tags.get("vehicle") in _ACCESS_BLOCKED:
            return ("forbidden", "vehicle_no", "Geen voertuigen toegestaan")

    if explicitly_allowed:
        return None

    # 4. Voetgangersinfrastructuur zonder fietsvrijgave.
    if highway == "steps":
        return ("forbidden", "steps", "Trap")
    if highway == "footway":
        return ("forbidden", "footway", "Voetpad")
    if highway == "corridor":
        return ("forbidden", "corridor", "Gang door een gebouw")
    if highway == "pedestrian":
        return ("warning", "pedestrian", "Voetgangersgebied")
    if highway == "bridleway":
        return ("warning", "bridleway", "Ruiterpad")
    if highway == "path":
        # `highway=path` is in Nederland dubbelzinnig. Alleen als het pad
        # nadrukkelijk voor voetgangers is bedoeld melden we het.
        if tags.get("foot") == "designated":
            return ("warning", "path_foot", "Wandelpad")
        return None
    return None


# -- Datastructuren ----------------------------------------------------------


@dataclass(slots=True)
class Way:
    """Een OSM-weg met alleen de tags en geometrie die wij nodig hebben."""

    id: int
    tags: dict[str, str]
    coords: list[tuple[float, float]]


@dataclass(slots=True)
class Segment:
    """Een aaneengesloten stuk route dat een probleem oplevert."""

    severity: str
    code: str
    label: str
    way_id: int | None
    way_name: str | None
    highway: str | None
    start_km: float
    end_km: float
    length_m: float
    coordinates: list[tuple[float, float]]


@dataclass(slots=True)
class Report:
    total_distance_km: float
    forbidden_count: int
    warning_count: int
    segments: list[Segment]
    checked_at: float = field(default_factory=time.time)
    source: str = "OpenStreetMap"


# -- Wegen ophalen -----------------------------------------------------------


def _boxes(
    points: Sequence[tuple[float, float]],
) -> list[tuple[float, float, float, float]]:
    """Deel de route op in zoekvakken.

    Eén vak om de hele route is verleidelijk maar verkeerd: bij een route van
    40 km beslaat dat al gauw 15 bij 15 km met tienduizenden wegen, terwijl we
    er maar een fractie van nodig hebben. Vakken van een paar kilometer volgen
    de route veel strakker en zijn samen een stuk sneller.
    """
    boxes: list[tuple[float, float, float, float]] = []
    span = MAX_BOX_KM * 1000.0
    current: list[tuple[float, float]] = []
    min_lat = min_lon = 90.0
    max_lat = max_lon = -90.0

    def flush() -> None:
        if len(current) < 1:
            return
        # Marge in graden: ruim boven de zoekstralen hieronder, zodat een weg
        # die net buiten het vak begint toch meekomt.
        dlat = BOX_MARGIN_M / 111_320.0
        dlon = dlat / max(0.2, abs(math.cos(math.radians((min_lat + max_lat) / 2))))
        boxes.append((min_lat - dlat, min_lon - dlon, max_lat + dlat, max_lon + dlon))

    for lat, lon in points:
        current.append((lat, lon))
        min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
        min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
        height = (max_lat - min_lat) * 111_320.0
        width = (max_lon - min_lon) * 111_320.0 * abs(math.cos(math.radians(lat)))
        if height > span or width > span:
            flush()
            current = [(lat, lon)]
            min_lat = max_lat = lat
            min_lon = max_lon = lon
    flush()
    return boxes


def load_ways(points: Sequence[tuple[float, float]]) -> list[Way]:
    """Alle wegen langs de route, uit de lokale kaart."""
    found: dict[int, Way] = {}
    for min_lat, min_lon, max_lat, max_lon in _boxes(points):
        for way_id, tags, coords in osm_index.ways_in_bbox(
            min_lat, min_lon, max_lat, max_lon
        ):
            if way_id not in found:
                found[way_id] = Way(id=way_id, tags=tags, coords=coords)
    return list(found.values())


def sample_route(
    points: Sequence[tuple[float, float]],
) -> list[tuple[float, float, float]]:
    """Verdeel de route in punten van ~SAMPLE_SPACING_M met hun km-positie."""
    if len(points) < 2:
        return [(points[0][0], points[0][1], 0.0)] if points else []
    samples: list[tuple[float, float, float]] = [(points[0][0], points[0][1], 0.0)]
    travelled = 0.0
    carry = 0.0
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        span = haversine_m(a[0], a[1], b[0], b[1])
        if span <= 0:
            continue
        offset = SAMPLE_SPACING_M - carry
        while offset < span:
            f = offset / span
            samples.append(
                (
                    a[0] + (b[0] - a[0]) * f,
                    a[1] + (b[1] - a[1]) * f,
                    (travelled + offset) / 1000.0,
                )
            )
            offset += SAMPLE_SPACING_M
        carry = (carry + span) % SAMPLE_SPACING_M
        travelled += span
    return samples


class _WayIndex:
    """Ruimtelijke index van wegen, in meters rond een referentiepunt."""

    def __init__(self, ways: Sequence[Way], projection: LocalProjection) -> None:
        self.ways: list[Way] = []
        lines: list[LineString] = []
        for way in ways:
            xy = projection.to_xy_many(way.coords)
            if len(xy) < 2:
                continue
            lines.append(LineString(xy))
            self.ways.append(way)
        self.lines = lines
        self.tree = STRtree(lines) if lines else None

    def nearest_within(
        self, x: float, y: float, radius: float
    ) -> list[tuple[float, Way]]:
        """Wegen binnen `radius`, als (afstand, weg), dichtstbijzijnde eerst."""
        if self.tree is None:
            return []
        point = Point(x, y)
        found: list[tuple[float, Way]] = []
        for index in self.tree.query(point.buffer(radius)):
            distance = self.lines[index].distance(point)
            if distance <= radius:
                found.append((distance, self.ways[index]))
        found.sort(key=lambda item: item[0])
        return found


# -- Controle ----------------------------------------------------------------


def _runs(
    flagged: dict[int, tuple[float, Way, tuple[str, str, str]]],
    samples: Sequence[tuple[float, float, float]],
) -> list[list[int]]:
    """Groepeer opeenvolgende gemarkeerde monsters tot reeksen van betekenis."""
    if not flagged:
        return []
    runs: list[list[int]] = []
    current: list[int] = []
    previous: int | None = None
    for index in sorted(flagged):
        if previous is not None and index - previous > GAP_TOLERANCE + 1:
            runs.append(current)
            current = []
        current.append(index)
        previous = index
    runs.append(current)

    keep: list[list[int]] = []
    for run in runs:
        if len(run) < MIN_SEGMENT_POINTS:
            continue
        length = (samples[run[-1]][2] - samples[run[0]][2]) * 1000.0
        if length >= MIN_SEGMENT_M:
            keep.append(run)
    return keep


def _verdict_of(
    run: Sequence[int],
    flagged: dict[int, tuple[float, Way, tuple[str, str, str]]],
) -> tuple[float, Way, tuple[str, str, str]]:
    """De zwaarste reden binnen een reeks; verboden weegt zwaarder dan let-op.

    Slaat niet-gemarkeerde monsters over: na `_merge_runs()` bevat een reeks ook
    de schone tussenstukjes.
    """
    return max(
        (flagged[i] for i in run if i in flagged),
        key=lambda item: (item[2][0] == "forbidden", -item[0]),
    )


def _merge_runs(
    runs: list[list[int]],
    flagged: dict[int, tuple[float, Way, tuple[str, str, str]]],
    samples: Sequence[tuple[float, float, float]],
) -> list[list[int]]:
    """Plak reeksen met dezelfde reden aan elkaar als ze vlak bij elkaar liggen.

    Zonder deze stap valt één doorlopende overtreding uiteen in losse meldingen,
    doordat elke inrit of zijweg langs de route een paar monsters onderdrukt
    (zie `MERGE_GAP_M`). Het gat tussen twee reeksen wordt mee opgenomen in de
    melding: dat stukje ligt op dezelfde weg, dus de lijn op de kaart blijft de
    route netjes volgen in plaats van los te breken in fragmenten.
    """
    if len(runs) < 2:
        return runs

    merged: list[list[int]] = [runs[0]]
    for run in runs[1:]:
        previous = merged[-1]
        gap_m = (samples[run[0]][2] - samples[previous[-1]][2]) * 1000.0
        _, _, (severity, code, _) = _verdict_of(previous, flagged)
        _, _, (next_severity, next_code, _) = _verdict_of(run, flagged)
        if gap_m <= MERGE_GAP_M and (severity, code) == (next_severity, next_code):
            # De monsters in het gat zijn zelf niet gemarkeerd, maar horen wel
            # bij het getekende stuk; vandaar het volledige bereik.
            merged[-1] = list(range(previous[0], run[-1] + 1))
        else:
            merged.append(run)
    return merged


def _way_label(
    run: Sequence[int],
    flagged: dict[int, tuple[float, Way, tuple[str, str, str]]],
) -> str | None:
    """Alle straatnamen binnen een melding, in volgorde van voorkomen.

    Na het samenvoegen beslaat een melding vaak meerdere OSM-ways — een lange
    dijk is daar zelden één object. "Lekdijk, Rijndijk" is dan informatiever
    dan alleen de naam van het toevallig eerste stuk.
    """
    names: list[str] = []
    for index in run:
        entry = flagged.get(index)
        if entry is None:
            continue
        name = entry[1].tags.get("name")
        if name and name not in names:
            names.append(name)
    if not names:
        return None
    if len(names) > 3:
        return f"{', '.join(names[:3])} e.a."
    return ", ".join(names)


def _to_segments(
    flagged: dict[int, tuple[float, Way, tuple[str, str, str]]],
    samples: Sequence[tuple[float, float, float]],
) -> list[Segment]:
    segments: list[Segment] = []
    for run in _merge_runs(_runs(flagged, samples), flagged, samples):
        first, last = run[0], run[-1]
        coords = [(samples[i][0], samples[i][1]) for i in range(first, last + 1)]
        length = sum(
            haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
            for i in range(len(coords) - 1)
        )
        # De zwaarste reden binnen het segment bepaalt het oordeel.
        _, way, (severity, code, label) = _verdict_of(run, flagged)
        segments.append(
            Segment(
                severity=severity,
                code=code,
                label=label,
                way_id=way.id,
                way_name=_way_label(run, flagged),
                highway=way.tags.get("highway"),
                start_km=round(samples[first][2], 2),
                end_km=round(samples[last][2], 2),
                length_m=round(length, 1),
                coordinates=coords,
            )
        )
    segments.sort(key=lambda s: s.start_km)
    return segments


def check_route(
    points: Sequence[tuple[float, float]],
    progress: Callable[[float, str], None] | None = None,
) -> Report:
    """Controleer een route op stukken waar fietsen niet (zonder meer) mag."""

    def report_progress(value: float, message: str) -> None:
        if progress is not None:
            progress(min(0.99, value), message)

    samples = sample_route(points)
    total_km = samples[-1][2] if samples else 0.0
    if len(samples) < 2:
        return Report(total_km, 0, 0, [])

    projection = LocalProjection.from_points(points)
    report_progress(0.05, "Kaartgegevens opzoeken")

    ways = load_ways(points)
    problematic: list[Way] = []
    allowed: list[Way] = []
    for way in ways:
        (problematic if classify(way.tags) is not None else allowed).append(way)

    report_progress(0.88, "Route langs de kaart leggen")

    problem_index = _WayIndex(problematic, projection)
    allowed_index = _WayIndex(allowed, projection)

    flagged: dict[int, tuple[float, Way, tuple[str, str, str]]] = {}
    for i, (lat, lon, _km) in enumerate(samples):
        x, y = projection.to_xy(lat, lon)
        near = problem_index.nearest_within(x, y, SNAP_RADIUS_M)
        if not near:
            continue
        distance, way = near[0]
        if allowed_index.nearest_within(x, y, ALLOWED_NEARBY_M):
            continue
        verdict = classify(way.tags)
        if verdict is not None:
            flagged[i] = (distance, way, verdict)

    report_progress(0.95, "Meldingen samenvoegen")
    segments = _to_segments(flagged, samples)
    return Report(
        total_distance_km=round(total_km, 2),
        forbidden_count=sum(1 for s in segments if s.severity == "forbidden"),
        warning_count=sum(1 for s in segments if s.severity == "warning"),
        segments=segments,
    )
