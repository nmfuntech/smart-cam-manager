"""Client HTTP minimale verso Ollama.

Stesso stile stdlib (``urllib.request``, nessuna dipendenza nuova) già usato
da ``telegram_commands._bot_api_call`` e ``classification.CloudBackend`` per
chiamate HTTP dirette. Nessun errore risale mai al chiamante: rete assente,
timeout o JSON non valido sono tutti trattati come "assistente non
disponibile" (``None``), coerente con l'approccio fail-closed già usato per
l'automazione smart-home quando Ollama non è raggiungibile.

Politica di retry, pensata per il budget di latenza stretto del mini PC:

- errore di connessione (refused/reset): un solo retry immediato — Ollama
  potrebbe essere appena (ri)partito;
- HTTP 400 con ``format`` a schema JSON: un retry con ``format: "json"`` —
  le versioni di Ollama precedenti alla 0.5 non supportano gli structured
  outputs e rifiutano il payload;
- timeout: MAI — ``AGENT_TIMEOUT_SEC`` è già l'intero budget percepito
  dall'utente, un retry lo raddoppierebbe.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def _keep_alive_value(value: str | None) -> str | int | None:
    """Ollama richiede numeri negativi come JSON number, non stringhe."""
    if value is None:
        return None
    rendered = str(value).strip()
    if rendered.lstrip("-").isdigit():
        return int(rendered)
    return rendered or None


def _base_url_allowed(base_url: str) -> bool:
    try:
        parsed = urlsplit(base_url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if os.getenv("AGENT_ALLOW_REMOTE_OLLAMA", "false").lower() in {"1", "true", "yes", "on"}:
        return True
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _api_url(base_url: str, path: str) -> str | None:
    if not _base_url_allowed(base_url):
        logger.error(
            "Endpoint Ollama non consentito: usa loopback o AGENT_ALLOW_REMOTE_OLLAMA=true"
        )
        return None
    return f"{base_url.rstrip('/')}{path}"


def _is_transient_conn_error(exc: Exception) -> bool:
    """Solo gli errori di connessione "istantanei" meritano un retry: un
    timeout ha già consumato il budget di latenza e non va mai ritentato."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (ConnectionRefusedError, ConnectionResetError, BrokenPipeError))


def _request(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _request_with_retry(url: str, payload: dict, timeout: float) -> dict:
    try:
        return _request(url, payload, timeout)
    except (urllib.error.URLError, OSError) as exc:
        if not _is_transient_conn_error(exc):
            raise
        logger.info("Connessione a Ollama fallita (%s), un retry immediato", exc)
        return _request(url, payload, timeout)


def _message_content(body: dict) -> str | None:
    content = (body.get("message") or {}).get("content")
    if not content or not isinstance(content, str):
        logger.warning("Risposta Ollama senza contenuto utilizzabile: %r", body)
        return None
    return content


def _chat_payload(
    model: str,
    system_prompt: str,
    user_text: str,
    *,
    keep_alive: str | None,
    history: list[dict] | None,
    options: dict | None,
) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *(history or []),
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {"temperature": 0, **(options or {})},
    }
    if keep_alive:
        payload["keep_alive"] = _keep_alive_value(keep_alive)
    return payload


def chat_json(
    base_url: str,
    model: str,
    system_prompt: str,
    user_text: str,
    *,
    timeout: float = 8.0,
    keep_alive: str | None = None,
    history: list[dict] | None = None,
    response_schema: dict | None = None,
    options: dict | None = None,
) -> dict | None:
    """Chiama ``POST {base_url}/api/chat`` in modalità JSON e ritorna il
    contenuto già parsato come dict, o ``None`` su qualunque errore.

    ``keep_alive`` (es. ``"30m"``) tiene il modello residente in RAM tra una
    richiesta e l'altra: senza specificarlo Ollama usa il default di 5 minuti
    e ogni richiesta dopo un periodo di inattività paga il costo di ricarica
    del modello da disco, che su hardware limitato può superare ``timeout``.

    ``response_schema`` (JSON Schema) vincola la *generazione* del modello
    alla forma attesa (structured outputs, Ollama >= 0.5): un modello piccolo
    non può proprio emettere un nome comando fuori dall'enum. Se il server
    rifiuta lo schema (HTTP 400, versione vecchia) si ripiega su
    ``format: "json"`` generico.

    ``history`` è una lista di messaggi ``{"role", "content"}`` inserita tra
    il system prompt e il messaggio utente (contesto conversazionale).
    """
    url = _api_url(base_url, "/api/chat")
    if url is None:
        return None
    payload = _chat_payload(
        model, system_prompt, user_text, keep_alive=keep_alive, history=history, options=options
    )
    payload["format"] = response_schema if response_schema else "json"

    try:
        body = _request_with_retry(url, payload, timeout)
    except urllib.error.HTTPError as exc:
        if response_schema is None or exc.code != 400:
            logger.warning("Chiamata Ollama fallita (%s): %s", base_url, exc)
            return None
        logger.info("Ollama ha rifiutato lo schema JSON (HTTP 400), ripiego su format=json")
        payload["format"] = "json"
        try:
            body = _request_with_retry(url, payload, timeout)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as retry_exc:
            logger.warning("Chiamata Ollama fallita (%s): %s", base_url, retry_exc)
            return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Chiamata Ollama fallita (%s): %s", base_url, exc)
        return None

    content = _message_content(body)
    if content is None:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Ollama ha risposto con JSON non valido: %r", content[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def chat_text(
    base_url: str,
    model: str,
    system_prompt: str,
    user_text: str,
    *,
    timeout: float = 8.0,
    keep_alive: str | None = None,
    options: dict | None = None,
) -> str | None:
    """Come ``chat_json`` ma senza vincolo di formato: ritorna il testo libero
    generato dal modello (usato per comporre risposte in italiano naturale),
    o ``None`` su qualunque errore."""
    url = _api_url(base_url, "/api/chat")
    if url is None:
        return None
    payload = _chat_payload(
        model, system_prompt, user_text, keep_alive=keep_alive, history=None, options=options
    )
    try:
        body = _request_with_retry(url, payload, timeout)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Chiamata Ollama fallita (%s): %s", base_url, exc)
        return None
    content = _message_content(body)
    return content.strip() if content else None


def warmup(
    base_url: str,
    model: str,
    *,
    keep_alive: str | None = None,
    timeout: float = 120.0,
) -> None:
    """Precarica il modello in RAM (``POST /api/generate`` senza prompt è la
    chiamata "load model" documentata da Ollama). Best-effort: qualunque
    errore viene solo loggato — il warm-up non deve mai bloccare l'avvio.

    Il timeout è volutamente generoso: sul mini PC il caricamento da disco
    può superare di molto ``AGENT_TIMEOUT_SEC``, ed è proprio il costo che
    questo warm-up paga in anticipo al posto del primo messaggio utente.
    """
    url = _api_url(base_url, "/api/generate")
    if url is None:
        return
    payload: dict = {"model": model}
    if keep_alive:
        payload["keep_alive"] = _keep_alive_value(keep_alive)
    try:
        _request(url, payload, timeout)
        logger.info("Warm-up Ollama completato: modello %s residente", model)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Warm-up Ollama fallito (%s): %s", base_url, exc)
