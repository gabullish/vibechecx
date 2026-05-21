#!/usr/bin/env python3
"""VibeChecx Patrol — refresh likes/views/replies on existing tweets.

Default mode (safe, human-looking): Playwright navigation to each tweet's
permalink. Two browser contexts run in parallel, each with its own cookie
from the pool, splitting the target list in half. Roughly 1s/tweet wall-clock
with heavy-resource blocking + parallelism.

--fast mode (faster, more flag-prone): bypasses Playwright entirely and
fires direct httpx calls to TweetResultByRestId via lib.xapi.XApiClient.
~0.2s/tweet but exactly the request pattern X's abuse detection looks for.
Use sparingly when you must refresh a large backlog.

Scope: --account / --cohort / --profile (see collector.scope).
Heartbeats: scrape_sessions session_type='metric_patrol'.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import traceback

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web",
))

from vibechecx_config import DB_CONFIG as DB, COOKIE_DIR  # noqa: E402

from lib import session as _session  # noqa: E402
from lib import browser as _browser  # noqa: E402
from lib import storage as _storage  # noqa: E402
from lib.cookies import default_pool, NoCookiesAvailable  # noqa: E402
from lib.parser import parse_tweet_result  # noqa: E402
from lib.xapi import XApiClient  # noqa: E402
from scope import add_scope_args, resolve as resolve_scope, select_tweets_in_scope  # noqa: E402

logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s patrol: %(message)s")
logger = logging.getLogger("vibechecx.patrol")


# ── DB writes (batched) ────────────────────────────────────────────────


def update_metrics_batch(metrics: dict[str, dict]) -> int:
    """Bulk UPDATE tweet metrics. One transaction for the whole batch."""
    if not metrics:
        return 0
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            updated = _storage.update_tweet_metrics_batch(cur, metrics)
        conn.commit()
        return updated
    finally:
        conn.close()


# ── Playwright (default) implementation ────────────────────────────────


async def _patrol_one_context(
    handle,
    targets: list[dict],
    *,
    metrics_out: dict[str, dict],
    metrics_lock: asyncio.Lock,
    progress: dict,
    session_id: int | None,
    total: int,
):
    """One browser context navigates through its slice of `targets`."""
    from playwright.async_api import async_playwright

    pool = default_pool()
    target_ids = {t["tweet_id"] for t in targets}
    local_seen: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await _browser.open_context(browser, cookie_path=handle.path)
        page = await ctx.new_page()
        await _browser.apply_stealth(page)
        await _browser.block_heavy_resources(page)

        async def on_response(response):
            if "TweetResultByRestId" not in response.url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            try:
                tr = body["data"]["tweetResult"]["result"]
            except (KeyError, TypeError):
                return
            leg = tr.get("legacy", {})
            tid = tr.get("rest_id", leg.get("id_str", ""))
            if not tid or tid not in target_ids or tid in local_seen:
                return
            local_seen.add(tid)
            views = 0
            vo = tr.get("views", {})
            if isinstance(vo, dict):
                views = int(vo.get("count") or 0)
            metric = {
                "likes":    int(leg.get("favorite_count") or 0),
                "retweets": int(leg.get("retweet_count") or 0),
                "replies":  int(leg.get("reply_count") or 0),
                "views":    views,
            }
            async with metrics_lock:
                metrics_out[tid] = metric

        page.on("response", on_response)
        try:
            await page.goto("https://x.com/",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(_browser.human_load_delay_ms())

            for i, t in enumerate(targets):
                url = f"https://x.com/{t['username']}/status/{t['tweet_id']}"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(_browser.human_scroll_delay_ms())
                except Exception:
                    logger.warning("[%s] patrol nav failed for %s",
                                   handle.name, url, exc_info=True)
                async with metrics_lock:
                    progress["done"] += 1
                    progress["captured"] = len(metrics_out)
                if session_id and (progress["done"] % 5 == 0 or progress["done"] == total):
                    _session.heartbeat(
                        progress_current=progress["done"],
                        progress_total=total,
                        tweets_collected=progress["captured"],
                    )
            pool.report_success(handle)
        except Exception as exc:
            err = str(exc).lower()
            if "429" in err or "rate" in err:
                pool.report_429(handle)
            elif "401" in err or "auth" in err:
                pool.report_auth_failure(handle)
            else:
                pool.release(handle)
            raise
        finally:
            await browser.close()


async def patrol_tweets(tweet_targets: list[dict],
                        session_id: int | None = None) -> list[str]:
    """Default: Playwright two-context patrol. Returns list of tweet_ids updated."""
    if not tweet_targets:
        return []

    total = len(tweet_targets)
    pool = default_pool()
    logger.info("patrol: refreshing %d tweets via Playwright (2 contexts)", total)

    # Try to grab 2 distinct cookies. If only 1 is available, run sequentially.
    handles = []
    try:
        handles.append(await pool.acquire(exclusive=True, timeout=5.0))
        try:
            handles.append(await pool.acquire(exclusive=True, timeout=5.0))
        except NoCookiesAvailable:
            logger.info("patrol: only 1 cookie available — running single context")
    except NoCookiesAvailable as e:
        logger.error("patrol: no cookies available: %s", e)
        return []

    # Split target list across handles
    n = len(handles)
    slices = [tweet_targets[i::n] for i in range(n)]

    metrics: dict[str, dict] = {}
    metrics_lock = asyncio.Lock()
    progress = {"done": 0, "captured": 0}

    try:
        await asyncio.gather(*[
            _patrol_one_context(h, slc,
                                metrics_out=metrics,
                                metrics_lock=metrics_lock,
                                progress=progress,
                                session_id=session_id,
                                total=total)
            for h, slc in zip(handles, slices)
        ])
    finally:
        for h in handles:
            pool.release(h)

    update_metrics_batch(metrics)

    updated_ids = list(metrics.keys())
    logger.info("patrol: done — %d/%d updated", len(updated_ids), total)
    for tid, m in list(metrics.items())[:3]:
        logger.info("  %s ❤%d 🔁%d 💬%d 👁%d", tid[:15],
                    m["likes"], m["retweets"], m["replies"], m["views"])
    return updated_ids


# ── --fast mode: direct httpx ──────────────────────────────────────────


async def patrol_tweets_fast(tweet_targets: list[dict],
                             session_id: int | None = None) -> list[str]:
    """Direct API patrol. ~10x faster, much more flag-prone — use sparingly."""
    if not tweet_targets:
        return []
    total = len(tweet_targets)
    logger.warning("patrol --fast: direct API for %d tweets — accepting "
                   "elevated rate-limit risk", total)

    tweet_ids = [t["tweet_id"] for t in tweet_targets]
    try:
        client = XApiClient.from_cookie_dir(COOKIE_DIR)
    except ValueError as e:
        logger.error("patrol --fast: no usable cookies — %s", e)
        return []

    batch_size = 50
    metrics_all: dict[str, dict] = {}
    async with client:
        for batch_start in range(0, total, batch_size):
            batch = tweet_ids[batch_start: batch_start + batch_size]
            metrics = await client.get_tweet_metrics(batch, concurrency=5)
            metrics_all.update(metrics)
            update_metrics_batch(metrics)

            done = min(batch_start + batch_size, total)
            logger.info("patrol --fast: %d/%d tweets refreshed", done, total)
            if session_id:
                _session.heartbeat(progress_current=done, progress_total=total,
                                   tweets_collected=len(metrics_all))

    logger.info("patrol --fast: done — %d/%d updated", len(metrics_all), total)
    return list(metrics_all.keys())


# ── scope entrypoint ───────────────────────────────────────────────────


def run_patrol_for_scope(scope: dict, *, limit: int = 50,
                         session_id: int | None = None,
                         fast: bool = False, do_patrol=None) -> dict:
    """Select tweets in scope, patrol them, return {patrolled, total}.

    fast=True opts into direct-API patrol. Defaults to safe Playwright mode.
    `do_patrol` is injectable for tests.
    """
    targets = select_tweets_in_scope(scope, days=14, limit=limit)
    total = len(targets)
    if session_id:
        _session.heartbeat(phase="metric_patrol", progress_total=total,
                           progress_current=0, target_handle=scope["label"])
    if not targets:
        return {"patrolled": 0, "total": 0}
    runner = do_patrol or (patrol_tweets_fast if fast else patrol_tweets)
    patrolled = asyncio.run(runner(targets, session_id=session_id))
    return {"patrolled": len(patrolled or []), "total": total}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Refresh metrics for a scope's tweets (Playwright by default)"
    )
    add_scope_args(parser)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--fast", action="store_true",
        help="Direct API mode — ~10x faster, more flag-prone. Use sparingly.",
    )
    args = parser.parse_args()

    try:
        scope = resolve_scope(args)
    except ValueError as e:
        logger.error("scope error: %s", e)
        sys.exit(1)

    existing_sid = _session.current_session_id()
    sid = existing_sid or _session.start_owned_session(
        user_id=scope.get("user_id"),
        session_type="metric_patrol",
        target_handle=scope["label"],
        cohort_id=scope.get("cohort_id"),
    )
    try:
        result = run_patrol_for_scope(scope, limit=args.limit,
                                      session_id=sid, fast=args.fast)
        logger.info("patrol done: %d/%d", result["patrolled"], result["total"])
        if sid and not existing_sid:
            _session.finish("completed", tweets_collected=result["patrolled"])
    except Exception as exc:
        logger.exception("patrol failed")
        if sid and not existing_sid:
            _session.finish("failed",
                            error=f"{exc}\n{traceback.format_exc()}"[:2000])
        sys.exit(1)
