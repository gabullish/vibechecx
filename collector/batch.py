#!/usr/bin/env python3
"""VibeChecx Batch Scraper — parallel workers backed by the CookiePool.

Each worker holds an exclusive CookieHandle from the pool. When the worker's
cookie hits 429, the pool's cooldown ladder applies and the worker waits for
its handle to recover (or grabs a different handle if the pool has spare
capacity). Workers can't step on each other because acquire() is exclusive.

Status is reported via the shared scrape_sessions row (VIBECHECX_SCRAPE_SESSION_ID).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web",
))

from vibechecx_config import DB_CONFIG, COOKIE_DIR, CUTOFF_DAYS_DEFAULT  # noqa: E402

from lib import session as _session  # noqa: E402
from lib import browser as _browser  # noqa: E402
from lib import storage as _storage  # noqa: E402
from lib.cookies import default_pool, CookieHandle, NoCookiesAvailable  # noqa: E402
from lib.parser import extract_tweets_from_graphql  # noqa: E402

logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s batch: %(message)s")
logger = logging.getLogger("vibechecx.batch")

CUTOFF_DAYS = CUTOFF_DAYS_DEFAULT


class RateLimited(Exception):
    """Worker hit a 429 — main loop re-queues the account."""


# ── DB helpers (thin wrappers that own their own connection) ───────────


def ensure_account_with_profile(t: dict) -> int | None:
    """Insert/update account with all profile fields. Returns id or None."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            aid = _storage.ensure_account(cur, t["author_username"])
            if aid:
                _storage.update_account_profile(cur, aid, tweet=t)
        conn.commit()
        return aid
    except Exception:
        conn.rollback()
        logger.warning("ensure_account failed for @%s", t.get("author_username"),
                       exc_info=True)
        return None
    finally:
        conn.close()


def upsert_tweet_record(t: dict) -> str | None:
    """One transaction per tweet."""
    aid = ensure_account_with_profile(t)
    if not aid:
        return None
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            tid = _storage.upsert_tweet(cur, t, author_account_id=aid,
                                        scrape_source="batch")
        conn.commit()
        return tid
    except Exception:
        conn.rollback()
        logger.warning("upsert_tweet failed for %s", t.get("tweet_id"), exc_info=True)
        return None
    finally:
        conn.close()


def log_observation(tweet_id: str, observer_username: str) -> None:
    """Record that observer saw this tweet (cohort visibility)."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            aid = _storage.ensure_account(cur, observer_username)
            if aid:
                _storage.log_observation(cur, tweet_id, aid, context="batch")
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("log_observation failed for %s", tweet_id, exc_info=True)


def insert_media_records(tweet_id: str, media: list) -> None:
    if not media:
        return
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            _storage.insert_media(cur, tweet_id, media)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("media insert failed for %s", tweet_id, exc_info=True)


def snapshot_account(username: str) -> None:
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT id FROM accounts WHERE username=%s", (username,))
        row = cur.fetchone()
        if row:
            aid = row[0]
            _storage.stamp_account_updated(cur, aid)
            _storage.record_account_snapshot(cur, aid)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("snapshot failed for @%s", username, exc_info=True)


# ── Worker ──────────────────────────────────────────────────────────────


class Worker:
    """One Playwright browser holding an exclusive cookie from the pool."""

    def __init__(self, wid: int, handle: CookieHandle):
        self.wid = wid
        self.handle = handle
        self.tweets_collected = 0
        self.accounts_done = 0
        self.fatal = False
        self._pw = None
        self._browser = None
        self._ctx = None

    async def launch(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._ctx = await _browser.open_context(
            self._browser, cookie_path=self.handle.path,
        )

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def scrape(self, username: str) -> int:
        if self.fatal:
            return 0
        # Respect any pool-level cooldown on our cookie
        if self.handle.cooldown_remaining > 0:
            await asyncio.sleep(self.handle.cooldown_remaining)
        page = await self._ctx.new_page()
        await _browser.apply_stealth(page)
        tweets: list[dict] = []
        try:
            async def capture(response):
                if "x.com/i/api/graphql/" not in response.url:
                    return
                try:
                    body = await response.json()
                except Exception:
                    return
                new = extract_tweets_from_graphql(body)
                for t in new:
                    if (t and t.get("tweet_id")
                            and not any(x.get("tweet_id") == t["tweet_id"] for x in tweets)):
                        tweets.append(t)

            page.on("response", capture)
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(os.environ.get("VIBECHECX_CUTOFF_DAYS", str(CUTOFF_DAYS))))

            async def scroll_url(url, max_scrolls=40):
                try:
                    await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    logger.warning("[W%d] nav failed: %s", self.wid, url, exc_info=True)
                    return
                await page.wait_for_timeout(_browser.human_load_delay_ms())
                no_new_streak = 0
                for s in range(max_scrolls):
                    prev = len(tweets)
                    await page.evaluate(
                        f"window.scrollTo(0, document.body.scrollHeight * "
                        f"{_browser.human_scroll_distance_pct()})"
                    )
                    await page.wait_for_timeout(_browser.human_scroll_delay_ms())
                    new_this_scroll = len(tweets) - prev
                    if new_this_scroll > 0:
                        no_new_streak = 0
                        all_old = True
                        for t in tweets[-new_this_scroll:]:
                            try:
                                td = datetime.strptime(
                                    t.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y"
                                )
                                if td >= cutoff:
                                    all_old = False
                                    break
                            except (ValueError, TypeError):
                                all_old = False
                                break
                        if all_old:
                            logger.info("[W%d] @%s %s: %d-day boundary at scroll %d",
                                        self.wid, username, url.split("/")[-1],
                                        CUTOFF_DAYS, s + 1)
                            break
                    else:
                        no_new_streak += 1
                        if no_new_streak >= 4:
                            break

            # Posts then with_replies (replies tab needs more depth)
            await scroll_url(f"https://x.com/{username}", max_scrolls=40)
            await scroll_url(f"https://x.com/{username}/with_replies", max_scrolls=60)
            await page.close()

            imported_target = 0
            imported_context = 0
            for t in tweets:
                tid = upsert_tweet_record(t)
                if tid:
                    is_target = (t.get("author_username") or "").lower() == username.lower()
                    if is_target:
                        imported_target += 1
                    else:
                        imported_context += 1
                    log_observation(tid, username)
                    if t.get("media"):
                        insert_media_records(tid, t["media"])

            self.tweets_collected += imported_target
            self.accounts_done += 1
            snapshot_account(username)
            default_pool().report_success(self.handle)

            logger.info("[W%d] @%s: %d target tweets + %d context",
                        self.wid, username, imported_target, imported_context)
            return imported_target

        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower() or "too many" in err.lower():
                default_pool().report_429(self.handle)
                logger.warning("[W%d] @%s: rate limited", self.wid, username)
                try:
                    await page.close()
                except Exception:
                    pass
                raise RateLimited()
            if "401" in err or "auth" in err.lower() or "login" in err.lower():
                default_pool().report_auth_failure(self.handle)
                self.fatal = True
                logger.error("[W%d] @%s: auth failure", self.wid, username)
            else:
                logger.warning("[W%d] @%s: %s", self.wid, username, err[:120])
            try:
                await page.close()
            except Exception:
                pass
            return 0


# ── batch entrypoint ────────────────────────────────────────────────────


async def batch_scrape(accounts: list[str], cohort_name: str = "batch"):
    total_accounts = len(accounts)
    _session.heartbeat(
        status="running", phase="batch_scraping",
        progress_total=total_accounts, progress_current=0,
        tweets_collected=0, target_handle=cohort_name,
    )

    pool = default_pool()
    queue: asyncio.Queue = asyncio.Queue()
    for a in accounts:
        await queue.put(a)
    logger.info("Scraping %d accounts with up to %d workers",
                len(accounts), pool.active_count)

    # Spin up one worker per usable cookie, but don't exceed (#accounts).
    workers: list[Worker] = []
    n_workers = min(pool.active_count, max(1, total_accounts))
    for i in range(n_workers):
        try:
            handle = await pool.acquire(exclusive=True, timeout=5.0)
        except NoCookiesAvailable:
            break
        w = Worker(i, handle)
        await w.launch()
        workers.append(w)
    if not workers:
        raise RuntimeError("No usable cookie files / pool empty")

    total_tweets = 0
    total_done = 0

    async def loop(w: Worker):
        nonlocal total_tweets, total_done
        while True:
            if queue.empty():
                break
            account = await queue.get()
            try:
                n = await w.scrape(account)
                total_tweets += n
                total_done += 1
                _session.heartbeat(
                    accounts_done=total_done,
                    progress_current=total_done,
                    tweets_collected=total_tweets,
                    target_handle=account,
                )
            except RateLimited:
                await queue.put(account)
            if w.fatal:
                break
        await w.close()
        pool.release(w.handle)

    await asyncio.gather(*[loop(w) for w in workers])

    logger.info("Done: %d tweets from %d accounts", total_tweets, total_done)
    for w in workers:
        logger.info("  W%d (%s): %d accts, %d tweets%s",
                    w.wid, w.handle.name, w.accounts_done, w.tweets_collected,
                    " FATAL" if w.fatal else "")
    return total_tweets, total_done


def _load_accounts(arg):
    """Resolve a CLI arg to a list of usernames. arg may be a cohort_id (int)
    or a list of @handles from sys.argv[1:]."""
    if isinstance(arg, list):
        return [a.lstrip("@") for a in arg if a], "batch"
    cid = int(arg)
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute(
        "SELECT a.username FROM cohort_members cm "
        "JOIN accounts a ON a.id = cm.account_id WHERE cm.cohort_id=%s",
        (cid,),
    )
    accounts = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT name FROM cohorts WHERE id=%s", (cid,))
    n = cur.fetchone()
    conn.close()
    return accounts, (n[0] if n else f"cohort#{cid}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 batch.py <cohort_id|username ...>")
        sys.exit(1)
    try:
        if sys.argv[1].isdigit():
            accounts, label = _load_accounts(sys.argv[1])
            logger.info("Loaded %d accounts from %s", len(accounts), label)
        else:
            accounts, label = _load_accounts(sys.argv[1:])
        if not accounts:
            _session.finish("failed", error="No accounts to scrape")
            sys.exit(1)
        total_tweets, _ = asyncio.run(batch_scrape(accounts, cohort_name=label))
        _session.finish("completed", tweets_collected=total_tweets)
    except Exception as exc:
        logger.exception("batch failed")
        _session.finish("failed",
                        error=f"{exc}\n{traceback.format_exc()}"[:2000])
        sys.exit(1)
