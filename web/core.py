"""web/core.py — DB + auth + profile helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from vibechecx_config import DB_CONFIG  # noqa: E402
from vibechecx_auth import get_user_from_session  # noqa: E402

DB = DB_CONFIG  # back-compat alias


# ── Connection / query helpers ────────────────────────────────────────

_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None


def _get_pool() -> "psycopg2.pool.ThreadedConnectionPool":
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 8,
            cursor_factory=RealDictCursor,
            options="-c timezone=UTC",
            **DB_CONFIG,
        )
    return _pool


def q(sql, params=None):
    """Execute a query, return list of dicts. Uses a connection pool."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                conn.commit()
                return []
            rows = cur.fetchall()
        conn.commit()
        return [dict(r) for r in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


import contextvars as _cv

# Module-level contextvar that holds the in-flight Starlette Request.
# Set by SecurityHeadersMiddleware on every HTTP request; reset on exit.
# contextvars propagate through anyio.to_thread.run_sync (which FastAPI
# uses for sync endpoints), so this works for every route regardless of
# whether it's async def or plain def.
current_request: "_cv.ContextVar[object | None]" = _cv.ContextVar(
    "vibechecx_current_request", default=None
)


def _as_request(r):
    """Return a real Starlette Request, no matter what FastAPI handed us.

    On Python 3.14 + the FastAPI/Starlette pinned on Render, sync endpoints
    declared as ``def route(... r: Request, ...)`` sometimes receive:
      • the real Request (happy path)
      • the raw ASGI scope dict (older variant of the same bug)
      • an empty dict ``{}`` (FastAPI treats Request as a body field when
        it fails to recognise the annotation, then injects an empty body)

    The empty-dict case is unrecoverable from ``r`` alone — there's no
    scope to wrap. So we fall back to a contextvar that the request
    middleware sets at the very start of every HTTP request, which holds
    the canonical Request for the current task.
    """
    # Fast path — already a Starlette Request (or compatible duck type).
    if hasattr(r, "headers") and hasattr(r, "cookies"):
        return r
    # ASGI scope dict — wrap it.
    if isinstance(r, dict) and r.get("type") in ("http", "websocket"):
        from starlette.requests import Request as _Req
        return _Req(r)
    # Garbage (empty dict / None / wrong type) — pull from contextvar.
    req = current_request.get()
    if req is not None:
        return req
    # Truly nothing — raise a clear error instead of hiding it.
    raise RuntimeError(
        f"_as_request: could not resolve a Request "
        f"(got {type(r).__name__}={r!r}, contextvar empty)"
    )


def get_user(r):
    r = _as_request(r)
    # Cache on request.state to avoid multiple DB lookups per request.
    state = getattr(r, "state", None)
    if state is not None and hasattr(state, "_vibechecx_user"):
        return state._vibechecx_user
    user = get_user_from_session(r.cookies.get("vibechecx_session"))
    if state is not None:
        try:
            state._vibechecx_user = user
        except AttributeError:
            pass
    return user


def require_login(r):
    """Return a redirect response if not authenticated, else None.

    For HTMX partial requests returns an HX-Redirect header instead of a
    302 so the login page replaces the entire browser window, not just the
    HTMX swap target.
    """
    r = _as_request(r)
    if not get_user(r):
        if r.headers.get("hx-request"):
            resp = Response(status_code=204)
            resp.headers["HX-Redirect"] = "/login"
            return resp
        return RedirectResponse("/login", status_code=302)
    return None


def get_active_profile(r):
    r = _as_request(r)
    pid = r.cookies.get("vibechecx_profile")
    if not pid:
        return None
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return None
    user = get_user(r)
    if not user:
        return None
    rows = q(
        "SELECT p.*, c.name as cname FROM profiles p "
        "LEFT JOIN cohorts c ON c.id=p.cohort_id "
        "WHERE p.id=%s AND p.user_id=%s",
        (pid, user["id"]),
    )
    return rows[0] if rows else None


class NoActiveProfile(Exception):
    """Raised when a route needs an active profile but none is set/resolvable."""


def profile_account_ids(prof):
    """Account-IDs in the active profile's scope. Raises NoActiveProfile if empty.

    For single profiles: the one account.
    For cohort profiles: every account_id in cohort_members.
    """
    if not prof or not prof.get("id"):
        raise NoActiveProfile("no profile set")
    if prof.get("cohort_id"):
        rows = q(
            "SELECT account_id FROM cohort_members WHERE cohort_id=%s",
            (prof["cohort_id"],),
        )
        ids = [r["account_id"] for r in rows]
        if not ids:
            raise NoActiveProfile("cohort has no members")
        return ids
    if prof.get("target_handle"):
        row = q("SELECT id FROM accounts WHERE username=%s", (prof["target_handle"],))
        if row:
            return [row[0]["id"]]
    raise NoActiveProfile("profile has no resolvable accounts")


def profile_display_handle(prof):
    """Short handle/title for the active profile (used in headers)."""
    if not prof:
        return ""
    if prof.get("cohort_id"):
        n = q("SELECT count(*)::int AS c FROM cohort_members WHERE cohort_id=%s",
              (prof["cohort_id"],))[0]["c"]
        return f"Cohort: {prof.get('name') or prof.get('cname') or 'Untitled'} · {n} members"
    if prof.get("target_handle"):
        return f"@{prof['target_handle']}"
    return prof.get("name", "")


def active_profile_name(r) -> str:
    """Name of the active profile for the header chip, or empty string."""
    prof = get_active_profile(r)
    return prof.get("name", "") if prof else ""


def _require_profile(r):
    """Return (user, prof) or a Response if the request needs to bounce."""
    redir = require_login(r)
    if redir:
        return None, redir
    user = get_user(r)
    prof = get_active_profile(r)
    if not prof or not prof.get("id"):
        return user, RedirectResponse("/profiles", status_code=302)
    return user, prof
