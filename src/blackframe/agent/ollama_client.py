"""Client HTTP minimale verso Ollama.

Stesso stile stdlib (``urllib.request``, nessuna dipendenza nuova) già usato
da ``telegram_commands._bot_api_call`` e ``classification.CloudBackend`` per
chiamate HTTP dirette. Nessun errore risale mai al chiamante: rete assente,
timeout o JSON non valido sono tutti trattati come "assistente non
disponibile" (``None``), coerente con l'approccio fail-closed già usato per
l'automazione smart-home quando Ollama non è raggiungibile.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def chat_json(
    base_url: str,
    model: str,
    system_prompt: str,
    user_text: str,
    *,
    timeout: float = 8.0,
) -> dict | None:
    """Chiama ``POST {base_url}/api/chat`` in modalità JSON e ritorna il
    contenuto già parsato come dict, o ``None`` su qualunque errore."""
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Chiamata Ollama fallita (%s): %s", base_url, exc)
        return None

    content = (body.get("message") or {}).get("content")
    if not content or not isinstance(content, str):
        logger.warning("Risposta Ollama senza contenuto utilizzabile: %r", body)
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Ollama ha risposto con JSON non valido: %r", content[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
