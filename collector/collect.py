#!/usr/bin/env python3
"""VibeChecx Collector — Playwright + GraphQL interception (single account).

Scrapes both the Posts and With-replies tabs for one username concurrently
across two browser contexts (each with its own cookie). The Posts/With-replies
parallel split is ~40% faster end-to-end than running them serially.

Shared infrastructure (cookies, parser, browser, storage, session) lives in
collector/lib/.  This file owns: the two-context scroll loop, per-tab early
exit logic, and the CLI entry point.
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

# Path setup so the shared `vibechecx_config` module is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
))

from vibechecx_config import DB_CONFIG, RAW_DIR as _RAW_DIR, CUTOFF_DAYS_DEFAULT  # noqa: E402

from lib import session as _session  # noqa: E402
from lib import browser as _browser  # noqa: E402
from lib import storage as _storage  # noqa: E402
from lib.cookies import default_pool, CookieHandle  # noqa: E402
from lib.parser import (  # noqa: E402  (re-exported for backwards compat)
    extract_tweets_from_graphql,
    extract_author,
    parse_tweet_result,
    extract_single_tweet,
)

logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s collector: %(message)s")
logger = logging.getLogger("vibechecx.collect")

CUTOFF_DAYS = CUTOFF_DAYS_DEFAULT
GRAPHQL_PATTERN = "x.com/i/api/graphql/"
RAW_DIR = _RAW_DIR


# ── status helpers (thin wrappers around lib.session for backwards compat) ──


def status_heartbeat(**kw):  # back-compat alias
    _session.heartbeat(**kw)


def status_finish(status, tweets_collected=None, error=None):  # back-compat alias
    _session.finish(status, tweets_collected=tweets_collected, error=error)


# ── cookie helpers ──────────────────────────────────────────────────────


def get_next_cookie() -> str:
    """Back-compat shim. Returns a cookie file path from the default pool.
    New code should use `default_pool().acquire()` directly.
    """
    pool = default_pool()
    # Non-async fallback for callers that haven't moved to async pool access.
    # Pick the freshest available, no waiting.
    candidates = [h for h in pool._handles if h.available]
    if not candidates:
        # Pool is fully cooled — return main.json as the historical fallback.
        return pool._handles[0].path
    handle = min(candidates, key=lambda h: h.last_used_at)
    handle.last_used_at = time.time()
    return handle.path


# ── raw JSON helpers ────────────────────────────────────────────────────


def save_raw_json(data, prefix="graphql"):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    with open(os.path.join(RAW_DIR, f"{prefix}_{ts}.json"), "w") as f:
        json.dump(data, f, indent=2)


# ── main entrypoint ─────────────────────────────────────────────────────


async def collect_profile(username, headful=False, limit=0, fresh=False,
                          cookie_file=None):
    """Scrape Posts + With-replies tabs for `username` across two parallel
    browser contexts.

    cookie_file: optional override for tab A's cookie. Tab B always pulls
    from the pool. Returns (all_tweets, graphql_responses).
    """
    from playwright.async_api import async_playwright

    pool = default_pool()

    # Tab A: caller-supplied cookie file, else pool. Tab B: pool, distinct
    # from A whenever possible.
    if cookie_file:
        handle_a = next((h for h in pool._handles if h.path == cookie_file), None)
        if handle_a is None:
            handle_a = await pool.acquire(exclusive=True)
        else:
            handle_a.in_use = True
            handle_a.last_used_at = time.time()
    else:
        handle_a = await pool.acquire(exclusive=True)
    try:
        handle_b = await pool.acquire(exclusive=True, timeout=10.0)
    except Exception:
        handle_b = handle_a  # only one cookie available — both tabs share it
    if handle_b is handle_a:
        logger.warning("collect: only one cookie available, both tabs share %s",
                       handle_a.name)

    all_tweets: list[dict] = []
    graphql_responses: list[dict] = []
    seen_ids: set[str] = set()
    dedup_lock = asyncio.Lock()
    # Per-tab tweet lists — accurate per-tab early-exit decisions when both
    # tabs run concurrently and share `all_tweets`.
    tab_a_tweets: list[dict] = []
    tab_b_tweets: list[dict] = []

    # Scope known-IDs preload to this account's recent tweets only.
    known_ids: set[str] = set()
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        known_ids = _storage.load_known_tweet_ids(cur, username, CUTOFF_DAYS + 7)
        conn.close()
        if known_ids:
            logger.info("Loaded %d known IDs for @%s", len(known_ids), username)
    except Exception:
        logger.warning("could not preload known tweet IDs", exc_info=True)

    _session.heartbeat(status="running", phase="navigating", target_handle=username)

    # Posts tab: 50 default scrolls. Replies tab: 100 (reply timelines
    # paginate ~50% fewer tweets per GraphQL call).
    max_scrolls_posts   = limit if limit > 0 else 50
    max_scrolls_replies = limit if limit > 0 else 100
    cutoff_days = int(os.environ.get("VIBECHECX_CUTOFF_DAYS", str(CUTOFF_DAYS)))
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=cutoff_days)

    async with async_playwright() as p:
        if headful:
            os.environ.setdefault("DISPLAY", ":0")
            os.environ.setdefault("XAUTHORITY", os.path.expanduser("~/.Xauthority"))
        browser = await p.chromium.launch(headless=not headful)

        ctx_a = await _browser.open_context(browser, cookie_path=handle_a.path)
        ctx_b = await _browser.open_context(browser, cookie_path=handle_b.path)
        page_a = await ctx_a.new_page()
        page_b = await ctx_b.new_page()
        await _browser.apply_stealth(page_a)
        await _browser.apply_stealth(page_b)

        def _make_handler(phase_label: str, tab_list: list[dict]):
            async def handle_response(response):
                if GRAPHQL_PATTERN not in response.url:
                    return
                try:
                    body = await response.json()
                except Exception:
                    return
                async with dedup_lock:
                    graphql_responses.append(body)
                    endpoint = response.url.split("/")[-1].split("?")[0]
                    logger.info("[%s] GraphQL %s status=%s size=%db",
                                phase_label, endpoint[:30], response.status,
                                len(json.dumps(body)))
                    save_raw_json(body, prefix=endpoint)
                    tweets = extract_tweets_from_graphql(body)
                    for t in tweets:
                        if not (t and t.get("tweet_id")):
                            continue
                        tab_list.append(t)
                        if t["tweet_id"] in seen_ids:
                            continue
                        seen_ids.add(t["tweet_id"])
                        all_tweets.append(t)
                        logger.info("[%s][%7s] @%s: %s",
                                    phase_label, t["tweet_type"], t["author_username"],
                                    (t["content"] or "")[:60])
            return handle_response

        page_a.on("response", _make_handler("posts", tab_a_tweets))
        page_b.on("response", _make_handler("replies", tab_b_tweets))

        async def scroll_tab(page, url, phase_label, tab_tweets, max_tab_scrolls):
            """Scroll one tab until cutoff, known-only streak, or stall."""
            known_only_streak = 0
            logger.info("Navigating to %s", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                logger.warning("nav failed for %s", url, exc_info=True)
                return
            await page.wait_for_timeout(_browser.human_load_delay_ms())
            for scroll_count in range(max_tab_scrolls):
                prev_tab_count = len(tab_tweets)
                prev_gql = len(graphql_responses)
                await page.evaluate(
                    f"window.scrollTo(0, document.body.scrollHeight * "
                    f"{_browser.human_scroll_distance_pct()})"
                )
                await page.wait_for_timeout(_browser.human_scroll_delay_ms())

                target_count = sum(
                    1 for t in tab_tweets
                    if (t.get("author_username") or "").lower() == username.lower()
                )
                _session.heartbeat(
                    status="running", phase=phase_label,
                    progress_current=scroll_count, progress_total=max_tab_scrolls,
                    tweets_collected=target_count, target_handle=username,
                )
                new_tab_t = len(tab_tweets) - prev_tab_count
                new_g = len(graphql_responses) - prev_gql

                # 1. Cutoff date
                if new_tab_t > 0:
                    all_old = True
                    for t in tab_tweets[-new_tab_t:]:
                        try:
                            td = datetime.strptime(t.get("created_at", ""),
                                                   "%a %b %d %H:%M:%S %z %Y")
                            if td >= cutoff_date:
                                all_old = False
                                break
                        except (ValueError, TypeError):
                            all_old = False
                            break
                    if all_old:
                        logger.info("[%s] hit %d-day boundary at scroll %d",
                                    phase_label, cutoff_days, scroll_count)
                        break

                # 2. Known-only streak
                if known_ids and new_tab_t > 0:
                    new_ids_this_scroll = {
                        t["tweet_id"] for t in tab_tweets[-new_tab_t:]
                        if t.get("tweet_id")
                    }
                    if new_ids_this_scroll and new_ids_this_scroll.issubset(known_ids):
                        known_only_streak += 1
                    else:
                        known_only_streak = 0
                    if known_only_streak >= 3:
                        logger.info(
                            "[%s] 3 consecutive known-only scrolls; stopping at %d",
                            phase_label, scroll_count,
                        )
                        break

                # 3. Empty stall
                if not new_tab_t and not new_g and scroll_count > 3:
                    await page.wait_for_timeout(2000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)
                    if (len(tab_tweets) == prev_tab_count
                            and len(graphql_responses) == prev_gql):
                        break

        try:
            await asyncio.gather(
                scroll_tab(page_a, f"https://x.com/{username}", "scrolling_posts",
                           tab_a_tweets, max_scrolls_posts),
                scroll_tab(page_b, f"https://x.com/{username}/with_replies",
                           "scrolling_replies", tab_b_tweets, max_scrolls_replies),
            )
            pool.report_success(handle_a)
            if handle_b is not handle_a:
                pool.report_success(handle_b)
        except Exception as exc:
            err = str(exc).lower()
            if "429" in err or "rate" in err or "too many" in err:
                pool.report_429(handle_a)
                if handle_b is not handle_a:
                    pool.report_429(handle_b)
            else:
                pool.release(handle_a)
                if handle_b is not handle_a:
                    pool.release(handle_b)
            raise

        await browser.close()

        target_count = sum(
            1 for t in all_tweets
            if (t.get("author_username") or "").lower() == username.lower()
        )
        context_count = len(all_tweets) - target_count
        logger.info("Captured: %d by @%s + %d context tweets (other authors)",
                    target_count, username, context_count)
        _session.heartbeat(phase="storing", tweets_collected=target_count)

        # Store tweets via lib.storage (one transaction per tweet — survives
        # partial failures, matches old behaviour).
        inserted_target = 0
        inserted_context = 0
        try:
            conn, supaconn, cur = _storage.dual_connect()
            for t in all_tweets:
                if not t.get("tweet_id"):
                    continue
                au = t.get("author_username", username)
                is_target = (au or "").lower() == username.lower()
                aid = _storage.ensure_account(cur, au)
                if not aid:
                    continue
                try:
                    _storage.update_account_profile(cur, aid, tweet=t)
                except Exception:
                    logger.warning("account UPDATE failed for @%s", au, exc_info=True)
                    conn.rollback()
                    if supaconn: supaconn.rollback()
                tid = _storage.upsert_tweet(cur, t, author_account_id=aid,
                                            scrape_source="collect")
                if tid:
                    _storage.dual_commit(conn, supaconn)
                    if is_target:
                        inserted_target += 1
                    else:
                        inserted_context += 1
                else:
                    conn.rollback()
                    if supaconn: supaconn.rollback()
            conn.close()
            if supaconn: supaconn.close()
            logger.info("DB: %d stored for @%s + %d context tweets",
                        inserted_target, username, inserted_context)
        except Exception:
            logger.exception("DB store failed for @%s", username)

    # Snapshot account stats for growth tracking.
    try:
        conn, supaconn, cur = _storage.dual_connect()
        cur.execute("SELECT id FROM accounts WHERE username=%s", (username,))
        row = cur.fetchone()
        if row:
            aid = row[0]
            _storage.stamp_account_updated(cur, aid)
            _storage.record_account_snapshot(cur, aid)
        _storage.dual_commit(conn, supaconn)
        conn.close()
        if supaconn: supaconn.close()
    except Exception:
        logger.warning("post-scrape account-stamp failed", exc_info=True)

    return all_tweets, graphql_responses


# ── CLI ────────────────────────────────────────────────────────────────


def print_report(tweets, graphql_count, target_username=None):
    if not tweets:
        print("\nNo tweets collected.")
        return 0
    target_tweets = [
        t for t in tweets
        if not target_username
        or (t.get("author_username") or "").lower() == target_username.lower()
    ]
    context_count = len(tweets) - len(target_tweets)
    types = {}
    for t in target_tweets:
        types[t.get("tweet_type", "?")] = types.get(t.get("tweet_type", "?"), 0) + 1
    print(f"\n{'=' * 50}")
    print(f"Scraped {len(tweets)} tweets across "
          f"{len({t['author_username'] for t in tweets if t.get('author_username')})} authors "
          f"({graphql_count} GraphQL responses)")
    if target_username:
        print(f"  @{target_username}: {len(target_tweets)} tweets  "
              f"({types.get('original', 0)} originals · {types.get('reply', 0)} replies · "
              f"{types.get('retweet', 0)} retweets · {types.get('quote', 0)} quotes)")
        print(f"  Context (other authors): {context_count} tweets")
    else:
        print(f"  Originals: {types.get('original', 0)}  Retweets: {types.get('retweet', 0)}  "
              f"Quotes: {types.get('quote', 0)}  Replies: {types.get('reply', 0)}")
    output = os.path.join(RAW_DIR,
                          f"collected_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(output, "w") as f:
        json.dump(tweets, f, indent=2, default=str)
    print(f"  Saved: {output}")
    return len(target_tweets)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 collect.py <username> [--headful] [--limit N] [--fresh] [--days N]")
        sys.exit(1)
    username = sys.argv[1]
    from vibechecx_config import SCRAPER_HEADFUL
    headful = True if SCRAPER_HEADFUL else "--headful" in sys.argv
    fresh = "--fresh" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    if "--days" in sys.argv:
        try:
            CUTOFF_DAYS = int(sys.argv[sys.argv.index("--days") + 1])
        except (ValueError, IndexError):
            pass
    cf = get_next_cookie()
    logger.info("VibeChecx Collector — @%s (cookies: %s, cutoff=%dd)",
                username, cf.split('/')[-1], CUTOFF_DAYS)
    try:
        tweets, gql = asyncio.run(collect_profile(username, headful, limit, fresh, cf))
        target_n = print_report(tweets, len(gql), target_username=username)
        _session.finish("completed", tweets_collected=target_n)
    except Exception as exc:
        logger.exception("collect failed for @%s", username)
        _session.finish("failed", error=f"{exc}\n{traceback.format_exc()}"[:2000])
        sys.exit(1)
