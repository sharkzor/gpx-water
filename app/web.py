"""Gedeelde Jinja2-omgeving voor de HTML-pagina's."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

#: Versie van de applicatie; wordt aan statische bestanden gehangen zodat
#: browsers na een update nooit een oude CSS/JS uit hun cache gebruiken.
APP_VERSION = "1.9.0"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["app_version"] = APP_VERSION
