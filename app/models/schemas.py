"""Datastructuren voor route- en waterpuntverwerking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

SourceName = Literal["drinkwaterpunten.nl", "openstreetmap"]


@dataclass(slots=True)
class RoutePoint:
    """Eén punt op de route."""

    lat: float
    lon: float
    ele: float | None = None


@dataclass(slots=True)
class WaterPoint:
    """Een drinkwaterpunt met optionele metadata."""

    lat: float
    lon: float
    name: str | None = None
    operator: str | None = None
    opening_hours: str | None = None
    website: str | None = None
    source: str = "onbekend"
    ele: float | None = None
    description: str | None = None
    # Ingevuld tijdens routeberekening:
    distance_to_route_m: float = 0.0
    along_route_km: float = 0.0
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RoadWork:
    """Een geplande wegwerkzaamheid die fietsers raakt."""

    lat: float
    lon: float
    start: str | None = None
    end: str | None = None
    cause: str | None = None
    authority: str | None = None
    detour: str | None = None
    comment: str | None = None
    # Het afgesloten stuk zelf als een of meer lijnen [[lat, lon], ...]. Veel
    # nauwkeuriger dan het losse punt, dat vaak bij een omleidingsbord staat.
    lines: list[list[list[float]]] = field(default_factory=list)
    # Ingevuld tijdens routeberekening:
    distance_to_route_m: float = 0.0
    along_route_km: float = 0.0


class RoadWorkOut(BaseModel):
    lat: float
    lon: float
    start: str | None = None
    end: str | None = None
    cause: str | None = None
    authority: str | None = None
    detour: str | None = None
    lines: list[list[list[float]]] = []
    distance_to_route_m: float
    along_route_km: float


class LegalitySegmentOut(BaseModel):
    """Stuk route over een pad waar fietsen niet (zonder meer) mag."""

    severity: str  # forbidden | warning
    code: str
    label: str
    way_name: str | None = None
    highway: str | None = None
    start_km: float
    end_km: float
    length_m: float
    coordinates: list[list[float]]


class RainSegmentOut(BaseModel):
    """Stuk route waar tijdens het passeren neerslag verwacht wordt."""

    start_km: float
    end_km: float
    start_time: str  # ISO, lokale tijd
    end_time: str
    max_mm_h: float
    label: str
    sources: list[str]
    max_probability: int | None = None
    uncertain: bool = False
    coordinates: list[list[float]]


class WeatherSampleOut(BaseModel):
    km: float
    time: str  # ISO, lokale tijd
    mm_h: float
    probability: int | None = None
    source: str


class WaterPointOut(BaseModel):
    lat: float
    lon: float
    name: str | None = None
    operator: str | None = None
    opening_hours: str | None = None
    website: str | None = None
    source: str
    distance_to_route_m: float
    along_route_km: float


class RouteStats(BaseModel):
    total_distance_km: float
    water_point_count: int
    average_gap_km: float | None = None
    longest_gap_km: float | None = None
    longest_gap_start_km: float | None = None
    warning: str | None = None
    has_elevation: bool = False


class ProcessResult(BaseModel):
    job_id: str
    filename: str
    source: str
    radius_m: int
    nl_share: float
    # False als alleen de route is gecontroleerd (geen waterpunten gezocht)
    water_checked: bool = True
    stats: RouteStats
    route: list[list[float]]
    water_points: list[WaterPointOut]
    bounds: list[list[float]]
    # Alleen gevuld als er op wegwerkzaamheden is gecontroleerd
    roadworks_checked: bool = False
    roadworks_date: str | None = None
    roadworks_error: str | None = None
    road_works: list[RoadWorkOut] = []
    # Alleen gevuld als er op verboden paden is gecontroleerd
    legality_checked: bool = False
    legality_error: str | None = None
    legality_segments: list[LegalitySegmentOut] = []
    # Alleen gevuld als er op regen is gecontroleerd
    weather_checked: bool = False
    weather_error: str | None = None
    weather_departure: str | None = None
    weather_arrival: str | None = None
    weather_speed_kmh: float | None = None
    weather_issued: str | None = None
    weather_max_probability: int | None = None
    weather_note: str | None = None
    weather_segments: list[RainSegmentOut] = []
    weather_samples: list[WeatherSampleOut] = []


class StravaRouteInfo(BaseModel):
    """Route zoals getoond in de keuzelijst."""

    id: str
    name: str
    distance_km: float
    elevation_gain_m: float | None = None
    type: str | None = None
    private: bool = False
    url: str


class StravaAthlete(BaseModel):
    id: int | None = None
    firstname: str | None = None
    lastname: str | None = None
    username: str | None = None


class StravaStatus(BaseModel):
    configured: bool
    connected: bool
    athlete: StravaAthlete | None = None
    scope: str | None = None
    # "oauth" = via de koppelknop, "env" = vast token uit environment variables
    mode: str | None = None
    oauth_available: bool = False


class StravaProcessedRoute(BaseModel):
    """Resultaat per verwerkte Strava-route."""

    route_id: str
    name: str
    original_job_id: str | None = None
    original_filename: str | None = None
    result: ProcessResult | None = None
    error: str | None = None


class RouteboekRouteInfo(BaseModel):
    """Route zoals getoond in de keuzelijst van routeboek.cc."""

    id: str
    slug: str
    name: str
    distance_km: float | None = None
    elevation_m: int | None = None
    rating: float | None = None
    url: str


class RouteboekProcessedRoute(BaseModel):
    """Resultaat per verwerkte routeboek.cc-route."""

    route_id: str
    name: str
    original_job_id: str | None = None
    original_filename: str | None = None
    result: ProcessResult | None = None
    error: str | None = None
