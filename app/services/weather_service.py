"""Regencontrole: kom je droog over de route?

Voor elke kilometer van de route wordt berekend hoe laat je daar bent
(vertrektijd + afstand / snelheid) en welke neerslag er op dat moment op die
plek verwacht wordt. Twee bronnen, beide zonder API-sleutel:

* **Buienradar-radarverwachting** (5 minuten, ~2 uur vooruit, alleen NL):
  gebaseerd op de actuele radarbeelden, het nauwkeurigst op korte termijn.
* **KNMI Harmonie-model** via Open-Meteo (``models=knmi_seamless``, per
  15 minuten). Na ~2,5 dag en buiten het KNMI-domein valt Open-Meteo zelf
  terug op ECMWF; dat wordt in de uitkomst als lagere betrouwbaarheid gemeld.

Het KNMI Data Platform zelf vereist een API-sleutel, vandaar Open-Meteo als
doorgeefluik voor hetzelfde KNMI-model.
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

import requests

from app.config import get_settings
from app.services.geo import haversine_m, in_netherlands

logger = logging.getLogger(__name__)

SOURCE_RADAR = "Buienradar (radar)"
SOURCE_KNMI = "KNMI Harmonie"
SOURCE_ECMWF = "ECMWF"

_SAMPLE_STEP_KM = 1.0
# Rastercel voor het samenvoegen van modelverzoeken (~Harmonie-resolutie 2,5 km)
_MODEL_GRID_DEG = 0.025
_RADAR_GRID_DEG = 0.02
_RADAR_HORIZON = timedelta(minutes=115)
_RADAR_MAX_REQUESTS = 40
# Harmonie rekent ~60 uur vooruit; daarna levert knmi_seamless ECMWF.
_KNMI_HORIZON = timedelta(hours=60)
# Ruwe begrenzing van het Harmonie-domein (West-Europa).
_KNMI_BBOX = (47.0, -5.0, 58.0, 16.0)
# Natte stukken die minder dan dit uit elkaar liggen worden één stuk.
_MERGE_GAP_KM = 2.0


class WeatherError(RuntimeError):
    """Weerdata kon niet worden opgehaald."""


@dataclass(slots=True)
class WeatherRequest:
    departure: datetime  # tijdzone-bewust
    speed_kmh: float


@dataclass(slots=True)
class Sample:
    km: float
    lat: float
    lon: float
    eta: datetime
    mm_h: float = 0.0
    probability: int | None = None
    source: str = SOURCE_KNMI


@dataclass(slots=True)
class RainSegment:
    start_km: float
    end_km: float
    start_time: datetime
    end_time: datetime
    max_mm_h: float
    sources: list[str]
    max_probability: int | None = None
    coordinates: list[tuple[float, float]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return intensity_label(self.max_mm_h)

    @property
    def uncertain(self) -> bool:
        """Het model rekent regen, maar de kans erop is klein.

        Komt vooral voor bij ECMWF op langere termijn: één modelrun zegt regen,
        de ensemble-kans is laag. Radar is een meting, dus nooit onzeker.
        """
        if SOURCE_RADAR in self.sources or self.max_probability is None:
            return False
        return self.max_probability < get_settings().weather_uncertain_probability


@dataclass(slots=True)
class WeatherReport:
    departure: datetime
    arrival: datetime
    speed_kmh: float
    samples: list[Sample]
    segments: list[RainSegment]
    max_probability: int | None
    issued: datetime
    note: str | None = None


def intensity_label(mm_h: float) -> str:
    if mm_h < 1.0:
        return "Lichte regen"
    if mm_h < 4.0:
        return "Matige regen"
    return "Zware regen"


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().weather_timezone)


def _sample_route(
    coords: Sequence[tuple[float, float]], request: WeatherRequest
) -> list[Sample]:
    """Punt per kilometer (plus het eindpunt) met de verwachte passeertijd."""
    speed_ms = request.speed_kmh / 3.6
    samples: list[Sample] = []
    step = _SAMPLE_STEP_KM * 1000
    next_mark = 0.0
    travelled = 0.0

    def add(lat: float, lon: float, dist: float) -> None:
        samples.append(
            Sample(
                km=round(dist / 1000, 2),
                lat=lat,
                lon=lon,
                eta=request.departure + timedelta(seconds=dist / speed_ms),
            )
        )

    for (lat1, lon1), (lat2, lon2) in zip(coords, coords[1:]):
        seg = haversine_m(lat1, lon1, lat2, lon2)
        while seg > 0 and next_mark <= travelled + seg:
            f = (next_mark - travelled) / seg
            add(lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f, next_mark)
            next_mark += step
        travelled += seg
    if not samples or samples[-1].km * 1000 < travelled - 1:
        add(coords[-1][0], coords[-1][1], travelled)
    return samples


def _cell(lat: float, lon: float, size: float) -> tuple[float, float]:
    return (round(round(lat / size) * size, 4), round(round(lon / size) * size, 4))


def _parse_local(value: str, zone: ZoneInfo) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=zone)


def _fetch_model(
    cells: list[tuple[float, float]], start: datetime, end: datetime
) -> dict[tuple[float, float], tuple[list[datetime], list[float | None], list[datetime], list[int | None]]]:
    """Haal per rastercel de 15-minutenneerslag en uurlijkse regenkans op."""
    settings = get_settings()
    zone = tz()
    now = datetime.now(zone)
    days_ahead = max(1, math.ceil((end - now).total_seconds() / 86400) + 1)
    params = {
        "latitude": ",".join(f"{c[0]:.4f}" for c in cells),
        "longitude": ",".join(f"{c[1]:.4f}" for c in cells),
        "minutely_15": "precipitation",
        "hourly": "precipitation_probability",
        "models": settings.weather_model,
        "timezone": settings.weather_timezone,
        "past_days": 1 if start < now.replace(hour=0, minute=0) else 0,
        "forecast_days": min(16, days_ahead),
    }
    response = None
    for attempt in range(3):
        try:
            response = requests.get(
                settings.weather_api_url,
                params=params,
                timeout=settings.weather_timeout,
                headers={"User-Agent": settings.user_agent},
            )
        except requests.RequestException as exc:
            if attempt == 2:
                raise WeatherError(f"Weerdienst niet bereikbaar: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
            continue
        # Open-Meteo meldt tijdelijke drukte met 429/503; even wachten helpt meestal.
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            logger.info("Weerdienst gaf %s, opnieuw proberen", response.status_code)
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    assert response is not None
    if response.status_code >= 400:
        raise WeatherError(
            f"Weerdienst tijdelijk niet beschikbaar ({response.status_code}); probeer het zo opnieuw."
        )
    payload = response.json()
    locations = payload if isinstance(payload, list) else [payload]
    if len(locations) != len(cells):
        raise WeatherError("Onverwacht antwoord van de weerdienst.")

    result = {}
    for cell, loc in zip(cells, locations):
        m15 = loc.get("minutely_15") or {}
        hourly = loc.get("hourly") or {}
        result[cell] = (
            [_parse_local(t, zone) for t in m15.get("time", [])],
            list(m15.get("precipitation", [])),
            [_parse_local(t, zone) for t in hourly.get("time", [])],
            list(hourly.get("precipitation_probability", [])),
        )
    return result


def _slot_value(times: list[datetime], values: list, eta: datetime, step: timedelta):
    """Waarde van het tijdvak waarin ``eta`` valt.

    Open-Meteo geeft per tijdstip de som over het voorafgaande tijdvak, dus we
    nemen het eerste tijdstip op of na ``eta``.
    """
    if not times:
        return None
    first = times[0]
    index = math.ceil((eta - first) / step)
    if index < 0 or index >= len(values):
        return None
    return values[index]


def radar_mm_h(value: int) -> float:
    """Buienradar-waarde (0-255) naar mm/u, volgens hun documentatie."""
    if value <= 0:
        return 0.0
    return round(10 ** ((value - 109) / 32), 2)


def _fetch_radar(cell: tuple[float, float], now: datetime) -> list[tuple[datetime, float]]:
    settings = get_settings()
    response = requests.get(
        settings.weather_radar_url,
        params={"lat": f"{cell[0]:.2f}", "lon": f"{cell[1]:.2f}"},
        timeout=settings.weather_timeout,
        headers={"User-Agent": settings.user_agent},
    )
    response.raise_for_status()
    rows: list[tuple[datetime, float]] = []
    previous: datetime | None = None
    for line in response.text.strip().splitlines():
        try:
            raw, hhmm = line.strip().split("|")
            hour, minute = (int(x) for x in hhmm.split(":"))
            value = int(raw)
        except ValueError:
            continue
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Tijden zijn alleen HH:MM; corrigeer voor middernacht.
        if moment < now - timedelta(hours=1):
            moment += timedelta(days=1)
        if previous and moment < previous:
            moment += timedelta(days=1)
        previous = moment
        rows.append((moment, radar_mm_h(value)))
    return rows


def _apply_radar(samples: list[Sample], now: datetime) -> None:
    """Overschrijf de modelwaarde met radar voor punten binnen ~2 uur (NL)."""
    horizon = now + _RADAR_HORIZON
    wanted = [
        s for s in samples
        if now - timedelta(minutes=10) <= s.eta <= horizon and in_netherlands(s.lat, s.lon)
    ]
    if not wanted:
        return
    cells = sorted({_cell(s.lat, s.lon, _RADAR_GRID_DEG) for s in wanted})
    if len(cells) > _RADAR_MAX_REQUESTS:
        step = len(cells) / _RADAR_MAX_REQUESTS
        cells = [cells[int(i * step)] for i in range(_RADAR_MAX_REQUESTS)]
    series: dict[tuple[float, float], list[tuple[datetime, float]]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {cell: pool.submit(_fetch_radar, cell, now) for cell in cells}
        for cell, future in futures.items():
            try:
                series[cell] = future.result()
            except Exception as exc:  # radar is een verfijning, nooit blokkerend
                logger.debug("Radar voor %s mislukt: %s", cell, exc)
    if not series:
        logger.warning("Buienradar niet beschikbaar; alleen modelverwachting gebruikt")
        return

    def nearest(lat: float, lon: float) -> list[tuple[datetime, float]]:
        key = min(series, key=lambda c: (c[0] - lat) ** 2 + (c[1] - lon) ** 2)
        return series[key]

    for sample in wanted:
        rows = nearest(sample.lat, sample.lon)
        best = min(rows, key=lambda r: abs((r[0] - sample.eta).total_seconds()), default=None)
        if best and abs((best[0] - sample.eta).total_seconds()) <= 450:
            sample.mm_h = best[1]
            sample.probability = None
            sample.source = SOURCE_RADAR


def _in_knmi_domain(lat: float, lon: float) -> bool:
    min_lat, min_lon, max_lat, max_lon = _KNMI_BBOX
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _build_segments(
    samples: list[Sample], threshold: float
) -> list[RainSegment]:
    segments: list[RainSegment] = []
    for sample in samples:
        if sample.mm_h < threshold:
            continue
        last = segments[-1] if segments else None
        if last and sample.km - last.end_km <= _MERGE_GAP_KM:
            last.end_km = sample.km
            last.end_time = sample.eta
            last.max_mm_h = max(last.max_mm_h, sample.mm_h)
            if sample.source not in last.sources:
                last.sources.append(sample.source)
            if sample.probability is not None:
                last.max_probability = max(last.max_probability or 0, sample.probability)
        else:
            segments.append(
                RainSegment(
                    start_km=sample.km,
                    end_km=sample.km,
                    start_time=sample.eta,
                    end_time=sample.eta,
                    max_mm_h=sample.mm_h,
                    sources=[sample.source],
                    max_probability=sample.probability,
                )
            )
    return segments


def _attach_coordinates(
    segments: list[RainSegment], coords: Sequence[tuple[float, float]]
) -> None:
    """Geef elk nat stuk de routecoördinaten, voor de kaart."""
    if not segments:
        return
    cumulative = [0.0]
    for (a, b), (c, d) in zip(coords, coords[1:]):
        cumulative.append(cumulative[-1] + haversine_m(a, b, c, d))
    for seg in segments:
        start = seg.start_km * 1000 - 500
        end = seg.end_km * 1000 + 500
        seg.coordinates = [
            (round(lat, 6), round(lon, 6))
            for (lat, lon), dist in zip(coords, cumulative)
            if start <= dist <= end
        ]
        if len(seg.coordinates) < 2:
            idx = min(range(len(cumulative)), key=lambda i: abs(cumulative[i] - seg.start_km * 1000))
            seg.coordinates = [coords[max(0, idx - 1)], coords[min(len(coords) - 1, idx + 1)]]


def check_route(
    coords: Sequence[tuple[float, float]], request: WeatherRequest
) -> WeatherReport:
    """Bepaal of en waar je nat wordt bij de gegeven vertrektijd en snelheid."""
    settings = get_settings()
    zone = tz()
    now = datetime.now(zone)
    samples = _sample_route(coords, request)
    arrival = samples[-1].eta

    cells = sorted({_cell(s.lat, s.lon, _MODEL_GRID_DEG) for s in samples})
    model = _fetch_model(cells, request.departure, arrival)

    for sample in samples:
        times, values, h_times, probs = model[_cell(sample.lat, sample.lon, _MODEL_GRID_DEG)]
        amount = _slot_value(times, values, sample.eta, timedelta(minutes=15))
        sample.mm_h = round(float(amount or 0.0) * 4, 2)
        prob = _slot_value(h_times, probs, sample.eta, timedelta(hours=1))
        sample.probability = None if prob is None else int(prob)
        knmi = sample.eta - now <= _KNMI_HORIZON and _in_knmi_domain(sample.lat, sample.lon)
        sample.source = SOURCE_KNMI if knmi else SOURCE_ECMWF

    _apply_radar(samples, now)

    segments = _build_segments(samples, settings.weather_rain_threshold_mm_h)
    _attach_coordinates(segments, coords)

    probabilities = [s.probability for s in samples if s.probability is not None]
    note = None
    if any(s.source == SOURCE_ECMWF for s in samples):
        note = (
            "Een deel van de rit valt buiten het bereik van het KNMI-model "
            "(verder dan ~2,5 dag vooruit of buiten West-Europa); daar is de "
            "grovere ECMWF-verwachting gebruikt. Controleer het dichter bij vertrek opnieuw."
        )
    logger.info(
        "Regencontrole: vertrek %s, %.1f km/u, %d natte stukken",
        request.departure.isoformat(timespec="minutes"),
        request.speed_kmh,
        len(segments),
    )
    return WeatherReport(
        departure=request.departure,
        arrival=arrival,
        speed_kmh=request.speed_kmh,
        samples=samples,
        segments=segments,
        max_probability=max(probabilities) if probabilities else None,
        issued=now,
        note=note,
    )
