"""Koppelen van waterpunten aan de route: afstand, positie, sortering, statistiek."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from geopy.distance import great_circle
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from app.config import get_settings
from app.models.schemas import RoadWork, RoutePoint, RouteStats, WaterPoint
from app.services.geo import LocalProjection

logger = logging.getLogger(__name__)


class RouteIndex:
    """Ruimtelijke index over de route voor snelle afstandsberekeningen."""

    def __init__(self, points: Sequence[RoutePoint]) -> None:
        if len(points) < 2:
            raise ValueError("Route heeft minimaal twee punten nodig")
        self.points = list(points)
        coords = [(p.lat, p.lon) for p in self.points]
        self.projection = LocalProjection.from_points(coords)
        self.xy = self.projection.to_xy_many(coords)
        self.line = LineString(self.xy)

        self._segments = [
            LineString([self.xy[i], self.xy[i + 1]]) for i in range(len(self.xy) - 1)
        ]
        self._cum: list[float] = [0.0]
        for seg in self._segments:
            self._cum.append(self._cum[-1] + seg.length)
        self._tree = STRtree(self._segments)

        self.projected_length_m = self._cum[-1]
        self.total_distance_m = self._route_length(coords)
        self._scale = (
            self.total_distance_m / self.projected_length_m
            if self.projected_length_m > 0
            else 1.0
        )

    @staticmethod
    def _route_length(coords: Sequence[tuple[float, float]]) -> float:
        """Lengte van de route in meters.

        Bewust de bolvormige (great-circle) methode: dat is wat Strava, Garmin
        en Wahoo ook gebruiken, zodat de getoonde afstand overeenkomt met wat de
        gebruiker in die apps ziet. Een WGS84-ellipsoïde zou op onze
        breedtegraad ~0,2% hoger uitkomen.
        """
        total = 0.0
        for a, b in zip(coords, coords[1:]):
            total += great_circle(a, b).meters
        return total

    def locate(self, lat: float, lon: float) -> tuple[float, float]:
        """Geef (afstand tot route in m, positie langs route in m)."""
        point = Point(self.projection.to_xy(lat, lon))
        idx = int(self._tree.nearest(point))
        seg = self._segments[idx]
        distance_m = seg.distance(point)
        along_m = (self._cum[idx] + seg.project(point)) * self._scale
        return distance_m, along_m


def attach_to_route(
    index: RouteIndex, water_points: Sequence[WaterPoint], radius_m: int
) -> list[WaterPoint]:
    """Filter op zoekradius, bepaal positie langs de route en sorteer op rijrichting."""
    selected: list[WaterPoint] = []
    for wp in water_points:
        distance_m, along_m = index.locate(wp.lat, wp.lon)
        if distance_m > radius_m:
            continue
        wp.distance_to_route_m = round(distance_m, 1)
        wp.along_route_km = round(along_m / 1000.0, 3)
        selected.append(wp)
    selected.sort(key=lambda w: (w.along_route_km, w.distance_to_route_m))
    logger.info(
        "%d van %d waterpunten binnen %d m van de route",
        len(selected),
        len(water_points),
        radius_m,
    )
    return selected


def deduplicate(
    water_points: Sequence[WaterPoint],
    projection: LocalProjection,
    min_distance_m: float | None = None,
) -> list[WaterPoint]:
    """Verwijder dubbele punten die dichter dan `min_distance_m` bij elkaar liggen."""
    threshold = (
        get_settings().dedupe_distance_m if min_distance_m is None else min_distance_m
    )
    cell = max(threshold, 1.0)
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    kept: list[WaterPoint] = []

    ordered = sorted(
        water_points,
        key=lambda w: (
            w.distance_to_route_m,
            0 if (w.name or w.operator or w.website) else 1,
        ),
    )
    for wp in ordered:
        x, y = projection.to_xy(wp.lat, wp.lon)
        cx, cy = int(x // cell), int(y // cell)
        duplicate = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ox, oy in grid.get((cx + dx, cy + dy), ()):
                    if (ox - x) ** 2 + (oy - y) ** 2 <= threshold**2:
                        duplicate = True
                        break
                if duplicate:
                    break
            if duplicate:
                break
        if duplicate:
            continue
        grid.setdefault((cx, cy), []).append((x, y))
        kept.append(wp)

    kept.sort(key=lambda w: w.along_route_km)
    removed = len(water_points) - len(kept)
    if removed:
        logger.info("%d dubbele waterpunten verwijderd (<%.0f m)", removed, threshold)
    return kept


def attach_road_works(
    index: RouteIndex,
    road_works: Sequence[RoadWork],
    radius_m: int,
    line_tolerance_m: float | None = None,
) -> list[RoadWork]:
    """Houd de werkzaamheden over die echt op de route liggen, in rijrichting.

    Heeft een melding een afgesloten stuk (vrijwel altijd), dan telt de afstand
    van dat stuk tot de route, met `line_tolerance_m` als grens. Het losse punt
    van NDW is daarvoor ongeschikt: dat staat vaak bij een omleidingsbord of
    op een parallelle straat, tot honderden meters van het werk zelf. De marker
    komt dan op de plek waar het afgesloten stuk de route raakt.

    Alleen zonder afgesloten stuk valt de controle terug op het punt, met
    `radius_m` als grens.
    """
    tolerance = (
        get_settings().roadworks_line_tolerance_m
        if line_tolerance_m is None
        else line_tolerance_m
    )
    selected: list[RoadWork] = []
    by_line = 0
    for work in road_works:
        if work.lines:
            hit = _nearest_on_closure(index, work.lines)
            if hit is None or hit[0] > tolerance:
                continue
            distance_m, (work.lat, work.lon) = hit
            by_line += 1
        else:
            distance_m, _ = index.locate(work.lat, work.lon)
            if distance_m > radius_m:
                continue
        _, along_m = index.locate(work.lat, work.lon)
        work.distance_to_route_m = round(distance_m, 1)
        work.along_route_km = round(along_m / 1000.0, 3)
        selected.append(work)
    selected.sort(key=lambda w: (w.along_route_km, w.distance_to_route_m))
    logger.info(
        "%d van %d wegwerkzaamheden op de route (%d via het afgesloten stuk <= %.0f m, "
        "%d via het punt <= %d m)",
        len(selected),
        len(road_works),
        by_line,
        tolerance,
        len(selected) - by_line,
        radius_m,
    )
    return _merge_road_works(selected, get_settings().roadworks_merge_gap_m / 1000.0)


def _nearest_on_closure(
    index: RouteIndex, lines: Sequence[Sequence[Sequence[float]]]
) -> tuple[float, tuple[float, float]] | None:
    """(afstand in m, punt op het afgesloten stuk dat het dichtst bij de route ligt)."""
    best: tuple[float, tuple[float, float]] | None = None
    for line in lines:
        if len(line) < 2:
            continue
        geometry = LineString(index.projection.to_xy_many([(p[0], p[1]) for p in line]))
        distance = geometry.distance(index.line)
        if best is None or distance < best[0]:
            on_closure, _ = nearest_points(geometry, index.line)
            lat, lon = index.projection.to_latlon(on_closure.x, on_closure.y)
            best = (distance, (round(lat, 6), round(lon, 6)))
    return best


def _merge_road_works(
    road_works: Sequence[RoadWork], max_gap_km: float
) -> list[RoadWork]:
    """Voeg meldingen van dezelfde klus langs hetzelfde stuk weg samen.

    Gemeenten melden een project vaak in stukken: per straatdeel, per fase of
    per rijrichting. Opeenvolgende meldingen met dezelfde oorzaak en instantie
    worden daarom een reeks zolang het gat tot de vorige kleiner is dan
    `max_gap_km`. Het gat telt vanaf het laatste lid van de reeks, zodat een
    lang werkvak niet halverwege breekt. De melding die het dichtst bij de
    route ligt blijft over als representant.
    """
    groups: list[list[RoadWork]] = []
    open_group: dict[tuple[str | None, str | None], list[RoadWork]] = {}
    for work in sorted(road_works, key=lambda w: w.along_route_km):
        key = (work.cause, work.authority)
        group = open_group.get(key)
        if group and work.along_route_km - group[-1].along_route_km <= max_gap_km:
            group.append(work)
            continue
        group = [work]
        groups.append(group)
        open_group[key] = group

    merged = [min(group, key=lambda w: w.distance_to_route_m) for group in groups]
    merged.sort(key=lambda w: (w.along_route_km, w.distance_to_route_m))
    removed = len(road_works) - len(merged)
    if removed:
        logger.info("%d wegwerkzaamheden op hetzelfde stuk weg samengevoegd", removed)
    return merged


def build_stats(
    index: RouteIndex, water_points: Sequence[WaterPoint], has_elevation: bool
) -> RouteStats:
    """Bereken analysegegevens over de route en gevonden waterpunten."""
    settings = get_settings()
    total_km = index.total_distance_m / 1000.0
    positions = [wp.along_route_km for wp in water_points]

    average_gap = None
    if len(positions) >= 2:
        gaps = [b - a for a, b in zip(positions, positions[1:])]
        average_gap = round(sum(gaps) / len(gaps), 2)

    # Voor de langste "droge" afstand tellen start en finish mee.
    anchors = [0.0, *positions, total_km]
    longest_gap = 0.0
    longest_start = 0.0
    for a, b in zip(anchors, anchors[1:]):
        if b - a > longest_gap:
            longest_gap = b - a
            longest_start = a

    warning = None
    if not water_points:
        warning = "Geen drinkwaterpunten gevonden binnen de gekozen zoekradius."
    elif longest_gap > settings.gap_warning_km:
        warning = (
            f"Let op: {longest_gap:.1f} km zonder drinkwaterpunt "
            f"(vanaf km {longest_start:.1f})."
        )

    return RouteStats(
        total_distance_km=round(total_km, 2),
        water_point_count=len(water_points),
        average_gap_km=average_gap,
        longest_gap_km=round(longest_gap, 2),
        longest_gap_start_km=round(longest_start, 2),
        warning=warning,
        has_elevation=has_elevation,
    )
