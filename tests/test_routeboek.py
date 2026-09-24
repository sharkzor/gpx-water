"""Tests voor de routeboek.cc-integratie (scrapen en verwerken) — volledig offline."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import routeboek_service, waterpoints_nl
from app.services.routeboek_service import RouteboekError

_OVERVIEW_HTML = """
<div id="route_5" class="routeitem">
<a href="/club/stampers/route/30k-trainingsrondje-dirk">30k Trainingsrondje Dirk</a>
<div class="routedatawrapper">
<span class="dataitem distance"><span class="bikeicon road">x</span>29,6 km</span>
<span class="dataitem hoogte"><span>x</span>11</span>
<div class="stars" style="width: 70px"></div>
</div>
</div>
<div id="route_209" class="routeitem">
<a href="/club/stampers/route/rondje-graaf">Rondje Graaf</a>
<div class="routedatawrapper">
<span class="dataitem distance"><span class="bikeicon road">x</span>42,6 km</span>
<span class="dataitem hoogte"><span>x</span>22</span>
</div>
</div>
"""

_DETAIL_HTML = """
<div class="btnwrap two">
<a href="../../../media/gpx/1/30k%20Trainingsrondje%20Dirk.gpx"
   id="cpContentContainer_cpContentContainer_cpContentContainer_lnkGPX"
   class="btn_icon dark xl" download="">download</a>
</div>
"""


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _routeboek_config(monkeypatch, waterpoints_gpx: str):
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)
    with routeboek_service._lock:
        routeboek_service._cache.clear()
        routeboek_service._cache_at = 0.0
    yield
    with routeboek_service._lock:
        routeboek_service._cache.clear()
        routeboek_service._cache_at = 0.0


def _stub_http(monkeypatch, gpx_bytes: bytes | None = None) -> None:
    """Vervang alle netwerkverkeer door vaste HTML/GPX-inhoud."""

    def fake_get(url: str) -> str:
        if "/route/" in url:
            return _DETAIL_HTML
        return _OVERVIEW_HTML

    monkeypatch.setattr(routeboek_service, "_get", fake_get)

    if gpx_bytes is not None:
        class _Resp:
            status_code = 200
            content = gpx_bytes

        monkeypatch.setattr(routeboek_service.requests, "get", lambda *a, **k: _Resp())


def test_lijst_scrapet_routes_uit_overzichtspagina(monkeypatch) -> None:
    _stub_http(monkeypatch)
    routes = routeboek_service.list_routes()
    assert len(routes) == 2
    first = routes[0]
    assert first.id == "5"
    assert first.slug == "30k-trainingsrondje-dirk"
    assert first.name == "30k Trainingsrondje Dirk"
    assert first.distance_km == pytest.approx(29.6)
    assert first.elevation_m == 11
    assert first.rating == pytest.approx(3.5)
    assert routes[1].rating is None


def test_lijst_gebruikt_korte_cache(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_get(url: str) -> str:
        calls["n"] += 1
        return _OVERVIEW_HTML

    monkeypatch.setattr(routeboek_service, "_get", fake_get)
    routeboek_service.list_routes()
    routeboek_service.list_routes()
    assert calls["n"] == 1
    routeboek_service.list_routes(force_refresh=True)
    assert calls["n"] == 2


def test_export_gpx_haalt_download_link_van_detailpagina(monkeypatch, sample_gpx: str) -> None:
    _stub_http(monkeypatch, gpx_bytes=sample_gpx.encode("utf-8"))
    content = routeboek_service.export_gpx("30k-trainingsrondje-dirk")
    assert content.startswith(b"<?xml")


def test_export_gpx_zonder_download_link_en_kaart_geeft_fout(monkeypatch) -> None:
    monkeypatch.setattr(routeboek_service, "_get", lambda url: "<html>geen link</html>")
    with pytest.raises(RouteboekError):
        routeboek_service.export_gpx("onbekende-route")


_MAP_HTML = _DETAIL_HTML + """
<title>Routeboek.cc - Gravel: Huize Scherpenzeel  V2</title>
<script>var path = [{ lat: 52.08755, lng: 5.04676 }, { lat: 52.08836, lng: 5.04611 },
{ lat: 52.09000, lng: 5.05000 }];</script>
"""


def test_ontbrekend_gpx_bestand_valt_terug_op_kaart(monkeypatch) -> None:
    """Gemeten op routeboek.cc: de GPX-link van één route geeft 404."""
    monkeypatch.setattr(routeboek_service, "_get", lambda url: _MAP_HTML)

    class _NotFound:
        status_code = 404
        content = b"not found"

    monkeypatch.setattr(routeboek_service.requests, "get", lambda *a, **k: _NotFound())
    content = routeboek_service.export_gpx("gravel-huize-scherpenzeel-v2")
    assert b"<gpx" in content
    assert content.count(b"<trkpt") == 3
    assert b"Gravel: Huize Scherpenzeel" in content
    assert b"ontbreekt" in content


def test_pagina_toont_zoekveld_en_navigatie(client: TestClient) -> None:
    response = client.get("/routeboek")
    assert response.status_code == 200
    assert 'id="route-search"' in response.text
    assert "routeboek.cc" in response.text


def test_index_pagina_toont_link_naar_routeboek(client: TestClient) -> None:
    response = client.get("/")
    assert 'href="/routeboek"' in response.text


def test_disabled_feature_geeft_404(monkeypatch, client: TestClient) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "routeboek_enabled", False)
    response = client.get("/routeboek")
    assert response.status_code == 404


def test_routes_endpoint_geeft_lijst(monkeypatch, client: TestClient) -> None:
    _stub_http(monkeypatch)
    response = client.get("/api/routeboek/routes")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["name"] == "30k Trainingsrondje Dirk"


def test_process_downloadt_en_verwerkt_route(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    _stub_http(monkeypatch, gpx_bytes=sample_gpx.encode("utf-8"))
    response = client.post(
        "/api/routeboek/process",
        json={"route_ids": ["5"], "radius": 500, "source": "nl"},
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["error"] is None
    assert items[0]["result"]["stats"]["water_point_count"] >= 0


def test_process_zonder_selectie_geeft_400(client: TestClient) -> None:
    response = client.post("/api/routeboek/process", json={"route_ids": []})
    assert response.status_code == 400


def test_process_onbekende_route_geeft_fout_per_item(monkeypatch, client: TestClient) -> None:
    _stub_http(monkeypatch)
    response = client.post(
        "/api/routeboek/process",
        json={"route_ids": ["999999"], "radius": 500},
    )
    assert response.status_code == 200
    items = response.json()
    assert items[0]["error"]


def test_process_alleen_controles_zonder_waterpunten(monkeypatch, client: TestClient, sample_gpx: str) -> None:
    from app.services import roadworks_nl

    _stub_http(monkeypatch, gpx_bytes=sample_gpx.encode("utf-8"))
    monkeypatch.setattr(roadworks_nl, "load_road_works_near", lambda *a, **k: [])
    response = client.post(
        "/api/routeboek/process",
        json={"route_ids": ["5"], "water": False, "roadworks": True},
    )
    item = response.json()[0]
    assert item["error"] is None
    assert item["result"]["water_checked"] is False
    assert item["result"]["filename"] == "30k-Trainingsrondje-Dirk_gecontroleerd.gpx"


def test_process_zonder_enige_controle_geeft_400(client: TestClient) -> None:
    response = client.post("/api/routeboek/process", json={"route_ids": ["5"], "water": False})
    assert response.status_code == 400
    assert "minimaal één controle" in response.json()["detail"]
