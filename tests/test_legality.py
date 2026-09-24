"""Tests voor de controle op verboden paden (lokale OSM-kaart), volledig offline."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import legality, osm_index, waterpoints_nl

LON = 4.9


def _way(way_id: int, tags: dict, coords: list[tuple[float, float]]):
    """Weg zoals `osm_index.ways_in_bbox` hem teruggeeft: (id, tags, [(lat, lon)])."""
    return (way_id, tags, coords)


def _fake_map(monkeypatch, ways) -> None:
    """Vervang de lokale kaart door een vaste set wegen."""
    monkeypatch.setattr(osm_index, "ways_in_bbox", lambda *box: list(ways))
    monkeypatch.setattr(
        osm_index,
        "status",
        lambda: osm_index.IndexStatus(available=True, way_count=len(ways), built_at=None),
    )


@pytest.fixture(autouse=True)
def _offline(monkeypatch, waterpoints_gpx: str):
    get_settings().ensure_dirs()
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)
    waterpoints_nl.load_water_points(force_refresh=True)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _route(lat_from: float, lat_to: float, steps: int = 50) -> list[tuple[float, float]]:
    return [
        (lat_from + (lat_to - lat_from) * i / steps, LON) for i in range(steps + 1)
    ]


# -- Regels -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"highway": "footway"}, ("forbidden", "footway")),
        ({"highway": "footway", "bicycle": "yes"}, None),
        ({"highway": "footway", "footway": "sidewalk"}, None),
        ({"highway": "cycleway"}, None),
        ({"highway": "residential", "bicycle": "no"}, ("forbidden", "bicycle_no")),
        ({"highway": "primary", "motorroad": "yes", "bicycle": "yes"}, ("forbidden", "motorway")),
        ({"highway": "service", "access": "private"}, ("forbidden", "access_private")),
        ({"highway": "pedestrian"}, ("warning", "pedestrian")),
        ({"highway": "path"}, None),
        ({"highway": "path", "foot": "designated"}, ("warning", "path_foot")),
    ],
)
def test_classify(tags: dict, expected) -> None:
    verdict = legality.classify(tags)
    assert (verdict[:2] if verdict else None) == expected


# -- Controle -----------------------------------------------------------------


def test_voetpad_zonder_alternatief_wordt_gemeld(monkeypatch) -> None:
    _fake_map(monkeypatch, [_way(1, {"highway": "footway", "name": "Parkpad"}, _route(52.30, 52.31))])
    report = legality.check_route(_route(52.30, 52.31))
    assert report.forbidden_count == 1
    segment = report.segments[0]
    assert segment.label == "Voetpad"
    assert segment.way_name == "Parkpad"
    assert segment.length_m > 900


def test_voetpad_naast_gewone_weg_geeft_geen_melding(monkeypatch) -> None:
    """Een route over een gewone weg met een parallel voetpad is prima."""
    _fake_map(
        monkeypatch,
        [
            _way(1, {"highway": "footway"}, _route(52.30, 52.31)),
            _way(2, {"highway": "residential"}, [(lat, LON + 0.00005) for lat, _ in _route(52.30, 52.31)]),
        ],
    )
    assert legality.check_route(_route(52.30, 52.31)).segments == []


def test_inrit_breekt_een_lang_verboden_stuk_niet(monkeypatch) -> None:
    """Zelfde probleem als de Lekdijk in routeboek: een zijweg mag geen twee meldingen geven."""
    _fake_map(
        monkeypatch,
        [
            _way(1, {"highway": "footway"}, _route(52.30, 52.32)),
            _way(2, {"highway": "service"}, [(52.31, LON - 0.001), (52.31, LON + 0.001)]),
        ],
    )
    report = legality.check_route(_route(52.30, 52.32))
    assert len(report.segments) == 1
    assert report.segments[0].length_m > 2000


# -- Lokale kaart -------------------------------------------------------------


def test_kaart_opbouwen_en_bevragen(tmp_path, monkeypatch) -> None:
    geojson = tmp_path / "wegen.geojsonseq"
    features = [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[4.90, 52.30], [4.90, 52.31]]},
         "properties": {"@id": 11, "highway": "footway", "surface": "paved"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[5.50, 52.00], [5.51, 52.00]]},
         "properties": {"@id": 12, "highway": "residential"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[4.0, 52.0], [4.1, 52.0]]},
         "properties": {"@id": 13, "waterway": "ditch"}},  # geen weg
    ]
    geojson.write_text("".join("\x1e" + json.dumps(f) + "\n" for f in features), encoding="utf-8")
    db = tmp_path / "netherlands.sqlite"
    monkeypatch.setattr(osm_index, "_db_path", lambda: db)
    osm_index._reader_cache.clear()

    assert osm_index._build_sqlite(geojson, db, lambda *_: None) == 2
    state = osm_index.status()
    assert state.available and state.way_count == 2 and not state.stale

    found = osm_index.ways_in_bbox(52.29, 4.89, 52.32, 4.91)
    assert [(w[0], w[1]) for w in found] == [(11, {"highway": "footway"})]  # surface is weggegooid
    assert found[0][2][0] == pytest.approx((52.30, 4.90))
    osm_index._reader_cache.clear()


def test_niet_bouwen_bij_te_weinig_geheugen(monkeypatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(osm_index, "status", lambda: osm_index.IndexStatus(available=False))
    monkeypatch.setattr(osm_index, "current_job", lambda: None)
    monkeypatch.setattr(osm_index, "free_memory_mb", lambda: 1000)
    monkeypatch.setattr(osm_index, "start_refresh", lambda: started.append(True))
    assert osm_index.refresh_if_needed() == "low_memory"
    assert started == []

    monkeypatch.setattr(osm_index, "free_memory_mb", lambda: 6000)
    assert osm_index.refresh_if_needed() == "started"
    assert started == [True]


def test_verse_kaart_wordt_niet_opnieuw_gebouwd(monkeypatch) -> None:
    import time

    monkeypatch.setattr(
        osm_index, "status", lambda: osm_index.IndexStatus(available=True, built_at=time.time() - 86400)
    )
    monkeypatch.setattr(osm_index, "current_job", lambda: None)
    monkeypatch.setattr(osm_index, "start_refresh", lambda: pytest.fail("mag niet bouwen"))
    assert osm_index.refresh_if_needed() == "fresh"


# -- Integratie in de verwerking -----------------------------------------------


def test_upload_met_controle_levert_segment_en_waypoint(
    client: TestClient, sample_gpx: str, monkeypatch
) -> None:
    footway = [(52.35 + i * 0.0005, LON) for i in range(11)]  # km ~5,5 tot ~6,1
    road_before = [(52.30 + i * 0.0005, LON) for i in range(101)]
    road_after = [(52.355 + i * 0.0005, LON) for i in range(91)]
    _fake_map(
        monkeypatch,
        [
            _way(1, {"highway": "footway", "name": "Stadspark"}, footway),
            _way(2, {"highway": "residential"}, road_before[:-3]),
            _way(3, {"highway": "residential"}, road_after[3:]),
        ],
    )
    response = client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data={"radius": "250", "source": "nl", "legality": "true"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["legality_checked"] is True
    assert data["legality_error"] is None
    segments = data["legality_segments"]
    assert len(segments) == 1
    assert segments[0]["severity"] == "forbidden"
    assert segments[0]["way_name"] == "Stadspark"
    assert 5.0 < segments[0]["start_km"] < 6.0
    assert data["stats"]["water_point_count"] == 2

    gpx = client.get(f"/api/download/{data['job_id']}").text
    assert "\u26d4 Verboden - 6 km" in gpx or "\u26d4 Verboden - 5 km" in gpx
    assert "<type>Forbidden</type>" in gpx
    assert "Voetpad | Stadspark" in gpx


def test_zonder_optie_wordt_niet_gecontroleerd(
    client: TestClient, sample_gpx: str, monkeypatch
) -> None:
    monkeypatch.setattr(legality, "check_route", lambda *a: pytest.fail("mag niet draaien"))
    data = client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data={"radius": "250", "source": "nl"},
    ).json()
    assert data["legality_checked"] is False
    assert data["legality_segments"] == []


def test_ontbrekende_kaart_blokkeert_waterpunten_niet(
    client: TestClient, sample_gpx: str, monkeypatch
) -> None:
    monkeypatch.setattr(osm_index, "status", lambda: osm_index.IndexStatus(available=False))
    monkeypatch.setattr(osm_index, "current_job", lambda: None)
    data = client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data={"radius": "250", "source": "nl", "legality": "true"},
    ).json()
    assert "nog niet beschikbaar" in data["legality_error"]
    assert data["stats"]["water_point_count"] == 2


def test_buitenlandse_route_wordt_niet_gecontroleerd(client: TestClient, monkeypatch) -> None:
    from app.services import osm_service

    monkeypatch.setattr(osm_service, "load_water_points_near", lambda *a: [])
    monkeypatch.setattr(legality, "check_route", lambda *a: pytest.fail("mag niet draaien"))
    points = "".join(f'<trkpt lat="{50.80 + i * 0.001:.4f}" lon="4.35"/>' for i in range(20))
    gpx = f'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{points}</trkseg></trk></gpx>'
    data = client.post(
        "/api/process",
        files={"file": ("brussel.gpx", gpx, "application/gpx+xml")},
        data={"radius": "250", "source": "osm", "legality": "true"},
    ).json()
    assert "alleen beschikbaar voor Nederland" in data["legality_error"]


def test_pagina_toont_optie(client: TestClient) -> None:
    assert "Controleer op verboden paden" in client.get("/").text


def test_uitschakelen_via_config(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "legality_enabled", False)
    assert "Controleer op verboden paden" not in client.get("/").text
    assert client.get("/api/osm/status").status_code == 404


def test_status_endpoint(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(osm_index, "status", lambda: osm_index.IndexStatus(available=False))
    body = client.get("/api/osm/status").json()
    assert body["available"] is False
    assert client.get("/api/health").json()["osm_map_available"] is False
