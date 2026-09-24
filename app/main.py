"""FastAPI applicatie: webinterface en API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import configure_logging, get_settings
from app.routers import core, routeboek, strava
from app.services import osm_index, processing, refresher
from app.web import APP_VERSION, STATIC_DIR

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    processing.cleanup_jobs()
    logger.info(
        "Applicatie gestart, datamap: %s, Strava: %s",
        settings.data_dir,
        _strava_state(),
    )
    refresher.start()
    if settings.legality_enabled and settings.background_refresh:
        osm_index.ensure_fresh_in_background()
    yield
    refresher.stop()
    logger.info("Applicatie gestopt")


def _strava_state() -> str:
    if not settings.strava_feature_enabled:
        return "uitgeschakeld"
    return "geconfigureerd" if settings.strava_enabled else "niet geconfigureerd"


app = FastAPI(title="GPX Drinkwaterpunten", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(core.router)
app.include_router(strava.router)
app.include_router(routeboek.router)
