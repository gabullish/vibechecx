#!/usr/bin/env python3
"""VibeChecx Search Collector — find tweets by keyword/hashtag/phrase.

Navigates to x.com/search?q=<query>&f=live and intercepts the SearchTimeline
GraphQL responses, storing discovered tweets and their authors. This is how
we discover accounts beyond the ones we explicitly track — anyone tweeting
about your brand keywords, token names, or hashtags.

Direct API (xapi) for search returned 404 in testing (the SearchTimeline
endpoint appears to require X's client-transaction token that twikit can't
generate), so we stick to Playwright. Cookie comes from the shared pool.

Usage:
    python3 search_collect.py "solflare dojo" --max 100 --days 7
    python3 search_collect.py "#solflare" --tab top
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web",
))

from vibechecx_config import DB_CONFIG  # noqa: E402

from lib import session as _session  # noqa: E402
from lib import browser as _browser  # noqa: E402
from lib import storage as _storage  # noqa: E402
from lib.cookies import default_pool  # noqa: E402
from lib.parser import extract_tweets_from_graphql  # noqa: E402

logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s search: %(message)s")
logger = logging.getLogger("vibechecx.search")

_GRAPHQL_PATTERN = "x.com/i/api/graphql/"


def upsert_tweet_from_search(t: dict) -> str | None:
    """Store a search-discovered tweet using lib/storage primitives."""
    username = t.get("author_username", "")
    if not username:
        return None
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            aid = _storage.ensure_account(cur, username)
            if not aid:
                return None
            _storage.update_account_profile(cur, aid, tweet=t)
            tid = _storage.upsert_tweet(cur, t, author_account_id=aid,
                                        scrape_source="search")
        conn.commit()
        return tid
    except Exception:
        conn.rollback()
        logger.warning("upsert_tweet_from_search failed for %s",
                       t.get("tweet_id"), exc_info=True)
        return None
    finally:
        conn.close()


async def search_collect(
    query: str,
    *,
    max_tweets: int = 200,
    days: int = 7,
    tab: str = "live",
) -> list[dict]:
    """Collect tweets matching `query`. tab: 'live' (latest) or 'top'."""
    from playwright.async_api import async_playwright

    pool = default_pool()
    handle = await pool.acquire(exclusive=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    encoded = quote_plus(query)
    tab_param = "live" if tab == "live" else "top"
    url = f"https://x.com/search?q={encoded}&src=typed_query&f={tab_param}"

    all_tweets: list[dict] = []
    seen_ids: set[str] = set()
    logger.info("search: query=%r url=%s", query, url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await _browser.open_context(browser, cookie_path=handle.path)
        page = await ctx.new_page()
        await _browser.apply_stealth(page)

        async def capture(response):
            if _GRAPHQL_PATTERN not in response.url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            for t in extract_tweets_from_graphql(body):
                if not (t and t.get("tweet_id")):
                    continue
                if t["tweet_id"] in seen_ids:
                    continue
                seen_ids.add(t["tweet_id"])
                all_tweets.append(t)

        page.on("response", capture)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            logger.warning("search: nav failed for %s", url, exc_info=True)
            await browser.close()
            pool.release(handle)
            return []

        await page.wait_for_timeout(_browser.human_load_delay_ms())

        no_new_streak = 0
        max_scrolls = max(20, max_tweets // 10)

        try:
            for scroll_n in range(max_scrolls):
                prev = len(all_tweets)
                await page.evaluate(
                    f"window.scrollTo(0, document.body.scrollHeight * "
                    f"{_browser.human_scroll_distance_pct()})"
                )
                await page.wait_for_timeout(_browser.human_scroll_delay_ms())

                if len(all_tweets) > prev:
                    no_new_streak = 0
                    all_old = True
                    for t in all_tweets[prev:]:
                        try:
                            td = datetime.strptime(
                                t.get("created_at", ""),
                                "%a %b %d %H:%M:%S %z %Y",
                            )
                            if td >= cutoff:
                                all_old = False
                                break
                        except (ValueError, TypeError):
                            all_old = False
                            break
                    if all_old:
                        logger.info("search: hit %d-day boundary at scroll %d",
                                    days, scroll_n + 1)
                        break
                else:
                    no_new_streak += 1
                    if no_new_streak >= 4:
                        logger.info("search: stalled at scroll %d", scroll_n + 1)
                        break

                if len(all_tweets) >= max_tweets:
                    logger.info("search: reached max_tweets=%d", max_tweets)
                    break

                if (scroll_n + 1) % 10 == 0:
                    logger.info("search: scroll %d — %d tweets",
                                scroll_n + 1, len(all_tweets))
            pool.report_success(handle)
        except Exception as exc:
            err = str(exc).lower()
            if "429" in err or "rate" in err:
                pool.report_429(handle)
            else:
                pool.release(handle)
            raise
        finally:
            await browser.close()

    stored = 0
    for t in all_tweets:
        if upsert_tweet_from_search(t):
            stored += 1
    logger.info("search: done — %d discovered, %d stored", len(all_tweets), stored)
    return all_tweets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Collect tweets by keyword/hashtag via X search"
    )
    parser.add_argument("queries", nargs="+",
                        help="Search queries (e.g. 'solflare dojo' '#phantom')")
    parser.add_argument("--max", type=int, default=200,
                        help="Max tweets per query (default 200)")
    parser.add_argument("--days", type=int, default=7,
                        help="Days back to collect (default 7)")
    parser.add_argument("--tab", choices=["live", "top"], default="live",
                        help="live=Latest tweets, top=Top tweets (default live)")
    args = parser.parse_args()

    try:
        for q in args.queries:
            logger.info("--- query: %r ---", q)
            tweets = asyncio.run(
                search_collect(q, max_tweets=args.max, days=args.days, tab=args.tab)
            )
            logger.info("query %r: %d tweets", q, len(tweets))
        _session.finish("completed")
    except Exception as exc:
        logger.exception("search_collect failed")
        _session.finish("failed",
                        error=f"{exc}\n{traceback.format_exc()}"[:2000])
        sys.exit(1)
