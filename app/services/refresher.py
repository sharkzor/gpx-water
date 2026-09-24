"""Houdt de externe caches op de achtergrond vers.

Zonder deze lus ververst een cache pas wanneer een bezoeker hem nodig heeft,
waardoor die bezoeker op de download (NDW: ~15 MB) moet wachten. Een instantie
die weken niet gebruikt wordt, zou bovendien met sterk verouderde data starten.

De lus kijkt elk interval of een cache ouder is dan zijn TTL en ververst dan.
Het verversen zelf loopt via de bestaande services, inclusief hun lock en
terugval op de oude cache bij een storing.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.config import get_settings
from app.services import roadworks_nl, waterpoints_nl

logger = logging.getLogger(__name__)

_started = threading.Event()
_stop = threading.Event()


def _is_stale(age: float | None, ttl: int) -> bool:
    return age is None or age >= ttl


def refresh_stale_caches() -> list[str]:
    """Ververs alle verouderde caches; geeft de namen terug die ververst zijn."""
    settings = get_settings()
    jobs: list[tuple[str, bool, Callable[[], object]]] = [
        (
            "drinkwaterpunten.nl",
            _is_stale(waterpoints_nl.cache_age_seconds(), settings.nl_cache_ttl_seconds),
            waterpoints_nl.load_water_points,
        ),
    ]
    if settings.roadworks_enabled:
        jobs.append(
            (
                "NDW-wegwerkzaamheden",
                _is_stale(
                    roadworks_nl.cache_age_seconds(),
                    settings.roadworks_cache_ttl_seconds,
                ),
                roadworks_nl.get_road_works,
            )
        )

    refreshed: list[str] = []
    for name, stale, refresh in jobs:
        if not stale:
            continue
        try:
            refresh()
            refreshed.append(name)
        except Exception:  # noqa: BLE001 - de lus mag nooit sterven
            logger.exception("Achtergrondverversing %s mislukt", name)
    if refreshed:
        logger.info("Achtergrond: %s ververst", ", ".join(refreshed))
    return refreshed


def start() -> bool:
    """Start de verversingslus eenmalig; geeft False als hij uit staat."""
    settings = get_settings()
    if not settings.background_refresh or _started.is_set():
        return False
    _started.set()
    _stop.clear()
    interval = max(60, settings.background_refresh_interval_seconds)

    def loop() -> None:
        while not _stop.is_set():
            refresh_stale_caches()
            _stop.wait(interval)

    threading.Thread(target=loop, name="cache-refresher", daemon=True).start()
    logger.info("Achtergrondverversing gestart (controle elke %d s)", interval)
    return True


def stop() -> None:
    _stop.set()
    _started.clear()
