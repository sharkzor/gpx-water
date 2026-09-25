"""Tests voor de HTTP-API en de webinterface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import gpx_service, osm_service, waterpoints_nl
from app.models.schemas import WaterPoint


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, waterpoints_gpx: str) -> None:
    """Geen netwerkverkeer tijdens tests."""
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)
    monkeypatch.setattr(
        osm_service,
        "load_water_points_near",
        lambda coords, radius: [
            WaterPoint(lat=52.35, lon=4.9005, name="OSM tap", source="openstreetmap")
        ],
    )


def test_index_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "GPX Drinkwaterpunten" in response.text
    assert "750 meter" in response.text


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_process_and_download(client: TestClient, sample_gpx: str) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("mijn route.gpx", sample_gpx, "application/gpx+xml")},
        data={"radius": "500", "source": "auto"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["source"] == "drinkwaterpunten.nl"
    assert payload["nl_share"] == 1.0
    assert payload["stats"]["water_point_count"] == 2
    assert payload["stats"]["total_distance_km"] == pytest.approx(11.1, abs=0.3)
    assert len(payload["route"]) == 101
    assert payload["filename"] == "mijn route-water.gpx"
    assert payload["water_points"][0]["along_route_km"] < payload["water_points"][1]["along_route_km"]

    download = client.get(f"/api/download/{payload['job_id']}?name=test.gpx")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/gpx")
    reparsed = gpx_service.parse_gpx(download.content)
    assert len(reparsed.waypoints) == 2
    assert len(gpx_service.extract_route_points(reparsed)) == 101


def test_process_with_osm_source(client: TestClient, sample_gpx: str) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx)},
        data={"radius": "1000", "source": "osm"},
    )
    payload = response.json()
    assert payload["source"] == "openstreetmap"
    assert payload["water_points"][0]["name"] == "OSM tap"


def test_invalid_radius(client: TestClient, sample_gpx: str) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx)},
        data={"radius": "300"},
    )
    assert response.status_code == 400


def test_invalid_gpx(client: TestClient) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", "geen gpx")},
        data={"radius": "500"},
    )
    assert response.status_code == 400


def test_download_unknown_job(client: TestClient) -> None:
    assert client.get("/api/download/deadbeef").status_code == 404


def test_radius_options_and_default(client: TestClient, sample_gpx: str) -> None:
    page = client.get("/").text
    assert "100 meter" in page
    assert '<option value="250" selected>' in page

    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx)},
        data={"radius": "100"},
    )
    assert response.status_code == 200
    assert response.json()["radius_m"] == 100

    # zonder expliciete radius wordt de standaard (250 m) gebruikt
    fallback = client.post("/api/process", files={"file": ("route.gpx", sample_gpx)})
    assert fallback.json()["radius_m"] == 250


def test_beveiligingsheaders_en_lokale_leaflet(client: TestClient) -> None:
    response = client.get("/")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "unpkg.com" not in response.text
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code == 200
    assert "Content-Security-Policy" not in client.get("/docs").headers
