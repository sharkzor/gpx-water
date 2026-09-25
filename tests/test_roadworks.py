"""Tests voor de controle op wegwerkzaamheden (NDW/Melvin) — volledig offline."""

from __future__ import annotations

import gzip
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models.schemas import RoadWork
from app.services import roadworks_nl, waterpoints_nl

NS = 'xmlns="http://datex2.eu/schema/2/2_0"'


def _record(
    lat: float,
    lon: float,
    start: str,
    end: str,
    vehicle: str = "bicycle",
    cause: str = "Asfaltering",
    authority: str = "Gemeente Testdorp",
    detour: str | None = "Omleiding via de Dorpsstraat",
) -> str:
    detour_xml = (
        f"<reroutingItineraryDescription><values><value>{detour}</value>"
        "</values></reroutingItineraryDescription>"
        if detour
        else ""
    )
    return f"""
    <situation>
    <situationRecord>
      <validity><validityTimeSpecification>
        <overallStartTime>{start}T05:00:00Z</overallStartTime>
        <overallEndTime>{end}T15:00:00Z</overallEndTime>
      </validityTimeSpecification></validity>
      <source><sourceName><values><value>{authority}</value></values></sourceName></source>
      <causeDescription><values><value>{cause}</value></values></causeDescription>
      <groupOfLocations><pointCoordinates>
        <latitude>{lat}</latitude><longitude>{lon}</longitude>
      </pointCoordinates></groupOfLocations>
      <vehicleType>{vehicle}</vehicleType>
      {detour_xml}
    </situationRecord>
    </situation>"""


@pytest.fixture
def feed_xml() -> str:
    """Feed met: twee fietsmeldingen, een auto-melding en een zonder coordinaat."""
    return (
        f"<?xml version='1.0' encoding='UTF-8'?><d2LogicalModel {NS}><payloadPublication>"
        + _record(52.3100, 4.9005, "2026-08-01", "2026-08-31")
        + _record(52.3500, 4.9010, "2026-09-01", "2026-09-30", cause="Bouwactiviteiten")
        + _record(52.3200, 4.9005, "2026-08-01", "2026-08-31", vehicle="lorry")
        + """
        <situation><situationRecord>
          <vehicleType>bicycle</vehicleType>
          <causeDescription><values><value>Zonder plaats</value></values></causeDescription>
        </situationRecord></situation>"""
        + "</payloadPublication></d2LogicalModel>"
    )


@pytest.fixture(autouse=True)
def _offline(monkeypatch, feed_xml: str, waterpoints_gpx: str):
    """Vervang de NDW-download door een lokaal gzip-bestand."""
    settings = get_settings()
    settings.ensure_dirs()
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)

    def fake_download() -> object:
        path = settings.cache_dir / "ndw_test.xml.gz"
        path.write_bytes(gzip.compress(feed_xml.encode("utf-8")))
        return path

    monkeypatch.setattr(roadworks_nl, "_download", fake_download)
    for name in ("wegwerkzaamheden_fiets.json",):
        (settings.cache_dir / name).unlink(missing_ok=True)
    yield
    (settings.cache_dir / "wegwerkzaamheden_fiets.json").unlink(missing_ok=True)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_alleen_fietsmeldingen_met_coordinaat(monkeypatch) -> None:
    works = roadworks_nl.get_road_works(force_refresh=True)
    assert len(works) == 2, "auto-melding en melding zonder coordinaat moeten wegvallen"
    assert {w.cause for w in works} == {"Asfaltering", "Bouwactiviteiten"}
    first = next(w for w in works if w.cause == "Asfaltering")
    assert first.authority == "Gemeente Testdorp"
    assert first.detour == "Omleiding via de Dorpsstraat"
    assert first.start == "2026-08-01" and first.end == "2026-08-31"


def test_datumfilter() -> None:
    works = roadworks_nl.get_road_works(force_refresh=True)
    augustus = [w for w in works if roadworks_nl.is_active_on(w, date(2026, 8, 15))]
    september = [w for w in works if roadworks_nl.is_active_on(w, date(2026, 9, 15))]
    juli = [w for w in works if roadworks_nl.is_active_on(w, date(2026, 7, 15))]
    assert [w.cause for w in augustus] == ["Asfaltering"]
    assert [w.cause for w in september] == ["Bouwactiviteiten"]
    assert juli == []


def test_melding_zonder_datums_telt_altijd_mee() -> None:
    work = RoadWork(lat=52.0, lon=5.0)
    assert roadworks_nl.is_active_on(work, date(2026, 8, 4)) is True


def test_cache_wordt_hergebruikt(monkeypatch) -> None:
    roadworks_nl.get_road_works(force_refresh=True)
    calls: list[int] = []
    original = roadworks_nl._download

    def counting_download():
        calls.append(1)
        return original()

    monkeypatch.setattr(roadworks_nl, "_download", counting_download)
    roadworks_nl.get_road_works()
    assert calls == [], "verse cache mag niet opnieuw downloaden"


def test_verouderde_cache_bij_storing(monkeypatch) -> None:
    roadworks_nl.get_road_works(force_refresh=True)

    def failing():
        raise RuntimeError("NDW plat")

    monkeypatch.setattr(roadworks_nl, "_download", failing)
    works = roadworks_nl.get_road_works(force_refresh=True)
    assert len(works) == 2, "bij een storing moet de oude cache blijven werken"


def test_upload_met_wegwerkzaamheden(client: TestClient, sample_gpx: str) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx, "application/gpx+xml")},
        data={
            "radius": "250",
            "source": "nl",
            "roadworks": "true",
            "ride_date": "2026-08-15",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["roadworks_checked"] is True
    assert data["roadworks_date"] == "2026-08-15"
    assert data["roadworks_error"] is None
    assert len(data["road_works"]) == 1
    work = data["road_works"][0]
    assert work["cause"] == "Asfaltering"
    assert work["detour"] == "Omleiding via de Dorpsstraat"
    assert work["distance_to_route_m"] < 250

    gpx = client.get(f"/api/download/{data['job_id']}").text
    assert "⚠️ Werkzaamheden" in gpx
    assert "Omleiding via de Dorpsstraat" in gpx
    assert "<sym>Danger Area</sym>" in gpx


def test_upload_zonder_wegwerkzaamheden_blijft_ongewijzigd(
    client: TestClient, sample_gpx: str
) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx, "application/gpx+xml")},
        data={"radius": "250", "source": "nl"},
    )
    data = response.json()
    assert data["roadworks_checked"] is False
    assert data["road_works"] == []
    assert "Werkzaamheden" not in client.get(f"/api/download/{data['job_id']}").text


def test_ongeldige_datum(client: TestClient, sample_gpx: str) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx, "application/gpx+xml")},
        data={"source": "nl", "roadworks": "true", "ride_date": "15-08-2026"},
    )
    assert response.status_code == 400
    assert "JJJJ-MM-DD" in response.json()["detail"]


def test_storing_blokkeert_verwerking_niet(
    client: TestClient, sample_gpx: str, monkeypatch
) -> None:
    """Een NDW-storing mag de waterpunten nooit in de weg zitten."""
    def failing(*args, **kwargs):
        raise RuntimeError("NDW onbereikbaar")

    monkeypatch.setattr(roadworks_nl, "load_road_works_near", failing)
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx, "application/gpx+xml")},
        data={"source": "nl", "roadworks": "true"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["stats"]["water_point_count"] > 0
    assert "NDW onbereikbaar" in data["roadworks_error"]
    assert data["road_works"] == []


def test_buitenlandse_route_geeft_melding(client: TestClient, monkeypatch) -> None:
    from app.services import osm_service

    monkeypatch.setattr(osm_service, "load_water_points_near", lambda *a, **k: [])
    points = "".join(
        f'<trkpt lat="{45.0 + i * 0.001:.6f}" lon="7.500000"></trkpt>' for i in range(50)
    )
    gpx = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><trkseg>{points}</trkseg></trk></gpx>"
    )
    response = client.post(
        "/api/process",
        files={"file": ("italie.gpx", gpx, "application/gpx+xml")},
        data={"source": "osm", "roadworks": "true"},
    )
    assert response.status_code == 200
    assert "Nederlandse routes" in (response.json()["roadworks_error"] or "")


def test_uitschakelen_via_config(client: TestClient, sample_gpx: str, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "roadworks_enabled", False)
    response = client.post(
        "/api/process",
        files={"file": ("route.gpx", sample_gpx, "application/gpx+xml")},
        data={"source": "nl", "roadworks": "true"},
    )
    assert response.status_code == 200
    assert "uitgeschakeld" in response.json()["roadworks_error"]
    from app.routers import core

    monkeypatch.setattr(core.settings, "admin_token", "geheim")
    assert client.post("/api/roadworks/refresh", headers={"X-Admin-Token": "geheim"}).status_code == 404

    page = client.get("/")
    assert "Wegwerkzaamheden (alleen Nederland)" not in page.text


def test_pagina_toont_optie_en_datum(client: TestClient) -> None:
    page = client.get("/")
    assert "Wegwerkzaamheden (alleen Nederland)" in page.text
    assert 'type="date"' in page.text
    assert date.today().isoformat() in page.text


def test_refresh_endpoint(monkeypatch, client: TestClient) -> None:
    from app.routers import core

    monkeypatch.setattr(core.settings, "admin_token", "")
    assert client.post("/api/roadworks/refresh").status_code == 403

    monkeypatch.setattr(core.settings, "admin_token", "geheim")
    assert client.post("/api/roadworks/refresh").status_code == 401
    assert client.post(
        "/api/roadworks/refresh", headers={"X-Admin-Token": "fout"}
    ).status_code == 401
    response = client.post("/api/roadworks/refresh", headers={"Authorization": "Bearer geheim"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "count": 2}


def test_dubbele_meldingen_worden_samengevoegd(client: TestClient, sample_gpx: str) -> None:
    """Dezelfde klus wordt vaak twee keer gemeld (heen- en terugrichting)."""
    from app.services import route_service
    from app.services.gpx_service import extract_route_points, parse_gpx

    index = route_service.RouteIndex(extract_route_points(parse_gpx(sample_gpx)))
    doubles = [
        RoadWork(lat=52.3100, lon=4.9005, cause="Asfaltering", authority="Gemeente X"),
        RoadWork(lat=52.3101, lon=4.9006, cause="Asfaltering", authority="Gemeente X"),
        RoadWork(lat=52.3102, lon=4.9004, cause="Riolering", authority="Gemeente X"),
    ]
    kept = route_service.attach_road_works(index, doubles, 250)
    assert len(kept) == 2, "gelijke oorzaak op dezelfde plek telt als een melding"


def test_oorzaak_en_coordinaat_uit_verschillende_records(monkeypatch, feed_xml) -> None:
    """Kern van de NDW-structuur: een situatie bundelt afsluiting en omleiding.

    Alleen het omleidingsrecord heeft coordinaten, terwijl de oorzaak in het
    afsluitingsrecord staat. Beide moeten in een melding terechtkomen.
    """
    settings = get_settings()
    combined = (
        f"<?xml version='1.0' encoding='UTF-8'?><d2LogicalModel {NS}><payloadPublication>"
        "<situation>"
        "  <situationRecord>"  # afsluiting: wel oorzaak, geen coordinaat
        "    <vehicleType>bicycle</vehicleType>"
        "    <cause><causeDescription><values><value>Onderhoud fietspaden</value>"
        "    </values></causeDescription></cause>"
        "    <validity><validityTimeSpecification>"
        "      <overallStartTime>2026-08-10T05:00:00Z</overallStartTime>"
        "      <overallEndTime>2026-08-20T15:00:00Z</overallEndTime>"
        "    </validityTimeSpecification></validity>"
        "  </situationRecord>"
        "  <situationRecord>"  # omleiding: wel coordinaat, geen oorzaak
        "    <vehicleType>bicycle</vehicleType>"
        "    <source><sourceName><values><value>Gemeente Woerden</value>"
        "    </values></sourceName></source>"
        "    <pointCoordinates><latitude>52.3100</latitude>"
        "    <longitude>4.9005</longitude></pointCoordinates>"
        "    <reroutingItineraryDescription><values>"
        "      <value>Omleiding 7</value>"
        "    </values></reroutingItineraryDescription>"
        "  </situationRecord>"
        "</situation></payloadPublication></d2LogicalModel>"
    )

    def fake_download():
        path = settings.cache_dir / "ndw_combined.xml.gz"
        path.write_bytes(gzip.compress(combined.encode("utf-8")))
        return path

    monkeypatch.setattr(roadworks_nl, "_download", fake_download)
    works = roadworks_nl.get_road_works(force_refresh=True)

    assert len(works) == 1, "de twee records vormen samen een melding"
    work = works[0]
    assert work.cause == "Onderhoud fietspaden"
    assert work.authority == "Gemeente Woerden"
    assert (work.lat, work.lon) == (52.31, 4.9005)
    assert work.start == "2026-08-10" and work.end == "2026-08-20"
    assert work.detour is None, "'Omleiding 7' is een label, geen omschrijving"


def test_lege_komma_restanten_worden_opgeschoond() -> None:
    assert roadworks_nl._clean("Kabels / Leidingen, , ") == "Kabels / Leidingen"
    assert roadworks_nl._clean("   ") is None
    assert roadworks_nl._clean("Asfaltering") == "Asfaltering"


def test_labels_gelden_niet_als_omleiding() -> None:
    assert roadworks_nl._useful_detour("Omleiding 5") is None
    assert roadworks_nl._useful_detour("omleiding") is None
    assert roadworks_nl._useful_detour("Omleiding via de Dorpsstraat").startswith("Omleiding via")


def _at(km: float, cause: str = "Asfaltering", authority: str = "Gemeente X",
        distance: float = 20.0) -> RoadWork:
    return RoadWork(lat=52.0, lon=5.0, cause=cause, authority=authority,
                    along_route_km=km, distance_to_route_m=distance)


def test_reeks_op_hetzelfde_stuk_weg_wordt_een_melding() -> None:
    from app.services.route_service import _merge_road_works

    # Gaten van 200 m: samen 600 m, maar elke stap valt binnen 250 m.
    works = [_at(10.0, distance=80), _at(10.2, distance=15), _at(10.4), _at(10.6)]
    merged = _merge_road_works(works, 0.25)
    assert len(merged) == 1
    assert merged[0].distance_to_route_m == 15  # dichtstbijzijnde blijft over


def test_samenvoegen_breekt_bij_groot_gat_of_andere_klus() -> None:
    from app.services.route_service import _merge_road_works

    works = [
        _at(10.0),
        _at(10.1, cause="Riolering"),       # andere oorzaak: eigen melding
        _at(10.15, authority="Gemeente Y"), # andere instantie: eigen melding
        _at(10.2),                          # sluit aan op de eerste reeks
        _at(11.0),                          # 700 m verder: nieuwe reeks
    ]
    merged = _merge_road_works(works, 0.25)
    assert [(w.along_route_km, w.cause, w.authority) for w in merged] == [
        (10.0, "Asfaltering", "Gemeente X"),
        (10.1, "Riolering", "Gemeente X"),
        (10.15, "Asfaltering", "Gemeente Y"),
        (11.0, "Asfaltering", "Gemeente X"),
    ]


def test_achtergrond_ververst_alleen_verouderde_caches(monkeypatch) -> None:
    from app.services import refresher

    calls: list[str] = []
    ttl = get_settings().nl_cache_ttl_seconds
    monkeypatch.setattr(waterpoints_nl, "cache_age_seconds", lambda: 60.0)
    monkeypatch.setattr(roadworks_nl, "cache_age_seconds", lambda: ttl + 1.0)
    monkeypatch.setattr(waterpoints_nl, "load_water_points", lambda: calls.append("nl"))
    monkeypatch.setattr(roadworks_nl, "get_road_works", lambda: calls.append("ndw"))

    assert refresher.refresh_stale_caches() == ["NDW-wegwerkzaamheden"]
    assert calls == ["ndw"]


def test_achtergrond_overleeft_storing(monkeypatch) -> None:
    from app.services import refresher

    def boom() -> None:
        raise RuntimeError("NDW plat")

    monkeypatch.setattr(waterpoints_nl, "cache_age_seconds", lambda: None)
    monkeypatch.setattr(roadworks_nl, "cache_age_seconds", lambda: None)
    monkeypatch.setattr(waterpoints_nl, "load_water_points", boom)
    monkeypatch.setattr(roadworks_nl, "get_road_works", lambda: None)

    assert refresher.refresh_stale_caches() == ["NDW-wegwerkzaamheden"]


def test_achtergrondlus_staat_uit_in_tests() -> None:
    from app.services import refresher

    assert refresher.start() is False


# -- Afgesloten stuk in plaats van punt ---------------------------------------

_XSI = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def _feed_with_closure(point: tuple[float, float], closure: list[tuple[float, float]],
                       detour: list[tuple[float, float]] | None = None,
                       closure_vehicle: str | None = None) -> str:
    pos = " ".join(f"{lat} {lon}" for lat, lon in closure)
    detour_pos = " ".join(f"{lat} {lon}" for lat, lon in (detour or []))
    vehicle = f"<vehicleType>{closure_vehicle}</vehicleType>" if closure_vehicle else ""
    return (
        f"<?xml version='1.0' encoding='UTF-8'?><d2LogicalModel {NS} {_XSI}><payloadPublication>"
        "<situation>"
        '  <situationRecord xsi:type="MaintenanceWorks">'
        "    <vehicleType>bicycle</vehicleType>"
        "    <cause><causeDescription><values><value>Afsluiting fietspad</value>"
        "    </values></causeDescription></cause>"
        f"    <pointCoordinates><latitude>{point[0]}</latitude>"
        f"    <longitude>{point[1]}</longitude></pointCoordinates>"
        "  </situationRecord>"
        '  <situationRecord xsi:type="RoadOrCarriagewayOrLaneManagement">'
        f"    {vehicle}<gmlLineString><posList>{pos}</posList></gmlLineString>"
        "  </situationRecord>"
        '  <situationRecord xsi:type="ReroutingManagement">'
        f"    <gmlLineString><posList>{detour_pos}</posList></gmlLineString>"
        "  </situationRecord>"
        "</situation></payloadPublication></d2LogicalModel>"
    )


def _load(monkeypatch, xml: str) -> list[RoadWork]:
    settings = get_settings()

    def fake_download():
        path = settings.cache_dir / "ndw_closure.xml.gz"
        path.write_bytes(gzip.compress(xml.encode("utf-8")))
        return path

    monkeypatch.setattr(roadworks_nl, "_download", fake_download)
    return roadworks_nl.get_road_works(force_refresh=True)


def _index():
    from app.models.schemas import RoutePoint
    from app.services.route_service import RouteIndex

    return RouteIndex([RoutePoint(lat=52.30 + i * 0.001, lon=4.9) for i in range(101)])


def test_parser_leest_afgesloten_stuk_maar_niet_de_omleiding(monkeypatch) -> None:
    works = _load(monkeypatch, _feed_with_closure(
        (52.31, 4.9005),
        closure=[(52.3100, 4.9000), (52.3110, 4.9000)],
        detour=[(52.3100, 4.9100), (52.3110, 4.9100)],
    ))
    assert len(works) == 1
    assert works[0].lines == [[[52.31, 4.9], [52.311, 4.9]]]


def test_afsluiting_alleen_voor_autos_telt_niet(monkeypatch) -> None:
    works = _load(monkeypatch, _feed_with_closure(
        (52.31, 4.9005), closure=[(52.31, 4.9), (52.311, 4.9)], closure_vehicle="car",
    ))
    assert works[0].lines == []


def test_punt_dichtbij_maar_afgesloten_stuk_op_parallelle_straat(monkeypatch) -> None:
    """Het Franks Avondetappe-geval: punt op 85 m, maar het werk ligt op een andere straat."""
    from app.services.route_service import attach_road_works

    work = RoadWork(lat=52.31, lon=4.90125, cause="Kabels",  # ~85 m van de route
                    lines=[[[52.3100, 4.9013], [52.3115, 4.9013]]])  # ~90 m ernaast
    assert attach_road_works(_index(), [work], radius_m=250) == []


def test_afgesloten_stuk_op_route_telt_ook_als_punt_ver_weg_ligt() -> None:
    """Bij een lang werkvak staat het NDW-punt soms honderden meters verderop."""
    from app.services.route_service import attach_road_works

    work = RoadWork(lat=52.35, lon=4.91, cause="Asfaltering",  # ~700 m van de route
                    lines=[[[52.3500, 4.9001], [52.3600, 4.9001]]])  # ligt op de route
    [hit] = attach_road_works(_index(), [work], radius_m=100)
    assert hit.distance_to_route_m < 10
    assert hit.lon == pytest.approx(4.9001, abs=1e-6), "marker verhuist naar het werk"
    assert 5.4 < hit.along_route_km < 5.8


def test_zonder_afgesloten_stuk_valt_terug_op_punt() -> None:
    from app.services.route_service import attach_road_works

    near = RoadWork(lat=52.31, lon=4.9010, cause="A")   # ~70 m
    far = RoadWork(lat=52.32, lon=4.9020, cause="B")    # ~140 m
    hits = attach_road_works(_index(), [near, far], radius_m=100)
    assert [w.cause for w in hits] == ["A"]


def test_bbox_vindt_melding_via_afgesloten_stuk() -> None:
    """Punt buiten het zoekvak, afgesloten stuk erin: toch meenemen."""
    works = [RoadWork(lat=53.5, lon=6.0, lines=[[[52.35, 4.9], [52.36, 4.9]]])]
    import unittest.mock as mock

    with mock.patch.object(roadworks_nl, "get_road_works", return_value=works):
        found = roadworks_nl.load_road_works_near([(52.30, 4.9), (52.40, 4.9)], 100, date(2026, 9, 1))
    assert len(found) == 1
