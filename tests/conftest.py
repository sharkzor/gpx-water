import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="gpx-test-"))
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Tests mogen nooit echte downloads starten via de achtergrondlus.
os.environ["BACKGROUND_REFRESH"] = "false"

# Zorg dat een echte .env in de projectmap de tests nooit beïnvloedt:
# python-dotenv overschrijft geen variabelen die al bestaan.
for _name in (
    "STRAVA_CLIENT_ID",
    "STRAVA_CLIENT_SECRET",
    "STRAVA_REDIRECT_URI",
    "STRAVA_ACCESS_TOKEN",
    "STRAVA_REFRESH_TOKEN",
    "SECRET_KEY",
    "STRAVA_ENABLED",
):
    os.environ[_name] = "auto" if _name == "STRAVA_ENABLED" else ""

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _prepare_dirs() -> None:
    get_settings().ensure_dirs()


@pytest.fixture(autouse=True)
def _clean_token_store():
    """Start elke test zonder achtergebleven Strava-sessies."""
    from app.services import strava_service, token_store

    token_store.delete(strava_service.ENV_SESSION_ID)
    yield
    token_store.delete(strava_service.ENV_SESSION_ID)


@pytest.fixture
def sample_gpx() -> str:
    """Rechte route van ~11 km langs de Amsterdamse ring, met hoogte."""
    points = []
    for i in range(0, 101):
        lat = 52.30 + i * 0.001
        points.append(
            f'<trkpt lat="{lat:.6f}" lon="4.900000"><ele>{2 + i * 0.1:.1f}</ele></trkpt>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">'
        "<metadata><name>Testroute</name></metadata>"
        "<trk><name>Testroute</name><trkseg>" + "".join(points) + "</trkseg></trk></gpx>"
    )


@pytest.fixture
def waterpoints_gpx() -> str:
    """Twee waterpunten dicht bij de route, één ver weg, één duplicaat."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">'
        '<wpt lat="52.310000" lon="4.900500"><name>Tappunt A</name></wpt>'
        '<wpt lat="52.310100" lon="4.900500"><name>Tappunt A duplicaat</name></wpt>'
        '<wpt lat="52.380000" lon="4.901500"><name>Tappunt B</name></wpt>'
        '<wpt lat="52.350000" lon="5.400000"><name>Ver weg</name></wpt>'
        "</gpx>"
    )
