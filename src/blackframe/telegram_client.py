"""Single Telegram Bot API client used by notifications, commands and alerts.

Before this module three hand-written urllib clients lived in different files.
Every method returns Telegram's own response shape: ``{"ok": True, ...}`` on
success, ``{"ok": False, "description": "..."}`` on any failure (HTTP, network,
bad JSON). Nothing here raises, so callers can decide about retries.

The bot token is part of the URL: it is never logged and never included in the
error descriptions returned to callers.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TELEGRAM_API_BASE = "https://api.telegram.org"


def _error(description: str) -> dict:
    return {"ok": False, "description": description}


def build_multipart(
    fields: dict[str, str],
    file_field: str | None = None,
    filename: str | None = None,
    file_bytes: bytes | None = None,
    file_content_type: str = "application/octet-stream",
) -> tuple[str, bytes]:
    """Encode a multipart/form-data body. Returns (content_type, body)."""
    boundary = f"----blackframe{uuid.uuid4().hex}"
    crlf = b"\r\n"
    body = bytearray()
    for name, value in fields.items():
        body += b"--" + boundary.encode() + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += str(value).encode("utf-8") + crlf
    if file_field and file_bytes is not None:
        body += b"--" + boundary.encode() + crlf
        body += (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename or "file"}"'
        ).encode() + crlf
        body += f"Content-Type: {file_content_type}".encode() + crlf + crlf
        body += file_bytes + crlf
    body += b"--" + boundary.encode() + b"--" + crlf
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def _open(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return _error(f"HTTP {exc.code}: {body[:200]}")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return _error(f"Errore di rete: {reason}")
    except json.JSONDecodeError:
        return _error("Risposta Telegram non valida")


def api_call(token: str, method: str, params: dict | None = None, timeout: float = 15) -> dict:
    """Call a Bot API method with form-encoded parameters."""
    if not token:
        return _error("Token Telegram mancante")
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    return _open(req, timeout)


def send_message(
    token: str,
    chat_id: str,
    text: str,
    reply_markup: str | dict | None = None,
    silent: bool = False,
    timeout: float = 15,
) -> dict:
    params: dict[str, str] = {"chat_id": str(chat_id), "text": text}
    if reply_markup is not None:
        params["reply_markup"] = (
            reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup)
        )
    if silent:
        params["disable_notification"] = "true"
    return api_call(token, "sendMessage", params, timeout=timeout)


def send_media(
    token: str,
    method: str,
    field: str,
    chat_id: str,
    *,
    data: bytes | None = None,
    path: str | Path | None = None,
    filename: str | None = None,
    content_type: str = "application/octet-stream",
    caption: str = "",
    reply_markup: str | dict | None = None,
    silent: bool = False,
    extra: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """Upload a photo/video/document from bytes or from a file path."""
    if not token:
        return _error("Token Telegram mancante")
    if data is None:
        if path is None:
            return _error("Nessun file da inviare")
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            return _error(f"File non leggibile: {exc.strerror or exc}")
        filename = filename or Path(path).name
    fields: dict[str, str] = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    if reply_markup is not None:
        fields["reply_markup"] = (
            reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup)
        )
    if silent:
        fields["disable_notification"] = "true"
    if extra:
        fields.update(extra)
    body_type, body = build_multipart(fields, field, filename or field, data, content_type)
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", body_type)
    return _open(req, timeout)


def send_photo(token: str, chat_id: str, **kwargs) -> dict:
    kwargs.setdefault("filename", "blackframe.jpg")
    return send_media(token, "sendPhoto", "photo", chat_id, content_type="image/jpeg", **kwargs)


def send_video(token: str, chat_id: str, **kwargs) -> dict:
    kwargs.setdefault("filename", "clip.mp4")
    kwargs.setdefault("timeout", 120)
    extra = dict(kwargs.pop("extra", None) or {})
    extra.setdefault("supports_streaming", "true")
    return send_media(
        token, "sendVideo", "video", chat_id, content_type="video/mp4", extra=extra, **kwargs
    )
