"""Tests voor de Strava-integratie (OAuth, routes, verwerking) — volledig offline."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models.schemas import StravaRouteInfo
from app.services import gpx_service, strava_service, token_store, waterpoints_nl


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _strava_config(monkeypatch, waterpoints_gpx: str):
    """Configureer een neptoepassing en houd al het netwerkverkeer weg."""
    settings = get_settings()
    monkeypatch.setattr(settings, "strava_client_id", "12345")
    monkeypatch.setattr(settings, "strava_client_secret", "geheim")
    monkeypatch.setattr(settings, "strava_redirect_uri", "http://testserver/strava/callback")
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)
    yield


def _fake_token(expires_in: float = 3600.0) -> dict:
    return {
        "access_token": "access-abc",
        "refresh_token": "refresh-xyz",
        "expires_at": time.time() + expires_in,
        "scope": "read,read_all",
        "athlete": {"id": 42, "firstname": "Tess", "lastname": "Fietser", "username": "tess"},
    }


def _connect(client: TestClient, monkeypatch, token: dict | None = None) -> None:
    """Doorloop de OAuth-flow met een neptokenserver."""
    monkeypatch.setattr(strava_service, "_post_token", lambda payload: token or _fake_token())
    redirect = client.get("/strava/connect", follow_redirects=False)
    assert redirect.status_code == 307
    state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]
    done = client.get(f"/strava/callback?code=code123&state={state}", follow_redirects=False)
    assert done.status_code == 303
    assert done.headers["location"] == "/strava?connected=1"


def test_strava_page_renders(client: TestClient) -> None:
    response = client.get("/strava")
    assert response.status_code == 200
    assert "Verbind met Strava" in response.text
    assert "Controleer route" in response.text
    assert "_checks" not in response.text and 'id="water"' in response.text


def test_navigation_on_upload_page(client: TestClient) -> None:
    assert 'href="/strava"' in client.get("/").text


def test_status_when_not_connected(client: TestClient) -> None:
    payload = client.get("/api/strava/status").json()
    assert payload == {
        "configured": True,
        "connected": False,
        "athlete": None,
        "scope": None,
        "mode": None,
        "oauth_available": True,
    }


def test_connect_redirects_to_strava(client: TestClient) -> None:
    response = client.get("/strava/connect", follow_redirects=False)
    assert response.status_code == 307
    url = urlparse(response.headers["location"])
    query = parse_qs(url.query)
    assert url.netloc == "www.strava.com"
    assert query["client_id"] == ["12345"]
    assert query["scope"] == ["read,read_all"]
    assert query["redirect_uri"] == ["http://testserver/strava/callback"]
    assert query["state"]


def test_connect_requires_configuration(client: TestClient, monkeypatch) -> None:
    """Module aan, maar credentials ontbreken: nette 503 in plaats van 404."""
    monkeypatch.setattr(get_settings(), "strava_setting", "true")
    monkeypatch.setattr(get_settings(), "strava_client_id", "")
    assert client.get("/strava/connect", follow_redirects=False).status_code == 503


def test_oauth_callback_stores_tokens_encrypted(client: TestClient, monkeypatch) -> None:
    _connect(client, monkeypatch)

    status = client.get("/api/strava/status").json()
    assert status["connected"] is True
    assert status["athlete"]["firstname"] == "Tess"
    assert status["scope"] == "read,read_all"

    store = token_store._store_file()
    assert store.exists()
    assert oct(store.stat().st_mode)[-3:] == "600"
    assert b"access-abc" not in store.read_bytes()  # versleuteld opgeslagen


def test_callback_rejects_tampered_state(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(strava_service, "_post_token", lambda payload: _fake_token())
    response = client.get("/strava/callback?code=x&state=vervalst", follow_redirects=False)
    assert response.headers["location"] == "/strava?error=ongeldige_state"
    assert client.get("/api/strava/status").json()["connected"] is False


def test_callback_handles_denied_access(client: TestClient) -> None:
    response = client.get("/strava/callback?error=access_denied", follow_redirects=False)
    assert response.headers["location"] == "/strava?error=access_denied"


def test_routes_require_connection(client: TestClient) -> None:
    assert client.get("/api/strava/routes").status_code == 401
    assert client.post("/api/strava/process", json={"route_ids": ["1"]}).status_code == 401


def test_list_routes(client: TestClient, monkeypatch) -> None:
    _connect(client, monkeypatch)

    class FakeResponse:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        if params and params.get("page", 1) > 1:
            return FakeResponse([])
        return FakeResponse(
            [
                {
                    "id": 987,
                    "id_str": "987",
                    "name": "Rondje Utrecht",
                    "distance": 42195.0,
                    "elevation_gain": 123.4,
                    "type": 1,
                    "private": False,
                }
            ]
        )

    monkeypatch.setattr(strava_service.requests, "get", fake_get)
    routes = client.get("/api/strava/routes").json()

    assert routes == [
        {
            "id": "987",
            "name": "Rondje Utrecht",
            "distance_km": 42.2,
            "elevation_gain_m": 123.0,
            "type": "Ride",
            "private": False,
            "url": "https://www.strava.com/routes/987",
        }
    ]
    assert calls[0]["url"].endswith("/athlete/routes")
    assert calls[0]["headers"]["Authorization"] == "Bearer access-abc"


def test_expired_token_is_refreshed(client: TestClient, monkeypatch) -> None:
    _connect(client, monkeypatch, token=_fake_token(expires_in=-10))

    refreshed = _fake_token()
    refreshed["access_token"] = "access-nieuw"
    calls: list[dict] = []

    def fake_post_token(payload):
        calls.append(payload)
        return refreshed

    monkeypatch.setattr(strava_service, "_post_token", fake_post_token)
    monkeypatch.setattr(
        strava_service, "list_routes", lambda session: [] if session["access_token"] == "access-nieuw" else ["fout"]
    )

    assert client.get("/api/strava/routes").json() == []
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "refresh-xyz"


def test_process_selected_routes(client: TestClient, monkeypatch, sample_gpx: str) -> None:
    _connect(client, monkeypatch)
    monkeypatch.setattr(
        strava_service,
        "list_routes",
        lambda session: [
            StravaRouteInfo(
                id="987",
                name="Rondje Utrecht",
                distance_km=11.1,
                elevation_gain_m=12,
                type="Ride",
                private=False,
                url="https://www.strava.com/routes/987",
            )
        ],
    )
    monkeypatch.setattr(
        strava_service, "export_gpx", lambda session, route_id: sample_gpx.encode()
    )

    response = client.post(
        "/api/strava/process",
        json={"route_ids": ["987"], "radius": 500, "source": "auto"},
    )
    assert response.status_code == 200, response.text
    (item,) = response.json()

    assert item["error"] is None
    assert item["name"] == "Rondje Utrecht"
    assert item["original_filename"] == "Rondje-Utrecht.gpx"
    assert item["result"]["filename"] == "Rondje-Utrecht_waterpunten.gpx"
    assert item["result"]["stats"]["water_point_count"] == 2
    assert item["result"]["source"] == "drinkwaterpunten.nl"

    original = client.get(f"/api/download/{item['original_job_id']}?name={item['original_filename']}")
    enriched = client.get(f"/api/download/{item['result']['job_id']}?name={item['result']['filename']}")
    assert original.status_code == enriched.status_code == 200
    assert len(gpx_service.parse_gpx(original.content).waypoints) == 0
    enriched_gpx = gpx_service.parse_gpx(enriched.content)
    assert len(enriched_gpx.waypoints) == 2
    assert enriched_gpx.waypoints[0].symbol == "Water Source"
    assert len(gpx_service.extract_route_points(enriched_gpx)) == 101


def test_process_reports_error_per_route(client: TestClient, monkeypatch) -> None:
    _connect(client, monkeypatch)
    monkeypatch.setattr(strava_service, "list_routes", lambda session: [])

    def boom(session, route_id):
        raise strava_service.StravaError("Route bestaat niet")

    monkeypatch.setattr(strava_service, "export_gpx", boom)
    (item,) = client.post("/api/strava/process", json={"route_ids": ["1"]}).json()
    assert item["error"] == "Route bestaat niet"
    assert item["result"] is None


def test_process_requires_selection(client: TestClient, monkeypatch) -> None:
    _connect(client, monkeypatch)
    assert client.post("/api/strava/process", json={"route_ids": []}).status_code == 400


def test_disconnect_removes_tokens(client: TestClient, monkeypatch) -> None:
    _connect(client, monkeypatch)
    assert client.post("/api/strava/disconnect").json() == {"status": "ok"}
    assert client.get("/api/strava/status").json()["connected"] is False
    assert client.get("/api/strava/routes").status_code == 401


def _fake_api(monkeypatch, routes_payload=None, athlete=None):
    class FakeResponse:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_get(url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer access-env"
        if url.endswith("/athlete"):
            return FakeResponse(athlete or {"id": 7, "firstname": "Env", "lastname": "Token"})
        if params and params.get("page", 1) > 1:
            return FakeResponse([])
        return FakeResponse(routes_payload or [])

    monkeypatch.setattr(strava_service.requests, "get", fake_get)


def test_env_token_connects_without_oauth(client: TestClient, monkeypatch) -> None:
    """Bestaande client id/secret + refresh token: koppelen via de knop is niet nodig."""
    settings = get_settings()
    monkeypatch.setattr(settings, "strava_redirect_uri", "")
    monkeypatch.setattr(settings, "strava_refresh_token", "refresh-uit-env")
    monkeypatch.setattr(settings, "strava_access_token", "")
    token_store.delete(strava_service.ENV_SESSION_ID)

    refreshed = _fake_token()
    refreshed["access_token"] = "access-env"
    refreshed.pop("athlete")
    calls: list[dict] = []

    def fake_post_token(payload):
        calls.append(payload)
        return refreshed

    monkeypatch.setattr(strava_service, "_post_token", fake_post_token)
    _fake_api(
        monkeypatch,
        routes_payload=[
            {"id": 5, "name": "Envroute", "distance": 10000, "elevation_gain": 50, "type": 1}
        ],
    )

    assert settings.strava_enabled is True
    assert settings.strava_oauth_enabled is False

    routes = client.get("/api/strava/routes").json()
    assert routes[0]["name"] == "Envroute"
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "refresh-uit-env"

    status = client.get("/api/strava/status").json()
    assert status["connected"] is True
    assert status["mode"] == "env"
    assert status["oauth_available"] is False
    assert status["athlete"]["firstname"] == "Env"

    token_store.delete(strava_service.ENV_SESSION_ID)


def test_env_token_survives_disconnect(client: TestClient, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "strava_refresh_token", "refresh-uit-env")
    token_store.delete(strava_service.ENV_SESSION_ID)

    refreshed = _fake_token()
    refreshed["access_token"] = "access-env"
    refreshed.pop("athlete")
    monkeypatch.setattr(strava_service, "_post_token", lambda payload: refreshed)
    _fake_api(monkeypatch)

    assert client.get("/api/strava/routes").json() == []
    body = client.post("/api/strava/disconnect").json()
    assert "vast token" in body["note"]
    assert client.get("/api/strava/status").json()["connected"] is True

    token_store.delete(strava_service.ENV_SESSION_ID)


def test_without_redirect_uri_connect_is_unavailable(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "strava_redirect_uri", "")
    assert client.get("/strava/connect", follow_redirects=False).status_code == 503


def test_strava_uitschakelbaar_via_env(client: TestClient, monkeypatch) -> None:
    """STRAVA_ENABLED=false verwijdert de hele module, ook mét credentials."""
    settings = get_settings()
    monkeypatch.setattr(settings, "strava_setting", "false")

    assert settings.strava_feature_enabled is False
    assert settings.strava_credentials is False
    assert settings.strava_enabled is False
    assert settings.strava_oauth_enabled is False
    assert settings.strava_token_configured is False

    assert client.get("/strava").status_code == 404
    assert client.get("/api/strava/status").status_code == 404
    assert client.get("/api/strava/routes").status_code == 404
    assert client.get("/strava/connect", follow_redirects=False).status_code == 404
    assert client.post("/api/strava/process", json={"route_ids": ["1"]}).status_code == 404

    index = client.get("/")
    assert index.status_code == 200
    assert "/strava" not in index.text

    health = client.get("/api/health").json()
    assert health["strava_enabled"] is False


def test_strava_schakelaar_standen(monkeypatch) -> None:
    settings = get_settings()

    monkeypatch.setattr(settings, "strava_setting", "auto")
    assert settings.strava_feature_enabled is True

    monkeypatch.setattr(settings, "strava_client_id", "")
    assert settings.strava_feature_enabled is False

    monkeypatch.setattr(settings, "strava_setting", "true")
    assert settings.strava_feature_enabled is True
    # Geforceerd aan zonder credentials blijft onbruikbaar, maar niet stuk.
    assert settings.strava_enabled is False


def test_env_token_wordt_genegeerd_als_strava_uit_staat(monkeypatch) -> None:
    """Belangrijk voor de publieke installatie: geen sessie uit environment."""
    settings = get_settings()
    monkeypatch.setattr(settings, "strava_refresh_token", "refresh-uit-env")
    assert settings.strava_token_configured is True

    monkeypatch.setattr(settings, "strava_setting", "false")
    assert settings.strava_token_configured is False
    assert strava_service.env_session() is None


def _stub_route(monkeypatch, sample_gpx: str, route_id: str, name: str = "Testrit") -> None:
    """Laat de Strava-service een vaste route met vaste GPX teruggeven."""
    monkeypatch.setattr(
        strava_service,
        "list_routes",
        lambda session: [
            StravaRouteInfo(
                id=route_id,
                name=name,
                distance_km=11.1,
                elevation_gain_m=12,
                type="Ride",
                private=False,
                url=f"https://www.strava.com/routes/{route_id}",
            )
        ],
    )
    monkeypatch.setattr(
        strava_service, "export_gpx", lambda session, rid: sample_gpx.encode()
    )


def test_strava_pagina_toont_wegwerkzaamheden_optie(client: TestClient) -> None:
    page = client.get("/strava")
    assert "Wegwerkzaamheden (alleen Nederland)" in page.text
    assert 'type="date"' in page.text


def test_strava_process_geeft_wegwerkzaamheden_door(
    client: TestClient, monkeypatch, sample_gpx: str
) -> None:
    """De Strava-batch moet dezelfde controle uitvoeren als de uploadpagina."""
    from app.models.schemas import RoadWork
    from app.services import roadworks_nl

    _connect(client, monkeypatch)
    _stub_route(monkeypatch, sample_gpx, "7")

    seen: dict = {}

    def fake_near(coords, margin, day):
        seen["day"] = day
        return [
            RoadWork(
                lat=52.3100,
                lon=4.9005,
                cause="Asfaltering",
                authority="Gemeente Testdorp",
                start="2026-08-01",
                end="2026-08-31",
            )
        ]

    monkeypatch.setattr(roadworks_nl, "load_road_works_near", fake_near)

    items = client.post(
        "/api/strava/process",
        json={
            "route_ids": ["7"],
            "radius": 250,
            "source": "nl",
            "roadworks": True,
            "ride_date": "2026-08-15",
        },
    ).json()

    result = items[0]["result"]
    assert result["roadworks_checked"] is True
    assert result["roadworks_date"] == "2026-08-15"
    assert seen["day"].isoformat() == "2026-08-15"
    assert len(result["road_works"]) == 1
    assert result["road_works"][0]["cause"] == "Asfaltering"

    gpx = client.get(f"/api/download/{result['job_id']}").text
    assert "⚠️ Werkzaamheden" in gpx


def test_strava_process_zonder_optie_slaat_controle_over(
    client: TestClient, monkeypatch, sample_gpx: str
) -> None:
    from app.services import roadworks_nl

    _connect(client, monkeypatch)
    _stub_route(monkeypatch, sample_gpx, "8")

    def unexpected(*args, **kwargs):
        raise AssertionError("wegwerkzaamheden mogen niet worden opgehaald")

    monkeypatch.setattr(roadworks_nl, "load_road_works_near", unexpected)

    items = client.post(
        "/api/strava/process", json={"route_ids": ["8"], "radius": 250, "source": "nl"}
    ).json()
    result = items[0]["result"]
    assert result["roadworks_checked"] is False
    assert result["road_works"] == []


def test_strava_process_geeft_verboden_paden_door(
    client: TestClient, monkeypatch, sample_gpx: str
) -> None:
    from app.services import legality, osm_index

    _connect(client, monkeypatch)
    _stub_route(monkeypatch, sample_gpx, "8")
    monkeypatch.setattr(osm_index, "status", lambda: osm_index.IndexStatus(available=True))
    called: list[int] = []

    def fake_check(coords):
        called.append(len(coords))
        return legality.Report(total_distance_km=11.1, forbidden_count=0, warning_count=0, segments=[])

    monkeypatch.setattr(legality, "check_route", fake_check)
    items = client.post(
        "/api/strava/process",
        json={"route_ids": ["8"], "radius": 250, "source": "nl", "legality": True},
    ).json()
    result = items[0]["result"]
    assert result["legality_checked"] is True
    assert result["legality_error"] is None
    assert called


def test_strava_pagina_toont_verboden_paden_optie(client: TestClient) -> None:
    assert "Verboden paden (alleen Nederland)" in client.get("/strava").text


def test_strava_pagina_toont_zoekveld(client: TestClient) -> None:
    assert 'id="route-search"' in client.get("/strava").text
