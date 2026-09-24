"""Versleutelde opslag van Strava-tokens per browsersessie.

Tokens staan nooit in leesbare vorm op schijf: het bestand wordt met Fernet
(AES-128-CBC + HMAC) versleuteld en met rechten 0600 weggeschreven. De sleutel
komt uit `SECRET_KEY` of wordt eenmalig gegenereerd in de datamap.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STORE_FILENAME = "strava_tokens.enc"
_KEY_FILENAME = "secret.key"


def _key_file() -> Path:
    return get_settings().data_dir / _KEY_FILENAME


def _store_file() -> Path:
    return get_settings().data_dir / _STORE_FILENAME


def _write_private(path: Path, data: bytes) -> None:
    """Schrijf atomisch weg met rechten 0600."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
        fh.write(data)
    tmp.replace(path)
    os.chmod(path, 0o600)


def get_secret() -> str:
    """Geef het applicatiegeheim; genereer en bewaar er één als het ontbreekt."""
    settings = get_settings()
    if settings.secret_key:
        return settings.secret_key
    settings.ensure_dirs()
    path = _key_file()
    with _LOCK:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        generated = secrets.token_urlsafe(48)
        _write_private(path, generated.encode("utf-8"))
        logger.info("Nieuw applicatiegeheim aangemaakt in %s", path)
        return generated


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _load_all() -> dict[str, dict[str, Any]]:
    path = _store_file()
    if not path.exists():
        return {}
    try:
        raw = _fernet().decrypt(path.read_bytes())
    except (InvalidToken, ValueError):
        logger.warning("Tokenopslag onleesbaar (ander SECRET_KEY?); opnieuw begonnen")
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_all(data: dict[str, dict[str, Any]]) -> None:
    get_settings().ensure_dirs()
    payload = _fernet().encrypt(json.dumps(data).encode("utf-8"))
    _write_private(_store_file(), payload)


def _prune(data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cutoff = time.time() - get_settings().session_ttl_seconds
    return {k: v for k, v in data.items() if v.get("updated_at", 0) >= cutoff}


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def get(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with _LOCK:
        return _load_all().get(session_id)


def put(session_id: str, payload: dict[str, Any]) -> None:
    with _LOCK:
        data = _prune(_load_all())
        payload = dict(payload)
        payload["updated_at"] = time.time()
        data[session_id] = payload
        _save_all(data)


def delete(session_id: str) -> None:
    with _LOCK:
        data = _load_all()
        if data.pop(session_id, None) is not None:
            _save_all(data)
