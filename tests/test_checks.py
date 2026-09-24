"""Tests voor losse controles: waterpunten zijn optioneel, minimaal één controle."""

from __future__ import annotations

import gpxpy
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import osm_service, roadworks_nl, waterpoints_nl


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, waterpoints_gpx: str) -> None:
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)


def _post(client: TestClient, sample_gpx: str, **data: str):
    return client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data=data,
    )


def test_zonder_enige_controle_geeft_400(client: TestClient, sample_gpx: str) -> None:
    response = _post(client, sample_gpx, water="false")
    assert response.status_code == 400
    assert "minimaal één controle" in response.json()["detail"]


def test_alleen_wegwerkzaamheden_zoekt_geen_waterpunten(
    monkeypatch, client: TestClient, sample_gpx: str
) -> None:
    def forbidden(*_a, **_k):
        raise AssertionError("waterpunten mogen niet worden opgehaald")

    monkeypatch.setattr(waterpoints_nl, "load_water_points_near", forbidden)
    monkeypatch.setattr(osm_service, "load_water_points_near", forbidden)
    monkeypatch.setattr(roadworks_nl, "load_road_works_near", lambda *a, **k: [])

    response = _post(client, sample_gpx, water="false", roadworks="true")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["water_checked"] is False
    assert payload["roadworks_checked"] is True
    assert payload["water_points"] == []
    assert payload["source"] == ""
    assert payload["stats"]["warning"] is None
    assert payload["stats"]["total_distance_km"] > 10
    assert payload["filename"] == "rit-gecontroleerd.gpx"

    gpx = gpxpy.parse(client.get(f"/api/download/{payload['job_id']}").text)
    assert gpx.waypoints == []
    assert gpx.tracks or gpx.routes


def test_waterpunten_staan_standaard_aan(client: TestClient, sample_gpx: str) -> None:
    payload = _post(client, sample_gpx, radius="500").json()
    assert payload["water_checked"] is True
    assert payload["filename"] == "rit-water.gpx"


def test_uploadpagina_toont_controles(client: TestClient) -> None:
    html = client.get("/").text
    assert "Controleer route" in html
    for box in ('id="water"', 'id="roadworks"', 'id="legality"', 'id="weather"'):
        assert box in html
    assert "/static/checks.js" in html
    assert html.count("data-check") == 4
