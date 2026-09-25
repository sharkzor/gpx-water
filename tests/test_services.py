"""Tests voor GPX-verwerking, afstandsberekening en waterpuntbronnen."""

from __future__ import annotations

import pytest

from app.services import gpx_service, route_service, waterpoints_nl
from app.services.geo import haversine_m, in_netherlands, nl_share
from app.services.route_service import RouteIndex


def test_parse_and_extract_route_points(sample_gpx: str) -> None:
    gpx = gpx_service.parse_gpx(sample_gpx)
    points = gpx_service.extract_route_points(gpx)
    assert len(points) == 101
    assert points[0].ele == pytest.approx(2.0)
    assert points[-1].ele == pytest.approx(12.0)


def test_parse_route_only_gpx() -> None:
    xml = (
        '<?xml version="1.0"?><gpx version="1.1" creator="t" '
        'xmlns="http://www.topografix.com/GPX/1/1"><rte>'
        '<rtept lat="52.0" lon="5.0"/><rtept lat="52.01" lon="5.0"/>'
        "</rte></gpx>"
    )
    points = gpx_service.extract_route_points(gpx_service.parse_gpx(xml))
    assert len(points) == 2


def test_invalid_gpx_raises() -> None:
    with pytest.raises(gpx_service.GpxError):
        gpx_service.extract_route_points(gpx_service.parse_gpx("<gpx></gpx>"))


def test_route_distance_matches_reference(sample_gpx: str) -> None:
    points = gpx_service.extract_route_points(gpx_service.parse_gpx(sample_gpx))
    index = RouteIndex(points)
    reference = haversine_m(52.30, 4.9, 52.40, 4.9)
    assert index.total_distance_m == pytest.approx(reference, rel=1e-5)


def test_route_distance_uses_spherical_model() -> None:
    """De afstand moet bolvormig zijn (zoals Strava/Garmin), niet WGS84-ellipsoidaal.

    Op 52 graden noorderbreedte levert de ellipsoide een ~0,3% langere
    oost-west afstand op; die afwijking mag hier niet optreden.
    """
    from geopy.distance import geodesic

    from app.models.schemas import RoutePoint

    # Zuiver oost-west traject: daar is het verschil het grootst.
    coords = [(52.0, 4.0), (52.0, 5.0)]
    index = RouteIndex([RoutePoint(lat, lon) for lat, lon in coords])

    spherical = haversine_m(*coords[0], *coords[1])
    ellipsoidal = geodesic(coords[0], coords[1]).meters

    assert index.total_distance_m == pytest.approx(spherical, rel=1e-6)
    assert ellipsoidal > spherical * 1.002  # bevestigt dat het verschil reeel is


def test_locate_distance_and_position(sample_gpx: str) -> None:
    points = gpx_service.extract_route_points(gpx_service.parse_gpx(sample_gpx))
    index = RouteIndex(points)
    # 0.0005 graden lengte op 52.35 N is ongeveer 34 meter
    distance_m, along_m = index.locate(52.35, 4.9005)
    assert distance_m == pytest.approx(34, abs=5)
    assert along_m == pytest.approx(index.total_distance_m / 2, rel=0.02)


def test_attach_filter_dedupe_and_stats(sample_gpx: str, waterpoints_gpx: str) -> None:
    points = gpx_service.extract_route_points(gpx_service.parse_gpx(sample_gpx))
    index = RouteIndex(points)
    candidates = gpx_service.parse_waypoints(
        gpx_service.parse_gpx(waterpoints_gpx), "test"
    )
    matched = route_service.attach_to_route(index, candidates, 500)
    assert len(matched) == 3  # het verre punt valt af

    deduped = route_service.deduplicate(matched, index.projection)
    assert len(deduped) == 2  # duplicaat binnen 50 m verwijderd
    assert deduped[0].along_route_km < deduped[1].along_route_km

    stats = route_service.build_stats(index, deduped, has_elevation=True)
    assert stats.water_point_count == 2
    assert stats.total_distance_km == pytest.approx(11.1, abs=0.3)
    assert stats.average_gap_km == pytest.approx(7.8, abs=0.5)
    assert stats.warning is None
    assert stats.has_elevation is True


def test_warning_for_long_dry_stretch(sample_gpx: str) -> None:
    points = gpx_service.extract_route_points(gpx_service.parse_gpx(sample_gpx))
    index = RouteIndex(points)
    index.total_distance_m = 120_000  # simuleer lange route
    stats = route_service.build_stats(index, [], has_elevation=False)
    assert stats.warning is not None
    assert stats.water_point_count == 0


def test_build_output_gpx_keeps_route_and_adds_waypoints(
    sample_gpx: str, waterpoints_gpx: str
) -> None:
    gpx = gpx_service.parse_gpx(sample_gpx)
    points = gpx_service.extract_route_points(gpx)
    index = RouteIndex(points)
    candidates = gpx_service.parse_waypoints(
        gpx_service.parse_gpx(waterpoints_gpx), "drinkwaterpunten.nl"
    )
    matched = route_service.deduplicate(
        route_service.attach_to_route(index, candidates, 500), index.projection
    )
    xml = gpx_service.build_output_gpx(gpx, matched)

    reparsed = gpx_service.parse_gpx(xml)
    assert len(reparsed.waypoints) == 2
    assert len(gpx_service.extract_route_points(reparsed)) == 101
    assert reparsed.tracks[0].segments[0].points[0].elevation == pytest.approx(2.0)
    wpt = reparsed.waypoints[0]
    assert wpt.name.startswith("\U0001f4a7 Water")
    assert "km" in wpt.name
    assert wpt.symbol == "Water Source"
    assert wpt.type == "Water"
    assert "Bron: drinkwaterpunten.nl" in wpt.description
    assert '<wpt lat=' in xml and "</gpx>" in xml


def test_nl_detection() -> None:
    assert in_netherlands(52.37, 4.90) is True  # Amsterdam
    assert in_netherlands(48.85, 2.35) is False  # Parijs
    assert nl_share([(52.37, 4.90), (52.09, 5.12), (48.85, 2.35)]) == pytest.approx(2 / 3)


def test_nl_cache_download_and_reuse(monkeypatch, waterpoints_gpx: str) -> None:
    calls = {"n": 0}

    def fake_download() -> str:
        calls["n"] += 1
        return waterpoints_gpx

    monkeypatch.setattr(waterpoints_nl, "_download", fake_download)
    cache = waterpoints_nl._cache_file()
    if cache.exists():
        cache.unlink()

    points = waterpoints_nl.load_water_points()
    assert len(points) == 4
    assert points[0].source == "drinkwaterpunten.nl"
    assert cache.exists()

    waterpoints_nl.load_water_points()  # tweede keer uit cache
    assert calls["n"] == 1

    waterpoints_nl.load_water_points(force_refresh=True)
    assert calls["n"] == 2

    near = waterpoints_nl.load_water_points_near([(52.30, 4.90), (52.40, 4.90)], 1000)
    assert len(near) == 3


def test_nl_cache_fallback_on_error(monkeypatch, waterpoints_gpx: str) -> None:
    def boom() -> str:
        raise RuntimeError("netwerk down")

    monkeypatch.setattr(waterpoints_nl, "_download", boom)
    waterpoints_nl._cache_file().write_text(waterpoints_gpx, encoding="utf-8")
    assert len(waterpoints_nl.load_water_points(force_refresh=True)) == 4


def test_output_stays_valid_xml_with_special_characters() -> None:
    from app.models.schemas import WaterPoint

    src = (
        '<?xml version="1.0"?><gpx version="1.1" creator="t" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
        '<metadata><link href="https://a.nl/?x=1&amp;y=2"><text>t</text></link></metadata>'
        '<trk><trkseg><trkpt lat="52.0" lon="5.0"/><trkpt lat="52.1" lon="5.0"/>'
        "</trkseg></trk></gpx>"
    )
    gpx = gpx_service.parse_gpx(src)
    wp = WaterPoint(
        lat=52.0,
        lon=5.0,
        name='Caf<e> & "co"',
        operator="A & B",
        website="https://x.nl/?a=1&b=2",
        source="openstreetmap",
    )
    reparsed = gpx_service.parse_gpx(gpx_service.build_output_gpx(gpx, [wp]))
    assert reparsed.waypoints[0].link == "https://x.nl/?a=1&b=2"
    assert reparsed.link == "https://a.nl/?x=1&y=2"
    assert reparsed.waypoints[0].comment == 'Caf<e> & "co"'


def test_website_alleen_http_links() -> None:
    from app.models.schemas import WaterPoint, safe_http_url

    assert safe_http_url("https://example.org/tap") == "https://example.org/tap"
    for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x",
                "java\tscript:alert(1)", "//evil.example", "https://", "ftp://x.nl"):
        assert safe_http_url(bad) is None, bad
    assert WaterPoint(lat=52, lon=5, website="javascript:alert(1)").website is None
