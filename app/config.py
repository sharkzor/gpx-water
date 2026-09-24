"""Applicatieconfiguratie, volledig instelbaar via environment variables."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    """Centrale settings-container."""

    def __init__(self) -> None:
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = _int("PORT", 8080)
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

        self.data_dir: Path = Path(os.getenv("DATA_DIR", "/app/data"))
        self.cache_dir: Path = self.data_dir / "cache"
        self.tmp_dir: Path = self.data_dir / "tmp"

        # Bron Nederlandse drinkwaterpunten
        self.nl_gpx_url: str = os.getenv(
            "NL_GPX_URL",
            "https://drinkwaterpunten.nl/assets/gpx/publieke_drinkwaterpunten_nl.gpx",
        )
        self.nl_cache_ttl_seconds: int = _int("NL_CACHE_TTL_SECONDS", 24 * 3600)

        # OpenStreetMap / Overpass
        self.overpass_url: str = os.getenv(
            "OVERPASS_URL", "https://overpass-api.de/api/interpreter"
        )
        self.overpass_timeout: int = _int("OVERPASS_TIMEOUT", 60)

        # Verwerking
        self.allowed_radii: tuple[int, ...] = (100, 250, 500, 750, 1000)
        self.default_radius_m: int = _int("DEFAULT_RADIUS_M", 250)
        self.dedupe_distance_m: float = _float("DEDUPE_DISTANCE_M", 50.0)
        self.nl_share_threshold: float = _float("NL_SHARE_THRESHOLD", 0.8)
        self.gap_warning_km: float = _float("GAP_WARNING_KM", 40.0)
        self.max_upload_mb: int = _int("MAX_UPLOAD_MB", 25)
        self.job_ttl_seconds: int = _int("JOB_TTL_SECONDS", 6 * 3600)

        # Wegwerkzaamheden (NDW/Melvin planningsfeed)
        self.roadworks_enabled: bool = os.getenv(
            "ROADWORKS_ENABLED", "true"
        ).lower() in ("1", "true", "yes", "on")
        self.roadworks_url: str = os.getenv(
            "ROADWORKS_URL",
            "https://opendata.ndw.nu/planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz",
        )
        self.roadworks_cache_ttl_seconds: int = _int(
            "ROADWORKS_CACHE_TTL_SECONDS", 24 * 3600
        )
        # Een melding telt als het afgesloten stuk binnen deze afstand van de
        # route komt. Gemeten op 165 routes: echte treffers liggen op 0-10 m,
        # meldingen op een parallelle straat vrijwel allemaal boven de 50 m.
        self.roadworks_line_tolerance_m: int = _int("ROADWORKS_LINE_TOLERANCE_M", 25)
        # Terugval voor meldingen zonder afgesloten stuk (~7%): afstand tot het punt.
        self.roadworks_radius_m: int = _int("ROADWORKS_RADIUS_M", 100)
        self.roadworks_timeout: int = _int("ROADWORKS_TIMEOUT", 300)
        self.roadworks_prefix: str = os.getenv(
            "ROADWORKS_PREFIX", "\u26a0\ufe0f Werkzaamheden"
        )
        self.roadworks_sym: str = os.getenv("ROADWORKS_SYM", "Danger Area")
        self.roadworks_type: str = os.getenv("ROADWORKS_TYPE", "Roadworks")
        # Meldingen van dezelfde klus die elkaar langs de route binnen deze
        # afstand opvolgen worden samengevoegd tot een enkele waarschuwing.
        self.roadworks_merge_gap_m: int = _int("ROADWORKS_MERGE_GAP_M", 250)

        # Controle op verboden paden (lokale OSM-wegenkaart, alleen Nederland)
        self.legality_enabled: bool = os.getenv(
            "LEGALITY_ENABLED", "true"
        ).lower() in ("1", "true", "yes", "on")
        self.osm_pbf_url: str = os.getenv(
            "OSM_PBF_URL",
            "https://download.geofabrik.de/europe/netherlands-latest.osm.pbf",
        )
        self.osm_max_age_days: int = _int("OSM_MAX_AGE_DAYS", 30)
        # Opbouwen piekt rond 2,5 GB; bij minder vrij geheugen wachten we.
        self.osm_min_free_mb: int = _int("OSM_MIN_FREE_MB", 2800)
        # Willekeurige wachttijd voor de eerste controle, zodat meerdere
        # installaties op dezelfde server niet tegelijk gaan bouwen.
        self.osm_start_jitter_seconds: int = _int("OSM_START_JITTER_SECONDS", 1800)
        self.legality_prefix_forbidden: str = os.getenv(
            "LEGALITY_PREFIX_FORBIDDEN", "\u26d4 Verboden"
        )
        self.legality_prefix_warning: str = os.getenv(
            "LEGALITY_PREFIX_WARNING", "\u2757 Let op"
        )
        self.legality_sym: str = os.getenv("LEGALITY_SYM", "Danger Area")

        # Regencontrole: KNMI-model (via Open-Meteo) + Buienradar-radar
        self.weather_enabled: bool = os.getenv(
            "WEATHER_ENABLED", "true"
        ).lower() in ("1", "true", "yes", "on")
        self.weather_api_url: str = os.getenv(
            "WEATHER_API_URL", "https://api.open-meteo.com/v1/forecast"
        )
        self.weather_model: str = os.getenv("WEATHER_MODEL", "knmi_seamless")
        self.weather_radar_url: str = os.getenv(
            "WEATHER_RADAR_URL", "https://gpsgadget.buienradar.nl/data/raintext"
        )
        self.weather_timezone: str = os.getenv("WEATHER_TIMEZONE", "Europe/Amsterdam")
        self.default_speed_kmh: float = _float("DEFAULT_SPEED_KMH", 30.0)
        # Vanaf deze intensiteit telt een stuk als "nat" (0,1 mm/u = motregen).
        self.weather_rain_threshold_mm_h: float = _float("WEATHER_RAIN_THRESHOLD_MM_H", 0.1)
        self.weather_max_days: int = _int("WEATHER_MAX_DAYS", 7)
        # Model zegt regen maar de kans is lager dan dit: "mogelijk regen".
        self.weather_uncertain_probability: int = _int("WEATHER_UNCERTAIN_PROBABILITY", 30)
        self.weather_timeout: int = _int("WEATHER_TIMEOUT", 20)
        self.weather_prefix: str = os.getenv("WEATHER_PREFIX", "\U0001f327\ufe0f Regen")
        self.weather_prefix_uncertain: str = os.getenv(
            "WEATHER_PREFIX_UNCERTAIN", "\U0001f326\ufe0f Mogelijk regen"
        )
        self.weather_sym: str = os.getenv("WEATHER_SYM", "Danger Area")

        # Caches (drinkwaterpunten.nl en NDW) op de achtergrond vers houden,
        # zodat een bezoeker nooit op een download hoeft te wachten.
        self.background_refresh: bool = os.getenv(
            "BACKGROUND_REFRESH", "true"
        ).lower() in ("1", "true", "yes", "on")
        self.background_refresh_interval_seconds: int = _int(
            "BACKGROUND_REFRESH_INTERVAL_SECONDS", 3600
        )

        # Waypoints
        self.waypoint_prefix: str = os.getenv("WAYPOINT_PREFIX", "\U0001f4a7 Water")
        self.waypoint_sym: str = os.getenv("WAYPOINT_SYM", "Water Source")
        self.waypoint_type: str = os.getenv("WAYPOINT_TYPE", "Water")
        self.waypoint_with_km: bool = os.getenv("WAYPOINT_WITH_KM", "true").lower() in (
            "1",
            "true",
            "yes",
        )

        # Strava. "auto" = aan zodra client id en secret ingevuld zijn.
        # Zet expliciet op false voor een publieke installatie zonder Strava.
        self.strava_setting: str = os.getenv("STRAVA_ENABLED", "auto").strip().lower()
        self.strava_client_id: str = os.getenv("STRAVA_CLIENT_ID", "").strip()
        self.strava_client_secret: str = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
        self.strava_redirect_uri: str = os.getenv("STRAVA_REDIRECT_URI", "").strip()
        self.strava_scope: str = os.getenv("STRAVA_SCOPE", "read,read_all")
        self.strava_api_base: str = os.getenv(
            "STRAVA_API_BASE", "https://www.strava.com/api/v3"
        )
        self.strava_oauth_base: str = os.getenv(
            "STRAVA_OAUTH_BASE", "https://www.strava.com/oauth"
        )
        self.strava_max_routes: int = _int("STRAVA_MAX_ROUTES", 100)
        # Bestaande tokens hergebruiken zonder OAuth-flow (single-user opstelling)
        self.strava_access_token: str = os.getenv("STRAVA_ACCESS_TOKEN", "").strip()
        self.strava_refresh_token: str = os.getenv("STRAVA_REFRESH_TOKEN", "").strip()
        self.strava_expires_at: float = _float("STRAVA_EXPIRES_AT", 0.0)
        self.session_cookie: str = os.getenv("SESSION_COOKIE", "gpxw_session")
        self.session_ttl_seconds: int = _int("SESSION_TTL_SECONDS", 30 * 24 * 3600)
        self.secret_key: str = os.getenv("SECRET_KEY", "").strip()
        self.cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() in (
            "1",
            "true",
            "yes",
        )

        self.user_agent: str = os.getenv(
            "USER_AGENT", "gpx-waterpoints/1.0 (self-hosted route tool)"
        )

        # Routeboek.cc: routes van een clubpagina scrapen (geen officiële API).
        self.routeboek_enabled: bool = os.getenv(
            "ROUTEBOEK_ENABLED", "true"
        ).lower() in ("1", "true", "yes", "on")
        self.routeboek_base_url: str = os.getenv(
            "ROUTEBOEK_BASE_URL", "https://routeboek.cc"
        ).strip()
        self.routeboek_club_slug: str = os.getenv("ROUTEBOEK_CLUB_SLUG", "stampers").strip()
        # Kort cachen zodat een paginaverversing niet steeds opnieuw scrapt;
        # geen volledige periodieke sync van alle GPX's (onnodige belasting).
        self.routeboek_cache_ttl_seconds: int = _int("ROUTEBOEK_CACHE_TTL_SECONDS", 15 * 60)

    @property
    def strava_feature_enabled(self) -> bool:
        """Is de Strava-module überhaupt ingeschakeld?

        Staat de schakelaar uit, dan worden de endpoints en de pagina niet
        geregistreerd: de applicatie draait dan puur als GPX-uploadtool.
        """
        if self.strava_setting in ("0", "false", "no", "off", "disabled"):
            return False
        if self.strava_setting in ("1", "true", "yes", "on", "enabled"):
            return True
        return bool(self.strava_client_id and self.strava_client_secret)

    @property
    def strava_credentials(self) -> bool:
        """Zijn client id en secret ingevuld?"""
        return self.strava_feature_enabled and bool(
            self.strava_client_id and self.strava_client_secret
        )

    @property
    def strava_oauth_enabled(self) -> bool:
        """Kan de OAuth-koppelknop gebruikt worden?"""
        return self.strava_credentials and bool(self.strava_redirect_uri)

    @property
    def strava_token_configured(self) -> bool:
        """Is er een vast refresh token ingesteld (koppelen niet nodig)?"""
        return self.strava_credentials and bool(self.strava_refresh_token)

    @property
    def strava_enabled(self) -> bool:
        """Strava-integratie is bruikbaar via OAuth of via een vast token."""
        return self.strava_oauth_enabled or self.strava_token_configured

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def configure_logging() -> None:
    """Logging naar stdout, zodat Docker de logs oppakt."""
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
