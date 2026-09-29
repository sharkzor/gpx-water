"""Aanvullende bronnen voor de controle op verboden paden: NDW en BGT.

Beide leveren `legality.Segment`s die `legality.combine()` samenvoegt met de
OSM-meldingen.

**NDW-verkeersborden.** Een bord geldt voor het verkeer dat erlangs rijdt in
de richting van zijn `bearing`, vanaf het bord. Een bord telt dus alleen als:

1. de route binnen `SIGN_RADIUS_M` langs het bord komt, in (ongeveer) de
   richting waarvoor het bord geldt;
2. het bord bij een OSM-weg hoort die parallel loopt aan die richting — het
   bord staat bij de ingang van dát pad, niet van de weg ernaast;
3. de route daarna echt over dat pad gaat (minstens `SIGN_MIN_M`).

Zo levert een fietspad langs een weg met een C14-bord op de parallelle
rijbaan geen melding op, en een bord bij een zijpad dat je voorbijrijdt ook
niet.

**BGT.** Een routepunt wordt gemeld als het op een BGT-voetpad, -trap,
-voetgangersgebied of -ruiterpad ligt en er in de buurt geen rijbaan,
fietspad, erf of inrit is. De BGT zegt niets over toegang, dus dit is altijd
"let op".
"""

from __future__ import annotations

import logging
import math
from typing import Sequence

from shapely.geometry import LineString, Point, Polygon
from shapely.strtree import STRtree

from app.config import get_settings
from app.services import bgt, ndw_signs
from app.services.geo import LocalProjection, in_netherlands
from app.services.legality import (
    _BICYCLE_OK,
    SOURCES,
    Segment,
    Way,
    _WayIndex,
    _to_segments,
    combine,
)

logger = logging.getLogger(__name__)

Samples = Sequence[tuple[float, float, float]]


def osm_allows_bikes(tags: dict[str, str]) -> bool:
    """Zegt OpenStreetMap uitdrukkelijk dat fietsen hier mag?"""
    return tags.get("highway") == "cycleway" or tags.get("bicycle") in _BICYCLE_OK - {"dismount"}

# -- NDW ---------------------------------------------------------------------

#: Maximale afstand tussen bord en route.
SIGN_RADIUS_M = 15.0
#: Maximale afwijking tussen rijrichting en de richting van het bord.
SIGN_HEADING_TOLERANCE = 50.0
#: Maximale afstand tussen bord en de OSM-weg waar het bij hoort.
SIGN_WAY_RADIUS_M = 12.0
#: Maximale hoek tussen die weg en de richting van het bord (beide kanten op).
SIGN_WAY_TOLERANCE = 40.0
#: De route moet zo dicht bij de weg van het bord blijven.
FOLLOW_RADIUS_M = 12.0
#: Minimale lengte die de route over de weg van het bord gaat.
SIGN_MIN_M = 40.0
#: Verder dan dit volgen we een bord niet (een kruising heft het meestal op).
SIGN_MAX_M = 2000.0


def _angle_diff(a: float, b: float) -> float:
    """Kleinste verschil tussen twee richtingen in graden (0..180)."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _heading(ax: float, ay: float, bx: float, by: float) -> float:
    """Kompasrichting van a naar b (0 = noord, met de klok mee)."""
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360.0


def _route_headings(xy: Sequence[tuple[float, float]]) -> list[float | None]:
    headings: list[float | None] = []
    for i in range(len(xy)):
        a = xy[max(0, i - 1)]
        b = xy[min(len(xy) - 1, i + 1)]
        headings.append(None if a == b else _heading(a[0], a[1], b[0], b[1]))
    return headings


def _line_heading(line: LineString, point: Point) -> float | None:
    """Richting van een lijn bij het punt dat het dichtst bij `point` ligt."""
    if line.length <= 0:
        return None
    pos = line.project(point)
    a = line.interpolate(max(0.0, pos - 3.0))
    b = line.interpolate(min(line.length, pos + 3.0))
    if a.equals(b):
        return None
    return _heading(a.x, a.y, b.x, b.y)


def _sign_dict(sign: ndw_signs.Sign) -> dict:
    return {
        "code": sign.code,
        "text": "; ".join(t for t in sign.texts if t) or None,
        "image_url": sign.image_url,
        "last_seen": sign.last_seen,
        "lat": sign.lat,
        "lon": sign.lon,
        "road": sign.road,
    }


def detect_signs(
    samples: Samples,
    projection: LocalProjection,
    ways: Sequence[Way],
) -> list[Segment]:
    """Meldingen op grond van NDW-verkeersborden langs de route."""
    if len(samples) < 2 or not ways:
        return []
    lats = [s[0] for s in samples]
    lons = [s[1] for s in samples]
    margin = 0.0005
    signs = [
        s
        for s in ndw_signs.signs_in_bbox(
            min(lats) - margin, min(lons) - margin, max(lats) + margin, max(lons) + margin
        )
        if s.bearing is not None and ndw_signs.interpret(s) is not None
    ]
    if not signs:
        return []

    xy = projection.to_xy_many((s[0], s[1]) for s in samples)
    headings = _route_headings(xy)
    route_tree = STRtree([Point(p) for p in xy])
    way_index = _WayIndex(ways, projection)
    lines = {id(w): line for w, line in zip(way_index.ways, way_index.lines)}

    found: list[Segment] = []
    for sign in signs:
        verdict = ndw_signs.interpret(sign)
        assert verdict is not None and sign.bearing is not None
        bearing = float(sign.bearing) % 360.0
        sx, sy = projection.to_xy(sign.lat, sign.lon)
        spoint = Point(sx, sy)

        # 2. De weg waar het bord bij hoort: dichtbij en in dezelfde richting.
        sign_way: Way | None = None
        for _distance, way in way_index.nearest_within(sx, sy, SIGN_WAY_RADIUS_M):
            heading = _line_heading(lines[id(way)], spoint)
            if heading is None:
                continue
            if min(_angle_diff(heading, bearing), _angle_diff(heading, bearing + 180)) <= SIGN_WAY_TOLERANCE:
                sign_way = way
                break
        if sign_way is None:
            continue
        way_line = lines[id(sign_way)]
        if osm_allows_bikes(sign_way.tags):
            # Bord en kaart spreken elkaar tegen: het bord kan verouderd zijn of
            # bij een stoep naast de weg horen. De foto geeft uitsluitsel.
            verdict = ("warning", verdict[1], f"{verdict[2]}; OpenStreetMap: fietsen toegestaan")

        # 1. Routepunten vlak bij het bord, in de richting van het bord.
        near = sorted(
            int(i)
            for i in route_tree.query(spoint.buffer(SIGN_RADIUS_M))
            if headings[int(i)] is not None
            and _angle_diff(headings[int(i)], bearing) <= SIGN_HEADING_TOLERANCE  # type: ignore[arg-type]
        )
        covered_until = -1
        for start in near:
            if start <= covered_until:
                continue
            # 3. Volg de route zolang die over de weg van het bord gaat.
            flagged: dict[int, tuple[float, Way, tuple[str, str, str]]] = {}
            i = start
            while i < len(samples):
                if (samples[i][2] - samples[start][2]) * 1000.0 > SIGN_MAX_M:
                    break
                x, y = xy[i]
                distance = way_line.distance(Point(x, y))
                if distance > FOLLOW_RADIUS_M:
                    break
                nearest = way_index.nearest_within(x, y, FOLLOW_RADIUS_M)
                if nearest and nearest[0][1] is not sign_way and nearest[0][0] + 1.0 < distance:
                    break
                flagged[i] = (distance, sign_way, verdict)
                i += 1
            covered_until = i
            if not flagged:
                continue
            length = (samples[max(flagged)][2] - samples[min(flagged)][2]) * 1000.0
            if length < SIGN_MIN_M:
                continue
            for segment in _to_segments(flagged, samples, SOURCES["ndw"]):
                segment.signs.append(_sign_dict(sign))
                found.append(segment)
    # Meerdere borden op hetzelfde stuk (begin en eind van een pad) → één melding.
    return combine([], found, samples)


# -- BGT ---------------------------------------------------------------------

#: BGT-functies waar fietsen niet vanzelfsprekend is, met hun omschrijving.
FOOT_FUNCTIONS = {
    "voetpad": "Voetpad volgens BGT",
    "voetpad op trap": "Trap volgens BGT",
    "voetgangersgebied": "Voetgangersgebied volgens BGT",
    "ruiterpad": "Ruiterpad volgens BGT",
}
#: Functies waar je (meestal) wel mag fietsen.
RIDE_FUNCTIONS = frozenset(
    {
        "fietspad",
        "rijbaan lokale weg",
        "rijbaan regionale weg",
        "woonerf",
        "inrit",
        "transitie",
        "overweg",
        "parkeervlak",
    }
)
#: Een routepunt ligt op een voetpad als het er zo dichtbij ligt…
BGT_ON_M = 3.0
#: …en er binnen deze afstand geen fietsbaar wegdeel ligt (GPS-afwijking).
BGT_RIDE_NEAR_M = 8.0
#: Tegels rond elk routepunt tot deze afstand.
BGT_TILE_MARGIN_M = 10.0


def _polygon(area: bgt.Area, projection: LocalProjection) -> Polygon | None:
    try:
        poly = Polygon(
            projection.to_xy_many(area.exterior),
            [projection.to_xy_many(h) for h in area.holes],
        )
        if not poly.is_valid:
            poly = poly.buffer(0)
        return None if poly.is_empty else poly
    except (ValueError, TypeError):
        return None


#: Ligt de route zo dicht bij een OSM-weg waar fietsen uitdrukkelijk mag, dan
#: telt de BGT niet: die zegt niets over toegang en tekent paden in het bos
#: vaak niet (of als voetpad) naast een fietspad dat er wel ligt.
BGT_OSM_OVERRULE_M = 5.0


def detect_bgt(
    samples: Samples,
    projection: LocalProjection,
    ways: Sequence[Way] = (),
) -> tuple[list[Segment], str | None]:
    """Meldingen op grond van BGT-wegdelen; geeft (meldingen, notitie)."""
    settings = get_settings()
    nl = [i for i, (lat, lon, _km) in enumerate(samples) if in_netherlands(lat, lon)]
    if not nl:
        return [], None
    tiles = sorted(bgt.tiles_for(((samples[i][0], samples[i][1]) for i in nl), BGT_TILE_MARGIN_M))
    if len(tiles) > settings.bgt_max_tiles:
        return [], (
            f"BGT overgeslagen: de route beslaat {len(tiles)} kaarttegels "
            f"(maximaal {settings.bgt_max_tiles})."
        )
    areas, failed = bgt.load_areas(tiles)
    note = None
    if failed:
        note = f"BGT: {failed} van {len(tiles)} kaarttegels konden niet worden opgehaald."
        if failed == len(tiles):
            return [], note

    foot: list[tuple[Polygon, str]] = []
    ride: list[Polygon] = []
    for area in areas:
        if area.functie not in FOOT_FUNCTIONS and area.functie not in RIDE_FUNCTIONS:
            continue
        poly = _polygon(area, projection)
        if poly is None:
            continue
        if area.functie in FOOT_FUNCTIONS:
            foot.append((poly, area.functie))
        else:
            ride.append(poly)
    if not foot:
        return [], note
    foot_tree = STRtree([p for p, _ in foot])
    ride_tree = STRtree(ride) if ride else None
    bike_ways = _WayIndex([w for w in ways if osm_allows_bikes(w.tags)], projection)

    flagged: dict[int, tuple[float, Way, tuple[str, str, str]]] = {}
    for i in nl:
        point = Point(projection.to_xy(samples[i][0], samples[i][1]))
        hits = [
            (foot[int(j)][0].distance(point), foot[int(j)][1])
            for j in foot_tree.query(point.buffer(BGT_ON_M))
        ]
        hits = [h for h in hits if h[0] <= BGT_ON_M]
        if not hits:
            continue
        if ride_tree is not None and any(
            ride[int(j)].distance(point) <= BGT_RIDE_NEAR_M
            for j in ride_tree.query(point.buffer(BGT_RIDE_NEAR_M))
        ):
            continue
        if bike_ways.nearest_within(point.x, point.y, BGT_OSM_OVERRULE_M):
            continue
        distance, functie = min(hits)
        code = "bgt_" + functie.replace(" ", "_")
        flagged[i] = (
            distance,
            Way(id=0, tags={}, coords=[]),
            ("warning", code, FOOT_FUNCTIONS[functie]),
        )
    segments = _to_segments(flagged, samples, SOURCES["bgt"])
    for segment in segments:
        segment.way_id = None
    return segments, note
