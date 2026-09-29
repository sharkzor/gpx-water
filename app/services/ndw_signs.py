"""NDW-verkeersbordendatabase: geplaatste borden die fietsen verbieden.

Het NDW houdt een landelijk register bij van verkeersborden, met positie,
kijkrichting, onderborden en een foto. Wij gebruiken alleen de borden die
voor fietsers tellen:

* C14 — gesloten voor fietsers
* C1  — gesloten voor alle voertuigen (ook fietsen), vaak met uitzonderingen
* G7  — voetpad
* G9  — ruiterpad

Het register is gevuld vanaf de openbare weg (camera-auto's): borden op
paden diep in bos of natuurgebied ontbreken meestal. Het is daarom een
aanvulling op OpenStreetMap, geen vervanging. Het grote voordeel: een bord is
een juridisch verbod, en de foto laat zien wat er echt staat.

Gemeten (september 2026): ~75.000 borden in deze vier soorten, samen ~43 MB
JSON. Ruim 80% is voor het laatst gezien in 2022; de cache wordt wekelijks
ververst.

`bearing` is de rijrichting van het verkeer waarvoor het bord geldt (in
graden, 0 = noord). Dat is vastgesteld door 600 borden rond Utrecht naast de
lokale OSM-kaart te leggen: in Nederland staat een bord rechts van dat
verkeer, en in ~80% van de gevallen klopte de bearing met die rijrichting.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

SOURCE_NAME = "NDW-verkeersborden"
_LOCK = threading.Lock()
_CACHE_VERSION = 1

#: Rastergrootte (graden) voor snel opzoeken rond een route.
_CELL = 0.02


@dataclass(slots=True)
class Sign:
    id: str
    code: str
    lat: float
    lon: float
    bearing: float | None
    texts: list[str] = field(default_factory=list)
    image_url: str | None = None
    road: str | None = None
    town: str | None = None
    last_seen: str | None = None


# -- Beoordeling -------------------------------------------------------------

#: "fiets", "fietsers", "(brom)fietsers" of "rijwielen" — maar niet "snorfiets"
#: of "bromfiets", want dat zijn andere voertuigen.
_BIKE = r"(?<![a-z])(?:fiets|rijwiel)\w*"
_BIKE_WORD = re.compile(_BIKE)
#: Een verbod dat over fietsers zelf gaat: "dus niet fietsen", "verboden te
#: fietsen", "fietsen niet toegestaan", "geen fietspad". "Snorfietsen niet"
#: telt niet: dankzij de lookbehind in `_BIKE` is dat geen fietswoord.
_BAN = re.compile(
    rf"\b(?:niet|geen|verboden)\b[^,.;]{{0,15}}?{_BIKE}"
    rf"|{_BIKE}\s+(?:is\s+)?(?:niet|verboden)\b"
)
#: Vrijstelling: "fietsers toegestaan", "uitgezonderd fietsers", "te gast" …
_ALLOW = re.compile(r"toegestaan|uitgezonderd|te gast|gedoogd|m\.?\s?u\.?\s?v|behalve|vrijgesteld")
#: Een verbod of vrijstelling die van tijd of omstandigheid afhangt.
_CONDITION = re.compile(
    r"\d|winkel|tijd|koopavond|markt|\buur\b|afstappen"
)
_MOPED = re.compile(r"(?:snor|brom)-\s*(?:en\s+(?:snor|brom)-?\s*)?fiets")
#: Onderborden over fietsen parkeren gaan niet over rijden.
_PARKING = re.compile(r"plaats|parke|stall|zetten")

_LABELS = {
    "C14": ("forbidden", "ndw_c14", "Gesloten voor fietsers (bord C14)"),
    "C1": ("forbidden", "ndw_c1", "Gesloten voor alle voertuigen (bord C1)"),
    "G7": ("forbidden", "ndw_g7", "Voetpad (bord G7)"),
    "G9": ("forbidden", "ndw_g9", "Ruiterpad (bord G9)"),
}


def bike_rule(text: str) -> str | None:
    """Wat zegt een onderbord over fietsers?

    "ban" (uitdrukkelijk verboden), "exempt" (toegestaan), "conditional"
    (afhankelijk van tijd, of afstappen) of None (noemt fietsers niet).
    Gemeten op de ~1.500 onderborden met een fietswoord in het NDW-register.
    """
    # "snor- en brom- fietsen" gaat over bromfietsen, niet over fietsers.
    text = _MOPED.sub("snorfietsen", text.lower())
    if not _BIKE_WORD.search(text) or _PARKING.search(text):
        return None
    if "geldt niet voor" in text:
        return "exempt"
    conditional = bool(_CONDITION.search(text))
    if _BAN.search(text):
        # "uitgezonderd fietsers (geen doorgaand fietsverkeer)": wel fietsen,
        # maar met een voorwaarde.
        return "conditional" if conditional or "uitgezonderd" in text else "ban"
    if _ALLOW.search(text):
        return "conditional" if conditional else "exempt"
    return "conditional" if conditional else None


def interpret(sign: Sign) -> tuple[str, str, str] | None:
    """(severity, code, label) voor een fietser, of None als het bord niet telt."""
    base = _LABELS.get(sign.code)
    if base is None:
        return None
    severity, code, label = base
    texts = [t.strip() for t in sign.texts if t and t.strip()]
    rules = {t: bike_rule(t) for t in texts}

    banned = next((t for t, r in rules.items() if r == "ban"), None)
    if banned:
        return (severity, code, f"{label}, {banned.lower()}")
    if "exempt" in rules.values():
        return None
    conditional = next((t for t, r in rules.items() if r == "conditional"), None)
    if conditional:
        return ("warning", code, f"{label}, {conditional.lower()}")
    if not sign.texts or sign.code == "C14":
        # Geen onderbord, of een C14 met een onderbord dat fietsers niet noemt
        # (bijv. "uitgezonderd snorfietsen"): het verbod blijft staan.
        return base

    # Er hangt een onderbord dat fietsers niet noemt. Dat kan een pictogram van
    # een fiets zijn dat als tekst ontbreekt (NDW laat de tekst dan leeg), of
    # een uitzondering voor bijvoorbeeld bestemmingsverkeer. Dan is het "let
    # op" met de foto.
    shown = next((t.lower() for t in texts if t.lower() != "onleesbaar"), "")
    if shown:
        extra = f", {shown}"
    elif texts:
        extra = ", onderbord onleesbaar"
    else:
        extra = ", onderbord zonder tekst"
    return ("warning", code, f"{label}{extra}")


# -- Cache -------------------------------------------------------------------


def _cache_file() -> Path:
    return get_settings().cache_dir / f"ndw_verkeersborden_v{_CACHE_VERSION}.json"


def cache_age_seconds() -> float | None:
    path = _cache_file()
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _compact(feature: dict) -> dict | None:
    try:
        lon, lat = feature["geometry"]["coordinates"][:2]
        props = feature["properties"]
    except (KeyError, TypeError, ValueError):
        return None
    if props.get("status") not in (None, "PLACED"):
        return None
    image = props.get("imageUrl")
    return {
        "id": str(feature.get("id") or ""),
        "code": str(props.get("rvvCode") or "").upper(),
        "lat": round(float(lat), 7),
        "lon": round(float(lon), 7),
        "bearing": props.get("bearing"),
        "texts": [str(t.get("text") or "") for t in props.get("textSigns") or []],
        "image_url": image if isinstance(image, str) and image.startswith("https://") else None,
        "road": props.get("roadName"),
        "town": props.get("townName"),
        "last_seen": props.get("lastSeenOn"),
    }


def refresh_cache() -> list[dict]:
    """Download alle relevante borden en schrijf de compacte cache."""
    settings = get_settings()
    settings.ensure_dirs()
    signs: list[dict] = []
    for code in settings.ndw_signs_codes:
        response = requests.get(
            settings.ndw_signs_url,
            params={"rvvCode": code},
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=180,
        )
        response.raise_for_status()
        features = response.json().get("features") or []
        found = [c for c in (_compact(f) for f in features) if c and c["code"] == code]
        logger.info("NDW-verkeersborden %s: %d geplaatst", code, len(found))
        signs.extend(found)
    if not signs:
        raise RuntimeError("NDW gaf geen verkeersborden terug")
    path = _cache_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(signs, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    for old in path.parent.glob("ndw_verkeersborden_v*.json"):
        if old != path:
            old.unlink(missing_ok=True)
    return signs


_memory: dict[str, object] = {"mtime": None, "grid": {}}
_REFRESH_LOCK = threading.Lock()
#: Na een mislukte download zo lang wachten voor een nieuwe poging.
RETRY_AFTER_SECONDS = 3600
_last_failure: list[float] = [0.0]


def _grid(raw: list[dict]) -> dict[tuple[int, int], list[Sign]]:
    grid: dict[tuple[int, int], list[Sign]] = {}
    for entry in raw:
        sign = Sign(**entry)
        key = (int(sign.lat // _CELL), int(sign.lon // _CELL))
        grid.setdefault(key, []).append(sign)
    return grid


def _refresh(force: bool) -> None:
    """Ververs de cache, met één download tegelijk en terugval op de oude."""
    settings = get_settings()
    with _REFRESH_LOCK:
        age = cache_age_seconds()
        if not force and age is not None and age < settings.ndw_signs_ttl_seconds:
            return  # een andere thread was ons voor
        waited = time.time() - _last_failure[0]
        if not force and waited < RETRY_AFTER_SECONDS:
            if age is None:
                raise RuntimeError(
                    "De NDW-verkeersborden zijn niet bereikbaar; "
                    f"nieuwe poging over {int((RETRY_AFTER_SECONDS - waited) / 60) + 1} minuten."
                )
            return
        try:
            refresh_cache()
        except Exception as exc:
            _last_failure[0] = time.time()
            if age is None:
                raise RuntimeError(f"Kan de NDW-verkeersborden niet ophalen: {exc}") from exc
            logger.warning("Verversen NDW-verkeersborden mislukt (%s); oude cache gebruikt", exc)


def _load(force_refresh: bool = False) -> dict[tuple[int, int], list[Sign]]:
    settings = get_settings()
    age = cache_age_seconds()
    stale = age is not None and age >= settings.ndw_signs_ttl_seconds
    # Met achtergrondverversing wacht een bezoeker nooit op een download als
    # er al (verouderde) borden zijn; de achtergrondlus ververst ze.
    if force_refresh or age is None or (stale and not settings.background_refresh):
        _refresh(force_refresh)
    path = _cache_file()
    with _LOCK:
        mtime = path.stat().st_mtime
        if _memory["mtime"] != mtime:
            _memory["grid"] = _grid(json.loads(path.read_text(encoding="utf-8")))
            _memory["mtime"] = mtime
        return _memory["grid"]  # type: ignore[return-value]


def get_signs(force_refresh: bool = False) -> int:
    """Laad (en ververs zo nodig) de borden; geeft het aantal terug."""
    return sum(len(v) for v in _load(force_refresh).values())


def signs_in_bbox(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> list[Sign]:
    grid = _load()
    found: list[Sign] = []
    for i in range(int(min_lat // _CELL), int(max_lat // _CELL) + 1):
        for j in range(int(min_lon // _CELL), int(max_lon // _CELL) + 1):
            for sign in grid.get((i, j), ()):
                if min_lat <= sign.lat <= max_lat and min_lon <= sign.lon <= max_lon:
                    found.append(sign)
    return found
