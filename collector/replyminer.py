#!/usr/bin/env python3
"""VibeChecx Reply Miner — fetch reply threads for any scope's recent tweets.

For each tweet in scope with `replies > 0` that isn't already mined,
navigates to its permalink, intercepts the TweetDetail GraphQL response,
scrolls within the thread (8 passes — picks up replies beyond initial render),
and stores every reply tweet in the `replies` table.

Scope: --account / --cohort / --profile (see collector.scope).
Heartbeats: scrape_sessions session_type='reply_mining'.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
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
from lib.cookies import default_pool  # noqa: E402
from lib.parser import parse_conversation_response  # noqa: E402
from scope import add_scope_args, resolve as resolve_scope, select_tweets_in_scope  # noqa: E402

logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s replymine: %(message)s")
logger = logging.getLogger("vibechecx.replymine")


# ── DB helper (own connection) ─────────────────────────────────────────


def insert_reply(parent_tweet_id, parent_author_username, reply_tweet):
    """Store one reply tweet. Returns reply_id or None."""
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            author_username = reply_tweet.get("author_username", "")
            if not author_username:
                return None
            author_id = _storage.ensure_account(cur, author_username)
            if not author_id:
                return None
            rid = _storage.insert_reply(
                cur,
                parent_tweet_id=parent_tweet_id,
                parent_username=parent_author_username,
                reply=reply_tweet,
                reply_author_account_id=author_id,
            )
        conn.commit()
        return rid
    except Exception:
        conn.rollback()
        logger.warning("insert_reply failed for %s", reply_tweet.get("tweet_id"),
                       exc_info=True)
        return None
    finally:
        conn.close()


# ── mine_replies (Playwright) ──────────────────────────────────────────


async def mine_replies(targets, session_id=None):
    """targets: list of {tweet_id, username}. Returns count of inserted replies."""
    from playwright.async_api import async_playwright

    pool = default_pool()
    handle = await pool.acquire(exclusive=True)

    total = len(targets)
    inserted = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await _browser.open_context(browser, cookie_path=handle.path)
        page = await context.new_page()
        await _browser.apply_stealth(page)
        await _browser.block_heavy_resources(page)

        captured_per_page: dict[str, dict] = {}
        _current = {"tid": None}

        async def handle_response(response):
            if "x.com/i/api/graphql/" not in response.url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            tweets = parse_conversation_response(body)
            captured_per_page.setdefault(_current["tid"], {}).update(tweets)

        page.on("response", handle_response)
        await page.goto("https://x.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(_browser.human_load_delay_ms())

        try:
            for i, t in enumerate(targets):
                tid = t["tweet_id"]
                uname = t["username"]
                _current["tid"] = tid
                captured_per_page[tid] = {}
                url = f"https://x.com/{uname}/status/{tid}"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(_browser.human_load_delay_ms())
                except Exception:
                    logger.warning("nav failed for %s", url, exc_info=True)
                    continue

                # Scroll inside the thread — X paginates reply timelines, so
                # the first render usually has ~10-15 replies; 8 scrolls covers
                # ~100 replies. Bail on stall.
                prev_count = len(captured_per_page[tid])
                stall_streak = 0
                for _ in range(8):
                    await page.evaluate(
                        f"window.scrollTo(0, document.body.scrollHeight * "
                        f"{_browser.human_scroll_distance_pct()})"
                    )
                    await page.wait_for_timeout(_browser.human_scroll_delay_ms())
                    new_count = len(captured_per_page[tid])
                    if new_count == prev_count:
                        stall_streak += 1
                        if stall_streak >= 2:
                            break
                    else:
                        stall_streak = 0
                    prev_count = new_count

                # Persist replies for this parent.
                # IMPORTANT: a captured tweet only qualifies as a "reply to
                # this parent" if its own reply_to_tweet_id MATCHES the parent
                # we're mining. TweetDetail GraphQL responses contain the
                # ancestor chain too (i.e. if `tid` is itself a reply, the
                # tweets gab was replying to come back in the same payload).
                # The earlier filter allowed those ancestors through — they
                # have empty reply_to_tweet_id, which is falsy, so the `and`
                # short-circuited. That stored 50/55 rows in `replies` with
                # the directionality inverted. Require an exact match now.
                for r_tid, r_tweet in captured_per_page[tid].items():
                    if r_tid == tid:
                        continue
                    if r_tweet.get("reply_to_tweet_id") != tid:
                        continue
                    if insert_reply(tid, uname, r_tweet):
                        inserted += 1

                if session_id:
                    _session.heartbeat(progress_current=i + 1,
                                       progress_total=total,
                                       tweets_collected=inserted)
                if (i + 1) % 10 == 0 or i == total - 1:
                    logger.info("[%d/%d] mined — %d replies stored",
                                i + 1, total, inserted)

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

    return inserted


# ── scope entrypoint ───────────────────────────────────────────────────


def run_replymine_for_scope(scope: dict, *, limit: int = 30,
                            session_id: int | None = None, do_mine=None) -> dict:
    """Select tweets-with-replies in scope, mine them, return counts."""
    targets = select_tweets_in_scope(
        scope, days=14, only_with_replies=True,
        exclude_already_mined=True, limit=limit,
    )
    total = len(targets)
    if session_id:
        _session.heartbeat(phase="reply_mining", progress_total=total,
                           progress_current=0, target_handle=scope["label"])
    if not targets:
        return {"replies_stored": 0, "tweets_mined": 0}
    runner = do_mine or mine_replies
    stored = asyncio.run(runner(targets, session_id=session_id))
    return {"replies_stored": int(stored or 0), "tweets_mined": total}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mine replies for a scope's recent tweets")
    add_scope_args(parser)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    try:
        scope = resolve_scope(args)
    except ValueError as e:
        logger.error("scope error: %s", e)
        sys.exit(1)

    existing_sid = _session.current_session_id()
    sid = existing_sid or _session.start_owned_session(
        user_id=scope.get("user_id"),
        session_type="reply_mining",
        target_handle=scope["label"],
        cohort_id=scope.get("cohort_id"),
    )
    try:
        res = run_replymine_for_scope(scope, limit=args.limit, session_id=sid)
        logger.info("done: %d replies from %d tweets",
                    res["replies_stored"], res["tweets_mined"])
        if sid and not existing_sid:
            _session.finish("completed", tweets_collected=res["replies_stored"])
    except Exception as exc:
        logger.exception("replymine failed")
        if sid and not existing_sid:
            _session.finish("failed",
                            error=f"{exc}\n{traceback.format_exc()}"[:2000])
        sys.exit(1)
