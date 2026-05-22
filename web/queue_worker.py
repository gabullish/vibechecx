"""web/queue_worker.py — background thread that serialises scrape runs.

One coordinator at a time. Picks the oldest 'pending' scrape_queue row,
launches the coordinator subprocess, waits for it to finish, then repeats.
Runs as a daemon thread started from web/app.py at FastAPI startup.
"""
import os
import sys
import time
import logging
import subprocess
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG, COLLECTOR_DIR, SCRAPER_HEADFUL

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger("vibechecx.queue")

_lock = threading.Lock()          # prevents two ticks overlapping
_current_proc: subprocess.Popen | None = None
_current_queue_id: int | None = None


def _conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def queue_depth() -> dict:
    """Return {running, waiting} counts for the header widget."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) n FROM scrape_queue "
            "WHERE status IN ('pending','running') GROUP BY status"
        )
        rows = {r["status"]: r["n"] for r in cur.fetchall()}
    return {"running": rows.get("running", 0), "waiting": rows.get("pending", 0)}


def user_queue_row(user_id: int):
    """Most recent active (pending/running) queue row for this user, or None."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.*, p.target_handle, p.cohort_id
              FROM scrape_queue q
              JOIN profiles p ON p.id = q.profile_id
             WHERE q.user_id = %s
               AND q.status IN ('pending', 'running')
             ORDER BY q.created_at DESC
             LIMIT 1
            """,
            (user_id,),
        )
        return cur.fetchone()


def position_in_queue(queue_id: int) -> int:
    """How many pending rows were created before this one (1-based position)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) n FROM scrape_queue
             WHERE status = 'pending'
               AND position <= (SELECT position FROM scrape_queue WHERE id = %s)
            """,
            (queue_id,),
        )
        row = cur.fetchone()
        return row["n"] if row else 1


def enqueue(user_id: int, profile_id: int, days: int) -> dict:
    """Insert a pending queue row. Deduplicates: if user already has an
    active row for this profile, returns the existing row unchanged."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM scrape_queue
             WHERE user_id = %s AND profile_id = %s
               AND status IN ('pending', 'running')
             ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, profile_id),
        )
        existing = cur.fetchone()
        if existing:
            return dict(existing)
        cur.execute(
            """
            INSERT INTO scrape_queue (user_id, profile_id, days)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (user_id, profile_id, days),
        )
        return dict(cur.fetchone())


def cancel_queue_row(queue_id: int, user_id: int) -> bool:
    """Cancel a pending queue row. Returns True if cancelled."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scrape_queue SET status='cancelled', ended_at=NOW()
             WHERE id=%s AND user_id=%s AND status='pending'
            """,
            (queue_id, user_id),
        )
        return cur.rowcount > 0


def _mark_running(queue_id: int, session_id: int):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE scrape_queue SET status='running', started_at=NOW(), session_id=%s WHERE id=%s",
            (session_id, queue_id),
        )


def _mark_done(queue_id: int, status: str, error: str | None = None):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE scrape_queue SET status=%s, ended_at=NOW(), error=%s WHERE id=%s",
            (status, error, queue_id),
        )


def _get_session_status(session_id: int) -> str | None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM scrape_sessions WHERE id=%s", (session_id,))
        row = cur.fetchone()
        return row["status"] if row else None


def _reconcile_orphaned():
    """Mark 'running' queue rows whose scrape_session is already terminal.

    Handles the case where the Boto worker was restarted mid-scrape: the
    coordinator may have finished (and updated scrape_sessions) but _current_proc
    was lost, so _tick() never called _mark_done().  We detect this by joining
    against the session row — if the session is done, so is the queue job.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.id, s.status AS sess_status
              FROM scrape_queue q
              JOIN scrape_sessions s ON s.id = q.session_id
             WHERE q.status = 'running'
               AND s.status IN ('completed', 'failed', 'cancelled')
            """
        )
        rows = cur.fetchall()
    for row in rows:
        final = "completed" if row["sess_status"] == "completed" else "failed"
        _mark_done(row["id"], final)
        logger.info("Reconciled orphaned queue row %s → %s", row["id"], final)


def _tick():
    """Single queue tick — called every 5s by the worker loop."""
    global _current_proc, _current_queue_id

    with _lock:
        # Reconcile any queue rows orphaned by a prior worker restart.
        _reconcile_orphaned()

        # Check if current job finished.
        if _current_proc is not None:
            ret = _current_proc.poll()
            if ret is not None:
                # Process exited — determine outcome from scrape_sessions
                qid = _current_queue_id
                _current_proc = None
                _current_queue_id = None

                with _conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT session_id FROM scrape_queue WHERE id=%s", (qid,)
                    )
                    row = cur.fetchone()
                sess_status = None
                if row and row["session_id"]:
                    sess_status = _get_session_status(row["session_id"])

                final = "completed" if sess_status == "completed" else "failed"
                _mark_done(qid, final)
                logger.info(f"Queue item {qid} finished → {final}")
            else:
                # Still running — nothing to do this tick.
                return

        # Nothing running — pick next pending.
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT q.*, p.target_handle, p.cohort_id
                  FROM scrape_queue q
                  JOIN profiles p ON p.id = q.profile_id
                 WHERE q.status = 'pending'
                 ORDER BY q.position
                 LIMIT 1
                """
            )
            row = cur.fetchone()

        if not row:
            return  # queue empty

        qid = row["id"]
        user_id = row["user_id"]
        profile_id = row["profile_id"]
        days = row["days"]

        # Start a scrape_session so we have a session_id before launching.
        from vibechecx_scrape_status import start_session
        session_id = start_session(
            user_id=user_id,
            session_type="full_scrape",
            cohort_id=row.get("cohort_id"),
            target_handle=row.get("target_handle"),
        )
        _mark_running(qid, session_id)

        coordinator = os.path.join(COLLECTOR_DIR, "coordinator.py")
        log_path = f"/tmp/vibechecx_coordinator_{profile_id}_{int(time.time())}.log"
        log_fh = open(log_path, "w")

        env = os.environ.copy()
        env["VIBECHECX_SCRAPE_SESSION_ID"] = str(session_id)
        env["VIBECHECX_SCRAPER_HEADFUL"] = "true" if SCRAPER_HEADFUL else "false"

        proc = subprocess.Popen(
            [
                "python3", coordinator,
                "--profile", str(profile_id),
                "--user", str(user_id),
                "--days", str(days),
            ],
            stdout=log_fh,
            stderr=log_fh,
            env=env,
        )
        _current_proc = proc
        _current_queue_id = qid
        logger.info(
            f"Queue item {qid}: launched coordinator pid={proc.pid} "
            f"profile={profile_id} user={user_id} days={days} "
            f"session={session_id} log={log_path}"
        )


def start():
    """Launch the queue worker as a daemon thread. Call once at app startup."""
    def _loop():
        logger.info("Queue worker started")
        while True:
            try:
                _tick()
            except Exception:
                logger.exception("Queue worker tick error")
            time.sleep(5)

    t = threading.Thread(target=_loop, name="scrape-queue-worker", daemon=True)
    t.start()
    logger.info("Queue worker thread launched")
