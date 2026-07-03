"""Pagina unificata delle impostazioni.

Solo la pagina HTML: i dati viaggiano sulle API esistenti
(``/runtime_config``, ``/api/runtime_config``, ``/api/telegram_config``,
``/api/agente/*``, ``/api/disk_estimate``), nessun endpoint nuovo.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from blackframe.auth import require_auth

settings_bp = Blueprint("settings", __name__)


@settings_bp.get("/impostazioni")
@require_auth()
def impostazioni_page():
    return render_template("impostazioni.html")
