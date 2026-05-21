"""web/patrol_scheduler.py — nightly metric refresh for all tracked accounts.

Runs patrol.py once per day for every account that has tweets in the last 30
days, keeping likes/views/replies counts current without a manual trigger.
Accounts are processed sequentially (patrol opens 2 browser contexts already,
so no extra parallelism needed).
"""
from __future__ import annotations

import datetime
import logging
import os
import subprocess
import threading
import time

import psycopg2

logger = logging.getLogger("vibechecx.patrol_scheduler")


def _active_accounts() -> list[str]:
    from vibechecx_config import DB_CONFIG
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT a.username
                FROM accounts a
                JOIN tweets t ON t.author_account_id = a.id
                WHERE t.created_at >= NOW() - INTERVAL '30 days'
                ORDER BY a.username
            """)
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def run_nightly_patrol():
    from vibechecx_config import COLLECTOR_DIR, SCRAPER_HEADFUL
    patrol = os.path.join(COLLECTOR_DIR, "patrol.py")
    accounts = _active_accounts()
    logger.info("Nightly patrol: %d accounts", len(accounts))
    env = os.environ.copy()
    env["VIBECHECX_SCRAPER_HEADFUL"] = "true" if SCRAPER_HEADFUL else "false"
    for username in accounts:
        logger.info("Patrolling @%s", username)
        try:
            subprocess.run(
                ["python3", patrol, "--account", username, "--limit", "200"],
                timeout=600,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Patrol timeout for @%s", username)
        except Exception:
            logger.exception("Patrol error for @%s", username)
    logger.info("Nightly patrol complete")


def _scheduler_loop():
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Target: 03:00 UTC daily
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += datetime.timedelta(days=1)
        sleep_secs = (next_run - now).total_seconds()
        logger.info("Next nightly patrol in %.1fh", sleep_secs / 3600)
        time.sleep(sleep_secs)
        try:
            run_nightly_patrol()
        except Exception:
            logger.exception("Nightly patrol crashed")


def start_patrol_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="patrol-scheduler")
    t.start()
    logger.info("Patrol scheduler started — runs daily at 03:00 UTC")
