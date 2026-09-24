"""Tests voor de regencontrole (KNMI-model + Buienradar) — volledig offline."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import routeboek_service, waterpoints_nl, weather_service as ws
from app.services.weather_service import Sample, WeatherRequest


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, waterpoints_gpx: str):
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)
    monkeypatch.setattr(ws.time, "sleep", lambda s: None)
    yield


class _Resp:
    def __init__(self, status: int = 200, json_data=None, text: str = "") -> None:
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _model_payload(
    params: dict, rain_from: datetime | None, rain_until: datetime | None, wet_probability: int = 80
):
    """Open-Meteo-achtig antwoord: droog, behalve tussen rain_from en rain_until."""
    zone = ws.tz()
    start = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    days = int(params["forecast_days"])
    times15 = [start + timedelta(minutes=15 * i) for i in range(days * 96)]
    hours = [start + timedelta(hours=i) for i in range(days * 24)]

    def wet(t: datetime) -> bool:
        # t is het einde van het tijdvak; regen valt in de vakken die eindigen in (from, until]
        return bool(rain_from and rain_from < t <= rain_until)

    location = {
        "minutely_15": {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in times15],
            "precipitation": [0.5 if wet(t) else 0.0 for t in times15],
        },
        "hourly": {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in hours],
            # uurvak eindigend op t overlapt met de regen
            "precipitation_probability": [
                wet_probability
                if rain_from and rain_from < t and t - timedelta(hours=1) < rain_until
                else 5
                for t in hours
            ],
        },
    }
    count = len(params["latitude"].split(","))
    return [location] * count if count > 1 else location


def _fake_http(
    monkeypatch, rain_from=None, rain_until=None, radar_value=0, model_status=None,
    wet_probability=80,
):
    calls = {"model": 0, "radar": 0}
    statuses = list(model_status or [])

    def fake_get(url, params=None, **kwargs):
        if "raintext" in url:
            calls["radar"] += 1
            now = datetime.now(ws.tz())
            lines = [
                f"{radar_value:03d}|{(now + timedelta(minutes=5 * i)):%H:%M}" for i in range(24)
            ]
            return _Resp(text="\n".join(lines))
        calls["model"] += 1
        if statuses:
            status = statuses.pop(0)
            if status != 200:
                return _Resp(status=status, json_data={"error": True})
        return _Resp(json_data=_model_payload(params, rain_from, rain_until, wet_probability))

    monkeypatch.setattr(ws.requests, "get", fake_get)
    return calls


def _coords(sample_gpx: str) -> list[tuple[float, float]]:
    return [
        (float(a), float(b))
        for a, b in re.findall(r'lat="([\d.]+)" lon="([\d.]+)"', sample_gpx)
    ]


def _later(hours: float) -> datetime:
    return (datetime.now(ws.tz()) + timedelta(hours=hours)).replace(second=0, microsecond=0)


def _quarter(hours: float) -> datetime:
    """Vertrektijd op een kwartiergrens, zodat tijdvakken exact uitkomen."""
    moment = _later(hours)
    return moment.replace(minute=moment.minute // 15 * 15)


# --------------------------------------------------------------- rekenwerk


def test_passeertijd_per_kilometer(sample_gpx: str) -> None:
    dep = _later(3)
    samples = ws._sample_route(_coords(sample_gpx), WeatherRequest(dep, 30))
    assert samples[0].km == 0 and samples[0].eta == dep
    # ~11,1 km bij 30 km/u = ~22 minuten
    assert samples[-1].km == pytest.approx(11.1, abs=0.1)
    assert (samples[-1].eta - dep).total_seconds() / 60 == pytest.approx(22.2, abs=0.3)
    assert samples[10].km == pytest.approx(10.0)
    assert (samples[10].eta - dep) == timedelta(minutes=20)


def test_snelheid_verandert_passeertijd(sample_gpx: str) -> None:
    dep = _later(3)
    slow = ws._sample_route(_coords(sample_gpx), WeatherRequest(dep, 15))
    assert (slow[10].eta - dep) == timedelta(minutes=40)


def test_radarwaarde_naar_mm_per_uur() -> None:
    assert ws.radar_mm_h(0) == 0.0
    assert ws.radar_mm_h(77) == pytest.approx(0.1)
    assert ws.radar_mm_h(109) == pytest.approx(1.0)
    assert ws.radar_mm_h(141) == pytest.approx(10.0)


def test_tijdvak_is_som_van_voorafgaand_kwartier() -> None:
    zone = ws.tz()
    t0 = datetime(2026, 9, 24, 0, 0, tzinfo=zone)
    times = [t0 + timedelta(minutes=15 * i) for i in range(4)]
    values = [0, 1, 2, 3]
    step = timedelta(minutes=15)
    assert ws._slot_value(times, values, t0 + timedelta(minutes=10), step) == 1
    assert ws._slot_value(times, values, t0 + timedelta(minutes=15), step) == 1
    assert ws._slot_value(times, values, t0 + timedelta(minutes=16), step) == 2
    assert ws._slot_value(times, values, t0 + timedelta(hours=5), step) is None


def test_natte_punten_worden_samengevoegd() -> None:
    dep = _later(3)
    samples = [
        Sample(km=float(k), lat=52, lon=5, eta=dep + timedelta(minutes=2 * k), mm_h=mmh)
        for k, mmh in enumerate([0, 0.5, 0.3, 0, 2.0, 0, 0, 0, 0.2])
    ]
    segments = ws._build_segments(samples, 0.1)
    # km 1-4 samen (gat van 2 km mag), km 8 apart
    assert [(s.start_km, s.end_km) for s in segments] == [(1.0, 4.0), (8.0, 8.0)]
    assert segments[0].max_mm_h == 2.0
    assert segments[0].label == "Matige regen"


# ------------------------------------------------------------ bronnen


def test_droge_rit_later_vandaag_gebruikt_knmi(monkeypatch, sample_gpx: str) -> None:
    calls = _fake_http(monkeypatch)
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(_later(4), 30))
    assert report.segments == []
    assert calls["model"] == 1 and calls["radar"] == 0
    assert {s.source for s in report.samples} == {ws.SOURCE_KNMI}
    assert report.note is None


def test_regen_onderweg_wordt_gevonden(monkeypatch, sample_gpx: str) -> None:
    dep = _quarter(4)
    # Regen in het eerste kwartier na vertrek = km 0 t/m 7,5 bij 30 km/u
    _fake_http(monkeypatch, dep, dep + timedelta(minutes=15))
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(dep, 30))
    assert len(report.segments) == 1
    seg = report.segments[0]
    assert (seg.start_km, seg.end_km) == (1.0, 7.0)
    assert seg.max_mm_h == pytest.approx(2.0)
    assert seg.max_probability == 80
    assert len(seg.coordinates) >= 2


def test_regen_met_kleine_kans_is_onzeker(monkeypatch, sample_gpx: str) -> None:
    dep = _quarter(4)
    _fake_http(monkeypatch, dep, dep + timedelta(minutes=15), wet_probability=12)
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(dep, 30))
    assert len(report.segments) == 1
    assert report.segments[0].uncertain is True


def test_regen_met_grote_kans_is_zeker(monkeypatch, sample_gpx: str) -> None:
    dep = _quarter(4)
    _fake_http(monkeypatch, dep, dep + timedelta(minutes=15), wet_probability=60)
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(dep, 30))
    assert report.segments[0].uncertain is False


def test_zelfde_regen_maar_later_vertrekken_is_droog(monkeypatch, sample_gpx: str) -> None:
    dep = _quarter(4)
    _fake_http(monkeypatch, dep, dep + timedelta(minutes=15))
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(dep + timedelta(hours=1), 30))
    assert report.segments == []


def test_direct_vertrekken_gebruikt_radar(monkeypatch, sample_gpx: str) -> None:
    calls = _fake_http(monkeypatch, radar_value=109)  # model droog, radar 1 mm/u
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(_later(0), 30))
    assert calls["radar"] >= 1
    assert all(s.source == ws.SOURCE_RADAR for s in report.samples)
    assert len(report.segments) == 1
    assert report.segments[0].sources == [ws.SOURCE_RADAR]
    assert report.segments[0].uncertain is False


def test_ver_vooruit_meldt_ecmwf(monkeypatch, sample_gpx: str) -> None:
    _fake_http(monkeypatch)
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(_later(24 * 4), 30))
    assert {s.source for s in report.samples} == {ws.SOURCE_ECMWF}
    assert report.note and "ECMWF" in report.note


def test_tijdelijke_drukte_wordt_opnieuw_geprobeerd(monkeypatch, sample_gpx: str) -> None:
    calls = _fake_http(monkeypatch, model_status=[503, 200])
    report = ws.check_route(_coords(sample_gpx), WeatherRequest(_later(4), 30))
    assert calls["model"] == 2
    assert report.segments == []


def test_aanhoudende_storing_geeft_nette_fout(monkeypatch, sample_gpx: str) -> None:
    _fake_http(monkeypatch, model_status=[503, 503, 503])
    with pytest.raises(ws.WeatherError, match="tijdelijk niet beschikbaar"):
        ws.check_route(_coords(sample_gpx), WeatherRequest(_later(4), 30))


# ------------------------------------------------------------------- API


def _post(client: TestClient, sample_gpx: str, **fields) -> dict:
    data = {"radius": "500", "source": "nl", **fields}
    response = client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data=data,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_met_regencontrole(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    dep = _quarter(4)
    _fake_http(monkeypatch, dep, dep + timedelta(minutes=15))
    body = _post(
        client, sample_gpx, weather="true",
        departure=dep.strftime("%Y-%m-%dT%H:%M"), speed_kmh="30",
    )
    assert body["weather_checked"] is True
    assert body["weather_error"] is None
    assert body["weather_speed_kmh"] == 30
    assert len(body["weather_segments"]) == 1
    # km 0 t/m 11 plus het eindpunt op 11,1 km
    assert len(body["weather_samples"]) == 13
    assert body["weather_departure"].startswith(dep.strftime("%Y-%m-%dT%H:%M"))

    gpx = client.get(f"/api/download/{body['job_id']}").text
    assert "Regen" in gpx and "<type>Weather</type>" in gpx
    assert "mm/u" in gpx


def test_onzekere_regen_krijgt_eigen_waypointnaam(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    dep = _quarter(4)
    _fake_http(monkeypatch, dep, dep + timedelta(minutes=15), wet_probability=10)
    body = _post(client, sample_gpx, weather="true", departure=dep.strftime("%Y-%m-%dT%H:%M"))
    assert body["weather_segments"][0]["uncertain"] is True
    gpx = client.get(f"/api/download/{body['job_id']}").text
    assert "Mogelijk regen - 1 km" in gpx
    assert "(onzeker)" in gpx


def test_upload_zonder_regencontrole_raakt_weerdienst_niet(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    calls = _fake_http(monkeypatch)
    body = _post(client, sample_gpx)
    assert body["weather_checked"] is False
    assert calls == {"model": 0, "radar": 0}


def test_storing_weerdienst_blokkeert_waterpunten_niet(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    _fake_http(monkeypatch, model_status=[503, 503, 503])
    body = _post(client, sample_gpx, weather="true", departure=_later(4).strftime("%Y-%m-%dT%H:%M"))
    assert body["weather_checked"] is True
    assert "mislukt" in body["weather_error"]
    assert body["stats"]["water_point_count"] >= 1


def test_standaardsnelheid_is_30(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    _fake_http(monkeypatch)
    body = _post(client, sample_gpx, weather="true", departure=_later(4).strftime("%Y-%m-%dT%H:%M"))
    assert body["weather_speed_kmh"] == 30


@pytest.mark.parametrize(
    "fields, detail",
    [
        ({"departure": "gisteren"}, "Ongeldige vertrektijd"),
        ({"departure": "2020-01-01T10:00"}, "verleden"),
        ({"departure": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")}, "maximaal"),
        ({"speed_kmh": "120"}, "snelheid"),
    ],
)
def test_ongeldige_invoer(client: TestClient, sample_gpx: str, fields: dict, detail: str) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data={"weather": "true", **fields},
    )
    assert response.status_code == 400
    assert detail in response.json()["detail"]


@pytest.mark.parametrize("path", ["/", "/routeboek"])
def test_paginas_tonen_regenoptie(client: TestClient, path: str) -> None:
    html = client.get(path).text
    assert 'id="weather"' in html
    assert 'id="departure"' in html
    assert 'id="speed_kmh"' in html and 'value="30"' in html
    assert "weather.js" in html


def test_regenoptie_verborgen_als_uitgeschakeld(monkeypatch, client: TestClient) -> None:
    monkeypatch.setattr(get_settings(), "weather_enabled", False)
    assert 'id="weather"' not in client.get("/").text


def test_routeboek_geeft_regenparameters_door(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    from tests.test_routeboek import _stub_http

    with routeboek_service._lock:
        routeboek_service._cache.clear()
        routeboek_service._cache_at = 0.0
    _stub_http(monkeypatch, gpx_bytes=sample_gpx.encode())
    # _stub_http vervangt requests.get voor de GPX; weer apart afvangen
    gpx_get = routeboek_service.requests.get
    calls = {"model": 0}

    def fake_get(url, params=None, **kwargs):
        if "open-meteo" in url:
            calls["model"] += 1
            return _Resp(json_data=_model_payload(params, None, None))
        return gpx_get(url, params=params, **kwargs)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    response = client.post(
        "/api/routeboek/process",
        json={
            "route_ids": ["5"], "radius": 500, "source": "nl",
            "weather": True, "departure": _later(4).strftime("%Y-%m-%dT%H:%M"), "speed_kmh": 25,
        },
    )
    assert response.status_code == 200
    result = response.json()[0]["result"]
    assert result["weather_checked"] and result["weather_speed_kmh"] == 25
    assert calls["model"] == 1
