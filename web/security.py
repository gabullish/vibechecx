"""web/security.py — security middleware and helpers.

Covers:
- Security response headers (clickjacking, MIME sniffing, CSP, referrer)
- Login rate limiting (per-IP, DB-backed, 10 attempts / 5 min — works across workers)
- Server header suppression via uvicorn server_header=False
"""
import logging
import psycopg2
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG as _DB  # noqa: E402

from starlette.datastructures import MutableHeaders
from starlette.requests import Request

logger = logging.getLogger("vibechecx.security")

# ── Security headers ──────────────────────────────────────────────────

_SCRIPT_SOURCES = " ".join([
    "'self'",
    "https://cdn.tailwindcss.com",
    "https://unpkg.com",
    "'unsafe-inline'",   # HTMX hx-on:* and Alpine x-data need this
])
_IMG_SOURCES = " ".join([
    "'self'",
    "data:",
    "https://pbs.twimg.com",   # Twitter profile avatars
    "https://abs.twimg.com",
])
_CSP = (
    f"default-src 'self'; "
    f"script-src {_SCRIPT_SOURCES}; "
    f"style-src 'self' 'unsafe-inline'; "
    f"img-src {_IMG_SOURCES}; "
    f"connect-src 'self'; "
    f"font-src 'self'; "
    f"frame-ancestors 'none';"   # stronger than X-Frame-Options
)

_SECURITY_HEADERS = {
    "X-Content-Type-Options":    "nosniff",
    "X-Frame-Options":           "DENY",
    "Referrer-Policy":           "strict-origin-when-cross-origin",
    "Permissions-Policy":        "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy":   _CSP,
    # Browsers ignore HSTS received over plain HTTP (local laptop), so this is
    # harmless locally and pins HTTPS for a year on Render. No preload (hard to
    # reverse, not needed).
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware:
    """Pure ASGI middleware that stamps security headers onto every
    response. Previously used Starlette's BaseHTTPMiddleware, but that
    class has a known regression on Python 3.14 where call_next() can
    return None during exception handling — which then surfaced as a
    cryptic global ``'NoneType' object has no attribute 'headers'``
    error masking the real exception. Pure ASGI sidesteps that whole
    code path and is measurably faster (no extra task hop)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Stash the canonical Request in a contextvar so any route can
        # recover it even when FastAPI's parameter binding for sync
        # endpoints decays on Python 3.14 (passes {} for r: Request).
        # contextvars propagate through anyio.to_thread.run_sync, which
        # is what FastAPI uses for sync endpoints — so this is safe for
        # every code path.
        from web.core import current_request
        token = current_request.set(Request(scope, receive=receive, send=send))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in _SECURITY_HEADERS.items():
                    headers[k] = v
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            current_request.reset(token)


# ── Login rate limiting ───────────────────────────────────────────────
# DB-backed so it works correctly across multiple uvicorn worker processes.
# Table created on first use if it doesn't exist.

_WINDOW_SECONDS = 300   # 5 minutes
_MAX_ATTEMPTS   = 10    # per IP per window

_TABLE_READY = False


def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        conn = psycopg2.connect(**_DB)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip TEXT NOT NULL,
                attempted_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip, attempted_at)"
        )
        conn.commit()
        conn.close()
        _TABLE_READY = True
    except Exception:
        logger.warning("login_attempts DDL failed (likely pooler perms) — table assumed existing", exc_info=True)
        _TABLE_READY = True  # Assume it exists; query will fail at runtime if not.


def _client_ip(request) -> str:
    """Get real IP, honouring Cloudflare / proxy forwarding headers.
    Tolerates an ASGI scope dict in place of a Request — see _as_request."""
    if isinstance(request, dict):
        request = Request(request)
    for header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        val = request.headers.get(header)
        if val:
            return val.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def record_failed_login(request: Request) -> None:
    """Record a failed login attempt. Silently degrades if table doesn't exist."""
    try:
        _ensure_table()
        ip = _client_ip(request)
        conn = psycopg2.connect(**_DB)
        cur = conn.cursor()
        cur.execute("INSERT INTO login_attempts (ip) VALUES (%s)", (ip,))
        cur.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip=%s AND attempted_at > NOW() - INTERVAL '5 minutes'",
            (ip,),
        )
        count = cur.fetchone()[0]
        conn.commit()
        conn.close()
        if count >= _MAX_ATTEMPTS:
            logger.warning("Login rate limit hit: %s (%d failures in %ds)", ip, count, _WINDOW_SECONDS)
    except Exception:
        logger.debug("record_failed_login skipped (table/perms issue)", exc_info=True)


def is_login_blocked(request: Request) -> bool:
    """Check if rate-limited. Returns False on any error (fails open)."""
    try:
        _ensure_table()
        ip = _client_ip(request)
        conn = psycopg2.connect(**_DB)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip=%s AND attempted_at > NOW() - INTERVAL '5 minutes'",
            (ip,),
        )
        count = cur.fetchone()[0]
        conn.close()
        return count >= _MAX_ATTEMPTS
    except Exception:
        return False


def clear_failed_logins(request: Request) -> None:
    """Call on successful login to reset the counter for this IP."""
    try:
        _ensure_table()
        ip = _client_ip(request)
        conn = psycopg2.connect(**_DB)
        cur = conn.cursor()
        cur.execute("DELETE FROM login_attempts WHERE ip=%s", (ip,))
        conn.commit()
        conn.close()
    except Exception:
        pass
