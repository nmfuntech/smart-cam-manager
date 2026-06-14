import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

AUTH_SESSION_KEY = "blackframe_auth_user"
CSRF_SESSION_KEY = "blackframe_csrf_token"
DEFAULT_ADMIN_USERNAME = "admin"

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self):
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window_seconds - (now - bucket[0])))
                return False, retry_after
            bucket.append(now)
            return True, 0


rate_limiter = RateLimiter()


def _default_next_url() -> str:
    return url_for("video.index")


def _normalize_next_url(target: str | None) -> str:
    if not target:
        return _default_next_url()

    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return _default_next_url()
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return _default_next_url()

    normalized = parsed.path
    if parsed.query:
        normalized = f"{normalized}?{parsed.query}"
    return normalized


def get_admin_username() -> str:
    return os.getenv("APP_ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME


def get_admin_password() -> str | None:
    value = os.getenv("APP_ADMIN_PASSWORD", "").strip()
    return value or None


def get_admin_password_hash() -> str | None:
    value = os.getenv("APP_ADMIN_PASSWORD_HASH", "").strip()
    return value or None


def admin_password_configured() -> bool:
    return bool(get_admin_password_hash() or get_admin_password())


def verify_admin_password(provided: str) -> bool:
    """Verify a submitted password.

    Prefers APP_ADMIN_PASSWORD_HASH (a werkzeug PBKDF2/scrypt hash, so no plaintext
    secret is stored). Falls back to a constant-time comparison against the legacy
    plaintext APP_ADMIN_PASSWORD for backward compatibility.
    """
    hashed = get_admin_password_hash()
    if hashed:
        try:
            return check_password_hash(hashed, provided)
        except Exception:
            logger.exception("Verifica hash password fallita")
            return False
    expected = get_admin_password()
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


def is_authenticated() -> bool:
    return bool(session.get(AUTH_SESSION_KEY))


def ensure_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _trust_proxy_headers() -> bool:
    """Whether to trust client-supplied X-Forwarded-For for the client IP.

    Off by default: when the app is reachable directly, X-Forwarded-For is fully
    attacker-controlled and would let a client rotate the header to bypass the
    per-IP rate limits (login brute force). Enable only when a trusted reverse
    proxy that rewrites the header sits in front of the app.
    """
    return os.getenv("APP_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}


def _client_ip() -> str:
    if _trust_proxy_headers():
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or "unknown"
    return request.remote_addr or "unknown"


def _login_redirect_target() -> str:
    full_path = request.full_path or request.path or ""
    if full_path.endswith("?"):
        full_path = full_path[:-1]
    return _normalize_next_url(full_path)


def _wants_api_response() -> bool:
    if request.path.startswith("/api/"):
        return True
    return request.endpoint in {
        "video.stream_status",
        "video.stream_diagnostics",
        "motion.motion_status",
        "motion.runtime_config",
        "motion.motion_captures",
        "motion.motion_event",
        "ptz.ptz_status",
        "cameras.list_cameras",
        "cameras.current_wifi",
    }


def _unauthorized_response(api: bool | None = None):
    login_url = url_for("auth.login", next=_login_redirect_target())
    if api or (api is None and _wants_api_response()):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Autenticazione richiesta",
                    "redirect": login_url,
                }
            ),
            401,
        )
    return redirect(login_url)


def _csrf_failed_response(api: bool | None = None):
    if api or (api is None and _wants_api_response()):
        return jsonify({"ok": False, "error": "Token CSRF non valido"}), 403
    return redirect(url_for("auth.login", next=_login_redirect_target()))


def _has_valid_origin() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    return hmac.compare_digest(origin.rstrip("/"), request.host_url.rstrip("/"))


def _consume_rate_limit(scope: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    key = f"{scope}:{_client_ip()}"
    return rate_limiter.allow(key, limit, window_seconds)


def rate_limit(scope: str, limit: int, window_seconds: int, api: bool = False):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            allowed, retry_after = _consume_rate_limit(scope, limit, window_seconds)
            if allowed:
                return view(*args, **kwargs)
            response = (
                (
                    jsonify(
                        {
                            "ok": False,
                            "error": "Troppi tentativi. Riprova piu tardi.",
                        }
                    ),
                    429,
                )
                if api
                else (
                    render_template(
                        "login.html",
                        error=f"Troppi tentativi. Riprova tra {retry_after} secondi.",
                        next_url=_normalize_next_url(request.form.get("next")),
                    ),
                    429,
                )
            )
            result = response
            if isinstance(result, tuple):
                body, status = result
                headers = {"Retry-After": str(retry_after)}
                return body, status, headers
            result.headers["Retry-After"] = str(retry_after)
            return result

        return wrapped

    return decorator


def require_auth(api: bool = False):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not is_authenticated():
                return _unauthorized_response(api)
            ensure_csrf_token()
            return view(*args, **kwargs)

        return wrapped

    return decorator


def require_csrf(api: bool = False):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            expected = session.get(CSRF_SESSION_KEY)
            provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if (
                not expected
                or not provided
                or not hmac.compare_digest(str(provided), str(expected))
                or not _has_valid_origin()
            ):
                return _csrf_failed_response(api)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def configure_auth(app) -> None:
    bind_host = os.getenv("APP_BIND_HOST", "127.0.0.1").strip().lower()
    is_loopback = bind_host in {"127.0.0.1", "localhost", "::1"}
    configured_secret = os.getenv("APP_SECRET_KEY") or app.secret_key
    if not configured_secret and not is_loopback:
        # Off-loopback (i.e. reachable from the network) an ephemeral random key
        # would silently reset every session on restart and weaken sign-in
        # integrity. Force an explicit, persistent secret instead.
        raise RuntimeError(
            "APP_SECRET_KEY obbligatorio quando APP_BIND_HOST non è loopback. "
            'Generane uno con: make hash-password (o python -c "import secrets;'
            ' print(secrets.token_hex(32))").'
        )
    app.secret_key = configured_secret or secrets.token_hex(32)
    app.config.setdefault("STATIC_ASSET_VERSION", str(int(time.time())))
    secure_cookie_default = not is_loopback
    app.config["SESSION_COOKIE_NAME"] = "blackframe_session"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv(
        "APP_SESSION_COOKIE_SECURE", str(secure_cookie_default)
    ).strip().lower() in {"1", "true", "yes", "on"}
    csp = "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' data: blob:",
            "connect-src 'self'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
        ]
    )

    @app.context_processor
    def inject_security_context():
        context = {
            "static_asset_version": app.config["STATIC_ASSET_VERSION"],
        }
        if not is_authenticated():
            return {
                **context,
                "csrf_token": "",
                "auth_username": None,
            }
        return {
            **context,
            "csrf_token": ensure_csrf_token(),
            "auth_username": session.get(AUTH_SESSION_KEY),
        }

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.is_secure or app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


@auth_bp.get("/login")
def login():
    if is_authenticated():
        return redirect(_normalize_next_url(request.args.get("next")))
    return render_template(
        "login.html",
        error=None,
        next_url=_normalize_next_url(request.args.get("next")),
    )


@auth_bp.post("/login")
@rate_limit("login", limit=5, window_seconds=300)
def login_submit():
    password = str(request.form.get("password", "")).strip()
    next_url = _normalize_next_url(request.form.get("next"))
    if not admin_password_configured():
        return (
            render_template(
                "login.html",
                error=(
                    "Configura APP_ADMIN_PASSWORD (o APP_ADMIN_PASSWORD_HASH) "
                    "prima di usare BLACKFRAME."
                ),
                next_url=next_url,
            ),
            503,
        )

    if not verify_admin_password(password):
        return (
            render_template(
                "login.html",
                error="Password non valida.",
                next_url=next_url,
            ),
            401,
        )

    session.clear()
    session[AUTH_SESSION_KEY] = get_admin_username()
    session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    return redirect(next_url)


@auth_bp.post("/logout")
@require_auth()
@require_csrf()
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
