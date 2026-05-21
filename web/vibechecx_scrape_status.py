"""DB-backed scrape status — replaces /tmp/vibechecx_scrape_status.json.

The collector writes its own row into scrape_sessions and updates it as it
progresses. The web UI polls current_for_user() to render real status. Stale
"running" rows (no heartbeat for >5min, no pid alive) are reconciled to
'failed' so the UI never lies about a crashed scrape.
"""
import os
import signal
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG

logger = logging.getLogger(__name__)

LIVE_STATUSES = ("starting", "running", "scrolling", "batch_scraping", "patrol")
STALE_HEARTBEAT_SECONDS = 5 * 60


def _conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def start_session(user_id, session_type, target_handle=None, cohort_id=None,
                  target_account_id=None, progress_total=0):
    """Insert a new scrape_sessions row in 'starting' state. Returns session_id."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scrape_sessions
                (user_id, session_type, target_handle, cohort_id, target_account_id,
                 status, phase, progress_total, pid, last_heartbeat_at)
            VALUES (%s, %s, %s, %s, %s, 'starting', 'starting', %s, %s, NOW())
            RETURNING id
            """,
            (user_id, session_type, target_handle, cohort_id, target_account_id,
             progress_total, os.getpid()),
        )
        return cur.fetchone()["id"]


def heartbeat(session_id, *, phase=None, status=None, progress_current=None,
              progress_total=None, accounts_done=None, tweets_collected=None,
              target_handle=None):
    """Update progress fields. Only non-None fields are written."""
    sets, vals = ["last_heartbeat_at = NOW()"], []
    if status is not None:
        sets.append("status = %s"); vals.append(status)
    if phase is not None:
        sets.append("phase = %s"); vals.append(phase)
    if progress_current is not None:
        sets.append("progress_current = %s"); vals.append(progress_current)
    if progress_total is not None:
        sets.append("progress_total = %s"); vals.append(progress_total)
    if accounts_done is not None:
        sets.append("accounts_done = %s"); vals.append(accounts_done)
    if tweets_collected is not None:
        sets.append("tweets_collected = %s"); vals.append(tweets_collected)
    if target_handle is not None:
        sets.append("target_handle = %s"); vals.append(target_handle)
    vals.append(session_id)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE scrape_sessions SET {', '.join(sets)} WHERE id = %s",
            tuple(vals),
        )


def finish_session(session_id, *, status="completed", tweets_collected=None, error=None):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scrape_sessions
               SET status = %s,
                   ended_at = NOW(),
                   tweets_collected = COALESCE(%s, tweets_collected),
                   error_log = %s
             WHERE id = %s
            """,
            (status, tweets_collected, error, session_id),
        )


def reconcile_stale():
    """Mark long-stale 'running' rows as failed. Safe to call before reads."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, pid
              FROM scrape_sessions
             WHERE status IN ({','.join(['%s'] * len(LIVE_STATUSES))})
               AND (last_heartbeat_at IS NULL
                    OR last_heartbeat_at < NOW() - INTERVAL '{STALE_HEARTBEAT_SECONDS} seconds')
            """,
            LIVE_STATUSES,
        )
        rows = cur.fetchall()
        for r in rows:
            if not _pid_alive(r["pid"]):
                cur.execute(
                    "UPDATE scrape_sessions SET status='failed', ended_at=NOW(), "
                    "error_log = COALESCE(error_log, '') || 'reconciled: heartbeat timeout' "
                    "WHERE id=%s",
                    (r["id"],),
                )


def current_for_user(user_id):
    """Most-recent scrape_sessions row for this user, or None.

    Terminal sessions (completed/failed/cancelled) older than 2 hours are
    not returned — the banner would be stale noise on every page load.
    """
    reconcile_stale()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM scrape_sessions
             WHERE user_id = %s
               AND (
                 status NOT IN ('completed', 'failed', 'cancelled')
                 OR started_at > NOW() - INTERVAL '5 minutes'
               )
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (user_id,),
        )
        return cur.fetchone()


def history_for_user(user_id, limit=20):
    reconcile_stale()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.*, a.username as target_username
              FROM scrape_sessions s
              LEFT JOIN accounts a ON a.id = s.target_account_id
             WHERE s.user_id = %s
             ORDER BY s.started_at DESC
             LIMIT %s
            """,
            (user_id, limit),
        )
        return cur.fetchall()


def is_live(row):
    return bool(row) and row.get("status") in LIVE_STATUSES
