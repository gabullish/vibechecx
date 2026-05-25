"""web/metric_refresh_worker.py — keep tweet metrics fresh via X GraphQL.

Background daemon thread that polls tweets eligible for a metric refresh,
based on tweet age — fresher tweets refresh more often. Uses the existing
XApiClient (TweetResultByRestId GraphQL, cookies-based) so we get the same
data quality as a full Playwright scrape but ~50× cheaper per tweet.

Scope: tweets <= 30 days old whose author appears in any cohort or any
tracked profile. Older tweets are frozen.

Coordination: yields when a full coordinator scrape is currently running so
we don't contend for cookies / rate-limits.

Started from web/app.py at FastAPI startup. Disabled on Render (the Boto
machine is the one with cookies + the network path to X).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import RealDictCursor

from vibechecx_config import DB_CONFIG, COOKIE_DIR  # noqa: E402

# collector/ is a sibling of web/ — add it to sys.path so we can reuse
# the existing XApiClient instead of duplicating the GraphQL plumbing.
_COLLECTOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collector"
)
if _COLLECTOR_DIR not in sys.path:
    sys.path.insert(0, _COLLECTOR_DIR)
from lib.xapi import XApiClient  # noqa: E402

logger = logging.getLogger("vibechecx.metric_refresh")


# How many tweets to refresh per loop. At concurrency=5 and ~200ms / call
# this works out to ~3s per batch + jitter.
_BATCH_SIZE = 25
_CONCURRENCY = 5

# Loop cadence. Short sleep when we just did real work, long sleep when the
# queue is empty so we're not hammering the DB to find nothing.
_BUSY_SLEEP = 5
_IDLE_SLEEP = 30


# Adaptive cadence. Younger tweets churn fastest in the first hour so we
# revisit them often; ancient tweets only get a daily nudge.
_DUE_QUERY = """
SELECT t.tweet_id
  FROM tweets t
 WHERE t.created_at > NOW() - INTERVAL '30 days'
   AND t.author_account_id IN (
       SELECT account_id FROM cohort_members
        UNION
       SELECT a.id FROM accounts a
         JOIN profiles p ON LOWER(p.target_handle) = LOWER(a.username)
   )
   AND (
       (t.created_at > NOW() - INTERVAL '30 min'
            AND (t.metrics_refreshed_at IS NULL OR t.metrics_refreshed_at < NOW() - INTERVAL '2 min'))
    OR (t.created_at > NOW() - INTERVAL '6 hours'
            AND (t.metrics_refreshed_at IS NULL OR t.metrics_refreshed_at < NOW() - INTERVAL '10 min'))
    OR (t.created_at > NOW() - INTERVAL '24 hours'
            AND (t.metrics_refreshed_at IS NULL OR t.metrics_refreshed_at < NOW() - INTERVAL '1 hour'))
    OR (t.created_at > NOW() - INTERVAL '7 days'
            AND (t.metrics_refreshed_at IS NULL OR t.metrics_refreshed_at < NOW() - INTERVAL '6 hours'))
    OR (t.metrics_refreshed_at IS NULL OR t.metrics_refreshed_at < NOW() - INTERVAL '24 hours')
   )
 ORDER BY t.metrics_refreshed_at NULLS FIRST
 LIMIT %s
"""


def _conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def _scrape_running() -> bool:
    """Soft coordination — skip this tick if a coordinator is currently
    burning through tweets. Cookies are shared; we'd rather defer than
    cause 429 cascades."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM scrape_queue WHERE status='running' LIMIT 1")
            return cur.fetchone() is not None
    except Exception:
        logger.exception("scrape_running check failed")
        return False


def _pick_due(limit: int) -> list[str]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(_DUE_QUERY, (limit,))
        return [r["tweet_id"] for r in cur.fetchall()]


def _apply(updates: dict[str, dict], queried_ids: list[str]) -> None:
    """Write metric updates and stamp metrics_refreshed_at on *every* queried
    tweet — even ones the API didn't return (deleted, restricted, etc.) so we
    don't loop on them every tick."""
    if not queried_ids:
        return
    with _conn() as conn, conn.cursor() as cur:
        for tid, m in updates.items():
            # views can be hidden by the author / unavailable for old tweets —
            # don't let a transient 0 wipe a real number we already had.
            cur.execute(
                """
                UPDATE tweets SET
                    likes     = %s,
                    retweets  = %s,
                    replies   = %s,
                    quotes    = %s,
                    bookmarks = %s,
                    views     = GREATEST(views, %s),
                    metrics_refreshed_at = NOW()
                  WHERE tweet_id = %s
                """,
                (m["likes"], m["retweets"], m["replies"],
                 m["quotes"], m["bookmarks"], m["views"], tid),
            )
        missing = [t for t in queried_ids if t not in updates]
        if missing:
            cur.execute(
                "UPDATE tweets SET metrics_refreshed_at = NOW() WHERE tweet_id = ANY(%s)",
                (missing,),
            )
        conn.commit()


async def _refresh_batch(ids: list[str]) -> dict[str, dict]:
    async with XApiClient.from_cookie_dir(COOKIE_DIR) as client:
        return await client.get_tweet_metrics(ids, concurrency=_CONCURRENCY)


def _tick() -> int:
    """Run one batch. Returns count of tweets refreshed (0 = nothing to do
    or coordinator is busy)."""
    if _scrape_running():
        return 0
    ids = _pick_due(_BATCH_SIZE)
    if not ids:
        return 0
    t0 = time.time()
    updates: dict[str, dict] = {}
    try:
        updates = asyncio.run(_refresh_batch(ids))
    except FileNotFoundError:
        logger.warning("no cookie files available; metric refresh disabled this tick")
        return 0
    except Exception:
        # Don't let one bad batch take down the loop — stamp the IDs so we
        # move on and try fresh ones next tick.
        logger.exception("refresh batch failed; stamping IDs to skip")
    _apply(updates, ids)
    logger.info(
        "metric_refresh: requested=%d returned=%d elapsed=%.1fs",
        len(ids), len(updates), time.time() - t0,
    )
    return len(updates)


def start() -> None:
    """Launch the refresh loop as a daemon thread. Call once at startup."""
    def _loop():
        logger.info("metric_refresh worker started — batch=%d concurrency=%d",
                    _BATCH_SIZE, _CONCURRENCY)
        while True:
            try:
                n = _tick()
                time.sleep(_BUSY_SLEEP if n else _IDLE_SLEEP)
            except Exception:
                logger.exception("metric_refresh tick crashed; sleeping")
                time.sleep(_IDLE_SLEEP)

    t = threading.Thread(target=_loop, name="metric-refresh-worker", daemon=True)
    t.start()
    logger.info("metric_refresh worker thread launched")
