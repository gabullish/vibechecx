"""web/core.py — DB + auth + profile helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import Request
from fastapi.responses import RedirectResponse

from vibechecx_config import DB_CONFIG  # noqa: E402
from vibechecx_auth import get_user_from_session  # noqa: E402

DB = DB_CONFIG  # back-compat alias


# ── Connection / query helpers ────────────────────────────────────────


def q(sql, params=None):
    """Execute a query, return list of dicts. One connection per call (we'll
    move to a pool when traffic justifies it)."""
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("SET timezone = 'UTC'")
            cur.execute(sql, params or ())
            if cur.description is None:
                conn.commit()
                return []
            rows = cur.fetchall()
        conn.commit()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user(r):
    return get_user_from_session(r.cookies.get("vibechecx_session"))


def require_login(r):
    """Return a RedirectResponse if not authenticated, else None."""
    if not get_user(r):
        return RedirectResponse("/login", status_code=302)
    return None


def get_active_profile(r):
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
