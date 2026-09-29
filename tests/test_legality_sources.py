"""Tests voor de extra bronnen bij verboden paden: NDW-verkeersborden en BGT."""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import (
    bgt,
    gpx_service,
    legality,
    legality_extra,
    mvt,
    ndw_signs,
    osm_index,
    processing,
    waterpoints_nl,
)
from app.services.geo import LocalProjection
from app.services.ndw_signs import Sign

LON = 4.9


@pytest.fixture(autouse=True)
def _offline(monkeypatch, waterpoints_gpx: str):
    get_settings().ensure_dirs()
    monkeypatch.setattr(waterpoints_nl, "_download", lambda: waterpoints_gpx)
    waterpoints_nl.load_water_points(force_refresh=True)


@pytest.fixture
def all_sources(monkeypatch):
    monkeypatch.setattr(get_settings(), "legality_sources", ("osm", "ndw", "bgt"))


def _route(lat_from: float, lat_to: float, steps: int = 50) -> list[tuple[float, float]]:
    return [(lat_from + (lat_to - lat_from) * i / steps, LON) for i in range(steps + 1)]


def _setup(points):
    samples = legality.sample_route(points)
    return samples, LocalProjection.from_points(points)


# -- Minimale MVT-encoder, alleen voor tests ---------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, wire: int, payload: bytes | int) -> bytes:
    key = _varint((number << 3) | wire)
    if wire == 0:
        return key + _varint(int(payload))  # type: ignore[arg-type]
    return key + _varint(len(payload)) + payload  # type: ignore[arg-type]


def _zz(v: int) -> int:
    return (v << 1) ^ (v >> 31)


def _polygon_commands(rings: list[list[tuple[int, int]]]) -> list[int]:
    commands: list[int] = []
    cx = cy = 0
    for ring in rings:
        x, y = ring[0]
        commands += [(1 << 3) | 1, _zz(x - cx), _zz(y - cy)]
        cx, cy = x, y
        commands.append(((len(ring) - 1) << 3) | 2)
        for x, y in ring[1:]:
            commands += [_zz(x - cx), _zz(y - cy)]
            cx, cy = x, y
        commands.append((1 << 3) | 7)
    return commands


def _tile(features: list[tuple[str, list[list[tuple[int, int]]]]], layer="wegdeel") -> bytes:
    values = sorted({f for f, _ in features})
    layer_bytes = _field(1, 2, layer.encode())
    for functie, rings in features:
        packed_tags = _varint(0) + _varint(values.index(functie))
        packed_geom = b"".join(_varint(c) for c in _polygon_commands(rings))
        feature = _field(2, 2, packed_tags) + _field(3, 0, 3) + _field(4, 2, packed_geom)
        layer_bytes += _field(2, 2, feature)
    layer_bytes += _field(3, 2, b"functie")
    for v in values:
        layer_bytes += _field(4, 2, _field(1, 2, v.encode()))
    layer_bytes += _field(5, 0, 4096) + _field(15, 0, 2)
    return _field(3, 2, layer_bytes)


# Buitenring met de klok mee in tegelcoördinaten (y omlaag) = positieve oppervlakte.
_SQUARE = [(100, 100), (900, 100), (900, 900), (100, 900), (100, 100)]
_HOLE = [(400, 400), (400, 600), (600, 600), (600, 400), (400, 400)]


def test_mvt_decoder_leest_polygonen_met_gat() -> None:
    data = _tile([("voetpad", [_SQUARE, _HOLE]), ("fietspad", [_SQUARE])])
    layers = mvt.decode(data, {"wegdeel"})
    features = layers["wegdeel"].features
    assert [f.properties["functie"] for f in features] == ["voetpad", "fietspad"]
    assert features[0].type == mvt.POLYGON
    outer, hole = features[0].rings
    assert mvt.signed_area(outer) > 0 > mvt.signed_area(hole)
    assert mvt.decode(data, {"andere laag"}) == {}


def test_bgt_tegel_naar_wegdelen() -> None:
    compact = bgt._compact_tile(_tile([("voetpad", [_SQUARE, _HOLE])]))
    x, y = bgt.tile_of(52.1, 5.1)
    areas = bgt._areas_of((x, y), compact)
    assert len(areas) == 1 and areas[0].functie == "voetpad"
    assert len(areas[0].holes) == 1
    lat, lon = areas[0].exterior[0]
    # De tegel ligt rond het punt waaruit hij berekend is (~190 m breed).
    assert abs(lat - 52.1) < 0.01 and abs(lon - 5.1) < 0.01
    assert bgt.tile_of(lat, lon) == (x, y)


def test_bgt_tegels_worden_gecachet(monkeypatch) -> None:
    calls: list[str] = []

    class Response:
        status_code = 200
        content = _tile([("voetpad", [_SQUARE])])

        def raise_for_status(self) -> None:
            pass

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response()

    monkeypatch.setattr(bgt.requests, "get", fake_get)
    tile = (67456, 43012)
    bgt._cache_path(*tile).unlink(missing_ok=True)
    first, failed = bgt.load_areas([tile])
    second, _ = bgt.load_areas([tile])
    assert failed == 0 and len(first) == len(second) == 1
    assert len(calls) == 1 and "/17/43012/67456" in calls[0]


def test_bgt_storing_telt_als_mislukte_tegel(monkeypatch) -> None:
    def boom(*a, **k):
        raise ConnectionError("PDOK plat")

    monkeypatch.setattr(bgt.requests, "get", boom)
    tile = (67457, 43013)
    bgt._cache_path(*tile).unlink(missing_ok=True)
    assert bgt.load_areas([tile]) == ([], 1)


# -- BGT-detectie ------------------------------------------------------------


def _box(lat1, lat2, lon1, lon2):
    return [(lat1, lon1), (lat1, lon2), (lat2, lon2), (lat2, lon1), (lat1, lon1)]


def _fake_areas(monkeypatch, areas):
    monkeypatch.setattr(bgt, "load_areas", lambda tiles: (areas, 0))


def test_bgt_voetpad_zonder_alternatief_is_let_op(monkeypatch) -> None:
    points = _route(52.30, 52.31)
    d = 0.00003  # ~2 m aan weerszijden
    _fake_areas(monkeypatch, [bgt.Area("voetpad", _box(52.302, 52.305, LON - d, LON + d), [])])
    samples, projection = _setup(points)
    segments, note = legality_extra.detect_bgt(samples, projection)
    assert note is None
    assert len(segments) == 1
    seg = segments[0]
    assert (seg.severity, seg.code, seg.sources) == ("warning", "bgt_voetpad", ["BGT"])
    assert 0.2 <= seg.start_km <= 0.25 and 0.53 <= seg.end_km <= 0.58


def test_bgt_voetpad_naast_rijbaan_telt_niet(monkeypatch) -> None:
    points = _route(52.30, 52.31)
    d = 0.00003
    _fake_areas(
        monkeypatch,
        [
            bgt.Area("voetpad", _box(52.302, 52.305, LON - d, LON + d), []),
            bgt.Area("rijbaan lokale weg", _box(52.302, 52.305, LON + d, LON + 3 * d), []),
        ],
    )
    samples, projection = _setup(points)
    assert legality_extra.detect_bgt(samples, projection) == ([], None)


def test_bgt_wijkt_voor_fietspad_in_osm(monkeypatch) -> None:
    points = _route(52.30, 52.31)
    d = 0.00003
    _fake_areas(monkeypatch, [bgt.Area("voetpad", _box(52.302, 52.305, LON - d, LON + d), [])])
    samples, projection = _setup(points)
    ways = [legality.Way(1, {"highway": "cycleway"}, points)]
    assert legality_extra.detect_bgt(samples, projection, ways) == ([], None)


def test_bgt_te_veel_tegels_geeft_notitie(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "bgt_max_tiles", 3)
    monkeypatch.setattr(bgt, "load_areas", lambda t: pytest.fail("mag niet laden"))
    samples, projection = _setup(_route(52.30, 52.31))
    segments, note = legality_extra.detect_bgt(samples, projection)
    assert segments == [] and "maximaal 3" in note


def test_bgt_buiten_nederland_doet_niets(monkeypatch) -> None:
    monkeypatch.setattr(bgt, "load_areas", lambda t: pytest.fail("mag niet laden"))
    points = [(50.80 + i * 0.0002, 4.35) for i in range(50)]
    samples, projection = _setup(points)
    assert legality_extra.detect_bgt(samples, projection) == ([], None)


# -- NDW-borden --------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "texts", "expected"),
    [
        ("C14", [], "forbidden"),
        ("C14", ["uitgezonderd snorfietsen"], "forbidden"),
        ("C14", ["GEEN FIETSEN PLAATSEN"], "forbidden"),
        ("G7", [], "forbidden"),
        ("G7", ["fietsers toegestaan"], None),
        ("G7", ["uitgezonderd (brom)fietsers"], None),
        ("G7", ["onleesbaar"], "warning"),
        ("C1", ["uitgezonderd bestemmingsverkeer"], "warning"),
        ("C2", [], None),
        # Echte onderborden uit het NDW-register:
        ("G7", ["dus niet fietsen"], "forbidden"),
        ("G7", ["verboden te fietsen"], "forbidden"),
        ("G7", ["fietsen niet toegestaan"], "forbidden"),
        ("G7", ["geen fietspad"], "forbidden"),
        ("C14", ["ook geen fiets aan de hand"], "forbidden"),
        ("G7", ["fietsen toegestaan snorfietsen niet"], None),
        ("G7", ["fietsen toegestaan. snor- en brom- fietsen niet"], None),
        ("C1", ["geldt niet voor fietsers"], None),
        ("G7", ["fietsers te gast"], None),
        ("G7", ["fietsen toegestaan buiten winkeltijden"], "warning"),
        ("G7", ["fietsers afstappen"], "warning"),
        ("C1", ["uitgezonderd fietsers (geen doorgaand fietsverkeer)"], "warning"),
        # Onderbord zonder tekst (vaak een fietspictogram): let op, niet verboden.
        ("G7", [""], "warning"),
        ("C14", [""], "forbidden"),
    ],
)
def test_ndw_bord_interpretatie(code, texts, expected) -> None:
    verdict = ndw_signs.interpret(Sign("1", code, 52.0, 5.0, 0, texts))
    assert (verdict[0] if verdict else None) == expected


def _sign(code="G7", lat=52.3021, lon=LON + 0.00006, bearing=0.0, texts=None) -> Sign:
    return Sign(
        "s1", code, lat, lon, bearing, texts or [],
        image_url="https://wegkenmerken.ndw.nu/api/images/abc", road="Parkpad",
        last_seen="2022-03-08",
    )


def _fake_signs(monkeypatch, signs):
    monkeypatch.setattr(ndw_signs, "signs_in_bbox", lambda *box: list(signs))


def test_ndw_bord_geldt_voor_pad_dat_de_route_neemt(monkeypatch) -> None:
    points = _route(52.30, 52.31)
    samples, projection = _setup(points)
    path = legality.Way(1, {"highway": "path", "name": "Parkpad"}, _route(52.302, 52.305))
    road = legality.Way(2, {"highway": "residential"}, _route(52.30, 52.302) + _route(52.305, 52.31))
    _fake_signs(monkeypatch, [_sign()])
    segments = legality_extra.detect_signs(samples, projection, [path, road])
    assert len(segments) == 1
    seg = segments[0]
    assert (seg.severity, seg.code, seg.sources) == ("forbidden", "ndw_g7", ["NDW-verkeersborden"])
    assert seg.signs[0]["image_url"].startswith("https://wegkenmerken")
    assert 0.2 <= seg.start_km <= 0.25 and seg.end_km >= 0.5


def test_ndw_bord_voor_tegengestelde_richting_telt_niet(monkeypatch) -> None:
    points = _route(52.30, 52.31)
    samples, projection = _setup(points)
    path = legality.Way(1, {"highway": "path"}, _route(52.302, 52.305))
    _fake_signs(monkeypatch, [_sign(bearing=180.0)])
    assert legality_extra.detect_signs(samples, projection, [path]) == []


def test_ndw_bord_bij_zijpad_telt_niet(monkeypatch) -> None:
    """Een bord bij een dwarspad dat de route alleen kruist, geldt niet voor de route."""
    points = _route(52.30, 52.31)
    samples, projection = _setup(points)
    road = legality.Way(1, {"highway": "residential"}, points)
    side = legality.Way(2, {"highway": "footway"}, [(52.303, LON), (52.303, LON + 0.003)])
    _fake_signs(monkeypatch, [_sign(lat=52.30305, lon=LON + 0.0001, bearing=90.0)])
    assert legality_extra.detect_signs(samples, projection, [road, side]) == []


def test_ndw_bord_tegen_fietsvriendelijke_osm_is_let_op(monkeypatch) -> None:
    points = _route(52.30, 52.31)
    samples, projection = _setup(points)
    path = legality.Way(1, {"highway": "service", "bicycle": "yes"}, points)
    _fake_signs(monkeypatch, [_sign()])
    segments = legality_extra.detect_signs(samples, projection, [path])
    assert len(segments) == 1
    assert segments[0].severity == "warning"
    assert "OpenStreetMap: fietsen toegestaan" in segments[0].label


def test_ndw_verbod_op_onderbord_staat_in_label() -> None:
    verdict = ndw_signs.interpret(Sign("1", "G7", 52.0, 5.0, 0, ["Dus niet fietsen"]))
    assert verdict == ("forbidden", "ndw_g7", "Voetpad (bord G7), dus niet fietsen")
    verdict = ndw_signs.interpret(Sign("1", "G7", 52.0, 5.0, 0, [""]))
    assert verdict[2].endswith("onderbord zonder tekst")


def test_ndw_storing_wordt_niet_bij_elk_verzoek_herhaald(monkeypatch) -> None:
    calls: list[str] = []

    def boom(*a, **k):
        calls.append("x")
        raise ConnectionError("NDW plat")

    ndw_signs._cache_file().unlink(missing_ok=True)
    monkeypatch.setattr(ndw_signs.requests, "get", boom)
    monkeypatch.setattr(ndw_signs, "_last_failure", [0.0])
    for _ in range(3):
        with pytest.raises(RuntimeError):
            ndw_signs.signs_in_bbox(52.0, 5.0, 52.1, 5.1)
    assert len(calls) == 1


def test_ndw_cache_verversen(monkeypatch) -> None:
    feature = {
        "id": "x1",
        "geometry": {"type": "Point", "coordinates": [5.1, 52.1]},
        "properties": {
            "rvvCode": "C14", "bearing": 45, "status": "PLACED",
            "textSigns": [{"type": "TEXT", "text": "uitgezonderd snorfietsen"}],
            "imageUrl": "javascript:alert(1)", "roadName": "Laan", "lastSeenOn": "2022-01-01",
        },
    }

    class Response:
        def __init__(self, code):
            self.code = code

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"features": [feature] if self.code == "C14" else []}

    monkeypatch.setattr(ndw_signs.requests, "get", lambda url, params, **k: Response(params["rvvCode"]))
    ndw_signs._cache_file().unlink(missing_ok=True)
    signs = ndw_signs.refresh_cache()
    assert len(signs) == 1 and signs[0]["image_url"] is None
    found = ndw_signs.signs_in_bbox(52.0, 5.0, 52.2, 5.2)
    assert [s.code for s in found] == ["C14"]
    assert ndw_signs.signs_in_bbox(53.0, 6.0, 53.1, 6.1) == []


# -- Samenvoegen en doorgeven ------------------------------------------------


def _segment(start, end, severity="warning", sources=("OpenStreetMap",), signs=()):
    return legality.Segment(
        severity=severity, code="x", label=f"{severity}-label", way_id=None, way_name=None,
        highway=None, start_km=start, end_km=end, length_m=(end - start) * 1000,
        coordinates=[], sources=list(sources), signs=list(signs),
    )


def test_bronnen_worden_samengevoegd() -> None:
    samples = legality.sample_route(_route(52.30, 52.32))
    osm = [_segment(0.2, 0.5)]
    ndw = _segment(0.45, 0.7, "forbidden", ["NDW-verkeersborden"], [{"code": "G7"}])
    loose = _segment(1.5, 1.7, sources=["BGT"])
    result = legality.combine(osm, [ndw, loose], samples)
    assert len(result) == 2
    merged = result[0]
    assert merged.severity == "forbidden" and merged.label == "forbidden-label"
    assert merged.sources == ["OpenStreetMap", "NDW-verkeersborden"]
    assert merged.signs == [{"code": "G7"}]
    assert merged.start_km == 0.2 and merged.end_km == 0.7 and merged.coordinates
    assert result[1].sources == ["BGT"]


def test_storing_in_bron_wordt_notitie(monkeypatch) -> None:
    monkeypatch.setattr(osm_index, "ways_in_bbox", lambda *box: [])

    def boom(*a):
        raise RuntimeError("NDW plat")

    monkeypatch.setattr(legality_extra, "detect_signs", boom)
    monkeypatch.setattr(legality_extra, "detect_bgt", lambda *a: ([], None))
    report = legality.check_route(_route(52.30, 52.31), sources=("osm", "ndw", "bgt"))
    assert report.segments == []
    assert report.notes == ["NDW-verkeersborden niet beschikbaar: NDW plat"]
    assert report.source == "OpenStreetMap, NDW-verkeersborden, BGT"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_api_geeft_gekozen_bronnen_door(client, sample_gpx, monkeypatch, all_sources) -> None:
    seen: list[tuple] = []

    def fake_check(coords, sources=("osm",), **kwargs):
        seen.append(tuple(sources))
        seg = _segment(1.0, 1.3, "forbidden", ["OpenStreetMap", "NDW-verkeersborden"],
                       [{"code": "G7", "text": None, "image_url": "https://wegkenmerken.ndw.nu/api/images/abc",
                         "last_seen": "2022-03-08", "lat": 52.31, "lon": 4.9, "road": None}])
        seg.coordinates = [(52.309, 4.9), (52.312, 4.9)]
        return legality.Report(1.0, 1, 0, [seg], notes=["BGT: 1 van 9 kaarttegels konden niet worden opgehaald."])

    monkeypatch.setattr(osm_index, "status", lambda: osm_index.IndexStatus(available=True))
    monkeypatch.setattr(legality, "check_route", fake_check)
    data = client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data={"water": "false", "legality": "true", "legality_sources": "bgt,ndw"},
    ).json()
    assert seen == [("ndw", "bgt")]
    assert data["legality_sources"] == ["NDW-verkeersborden", "BGT"]
    assert data["legality_notes"][0].startswith("BGT:")
    seg = data["legality_segments"][0]
    assert seg["sources"] == ["OpenStreetMap", "NDW-verkeersborden"]
    assert seg["signs"][0]["image_url"].startswith("https://")

    gpx = client.get(f"/api/download/{data['job_id']}").text
    assert "Bord G7" in gpx and "wegkenmerken.ndw.nu" in gpx
    assert "Bron: OpenStreetMap, NDW-verkeersborden" in gpx

    # Zonder keuze: alle bronnen van deze instantie.
    client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data={"water": "false", "legality": "true"},
    )
    assert seen[-1] == ("osm", "ndw", "bgt")


def test_onbekende_bron_geeft_400(client, sample_gpx) -> None:
    response = client.post(
        "/api/process",
        files={"file": ("rit.gpx", sample_gpx, "application/gpx+xml")},
        data={"legality": "true", "legality_sources": "osm,<script>"},
    )
    assert response.status_code == 400


def test_zonder_kaart_draait_alleen_bgt(monkeypatch, all_sources) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(osm_index, "status", lambda: osm_index.IndexStatus(available=False))
    monkeypatch.setattr(osm_index, "current_job", lambda: None)
    monkeypatch.setattr(
        legality, "check_route", lambda coords, sources, **k: seen.append(tuple(sources)) or legality.Report(1, 0, 0, [])
    )
    outcome = processing._collect_legality(_route(52.30, 52.31), ("osm", "ndw", "bgt"))
    assert seen == [("bgt",)] and outcome.error is None
    assert outcome.sources == ["BGT"]
    assert "OpenStreetMap en NDW overgeslagen" in outcome.notes[0]

    outcome = processing._collect_legality(_route(52.30, 52.31), ("osm",))
    assert "nog niet beschikbaar" in outcome.error


def test_pagina_toont_bronkeuze(client, all_sources) -> None:
    html = client.get("/").text
    assert 'data-legality-source' in html and "NDW-verkeersborden" in html


def test_gpx_beschrijving_noemt_bronnen_en_foto() -> None:
    seg = _segment(1.0, 1.2, "forbidden", ["OpenStreetMap", "BGT"],
                   [{"code": "C14", "text": "uitgezonderd snorfietsen", "image_url": "https://x.test/a.jpg",
                     "last_seen": "2022-01-01"}])
    text = gpx_service._describe_segment(seg)
    assert "Bord C14 (uitgezonderd snorfietsen), gezien 2022-01-01, foto: https://x.test/a.jpg" in text
    assert text.endswith("Bron: OpenStreetMap, BGT")
    assert math.isfinite(seg.length_m)
