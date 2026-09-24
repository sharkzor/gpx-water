"""Geometrische hulpfuncties: lokale metrische projectie en NL-detectie."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from shapely.geometry import Point, Polygon

EARTH_RADIUS_M = 6371008.8
_DEG_LAT_M = 111132.0

# Sterk vereenvoudigde omtrek van Nederland (lon, lat). Voldoende nauwkeurig
# voor de vraag "ligt deze route grotendeels in Nederland?".
NL_OUTLINE: tuple[tuple[float, float], ...] = (
    (3.36, 51.28),
    (3.36, 51.55),
    (3.95, 51.98),
    (4.45, 52.35),
    (4.72, 52.98),
    (5.35, 53.30),
    (6.20, 53.55),
    (6.95, 53.45),
    (7.22, 53.20),
    (7.05, 52.85),
    (6.70, 52.63),
    (7.08, 52.42),
    (7.06, 52.23),
    (6.75, 52.10),
    (6.83, 51.99),
    (6.42, 51.86),
    (6.22, 51.87),
    (6.16, 51.54),
    (6.23, 51.36),
    (6.02, 51.24),
    (5.90, 50.98),
    (6.02, 50.75),
    (5.64, 50.84),
    (5.70, 51.09),
    (5.20, 51.26),
    (4.83, 51.42),
    (4.39, 51.45),
    (3.85, 51.41),
    (3.36, 51.28),
)

_NL_POLYGON = Polygon(NL_OUTLINE)


def in_netherlands(lat: float, lon: float) -> bool:
    """Ligt een coordinaat (ruwweg) in Nederland?"""
    return _NL_POLYGON.covers(Point(lon, lat))


def nl_share(points: Sequence[tuple[float, float]]) -> float:
    """Aandeel van de punten dat in Nederland ligt (0..1)."""
    if not points:
        return 0.0
    inside = sum(1 for lat, lon in points if in_netherlands(lat, lon))
    return inside / len(points)


class LocalProjection:
    """Equirectangulaire projectie rond een referentiepunt, in meters.

    Nauwkeurig genoeg (<0.1% fout) voor routes van enkele honderden km.
    """

    __slots__ = ("lat0", "lon0", "_mx", "_my")

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0 = lat0
        self.lon0 = lon0
        self._my = _DEG_LAT_M
        self._mx = _DEG_LAT_M * math.cos(math.radians(lat0))

    @classmethod
    def from_points(cls, points: Sequence[tuple[float, float]]) -> "LocalProjection":
        if not points:
            return cls(52.0, 5.0)
        lat0 = sum(p[0] for p in points) / len(points)
        lon0 = sum(p[1] for p in points) / len(points)
        return cls(lat0, lon0)

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self._mx, (lat - self.lat0) * self._my)

    def to_xy_many(
        self, points: Iterable[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        return [self.to_xy(lat, lon) for lat, lon in points]

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return (self.lat0 + y / self._my, self.lon0 + x / self._mx)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Afstand in meters tussen twee coordinaten."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bounding_box(
    points: Sequence[tuple[float, float]], margin_m: float = 0.0
) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) met optionele marge in meters."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    if margin_m:
        dlat = margin_m / _DEG_LAT_M
        mid = (min_lat + max_lat) / 2
        dlon = margin_m / max(1.0, _DEG_LAT_M * math.cos(math.radians(mid)))
        min_lat, max_lat = min_lat - dlat, max_lat + dlat
        min_lon, max_lon = min_lon - dlon, max_lon + dlon
    return min_lat, min_lon, max_lat, max_lon
