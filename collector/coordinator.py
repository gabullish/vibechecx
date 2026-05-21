#!/usr/bin/env python3
"""VibeChecx Scrape Coordinator — full collect → patrol → reply-mine cycle.

Web UI launches this with `--profile <id> --user <id>`. It opens a single
`scrape_sessions` row (session_type='full_scrape') and walks through three
phases, updating `phase` on the row so the UI polls one stable record.

Each phase also writes its own `scrape_sessions` row (session_type='profile_scrape',
'metric_patrol', 'reply_mining') for first-class history on /scrapes.

Stage functions are injectable via keyword arg for tests.
"""
import argparse
import asyncio
import logging
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))

from vibechecx_config import DB_CONFIG  # noqa: E402
from vibechecx_scrape_status import (  # noqa: E402
    start_session as ss_start,
    heartbeat as ss_hb,
    finish_session as ss_finish,
)
from scope import resolve as resolve_scope  # noqa: E402

logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s coordinator: %(message)s")
logger = logging.getLogger("vibechecx.coordinator")


# ── Default stage implementations ────────────────────────────────────


def default_collect_stage(scope, master_sid, *, limit=100):
    """Run collect for each handle in scope.

    §10.4A — when there is more than one handle, delegate to batch.py which
    runs 3 parallel Playwright workers (one per cookie). Sequential
    collect_profile() is reserved for single-handle scopes where the parallel
    Posts/With-replies split inside collect_profile (§10.4B) is the win.
    """
    handles = scope["handles"]
    n = len(handles)
    ss_hb(master_sid, phase="collecting", progress_total=n, progress_current=0,
          target_handle=scope["label"])

    if n > 1:
        from batch import batch_scrape
        try:
            total_tweets, total_done = asyncio.run(
                batch_scrape(handles, cohort_name=scope["label"])
            )
            ss_hb(master_sid, progress_current=total_done, tweets_collected=total_tweets)
            return total_tweets
        except Exception:
            logger.exception("batch_scrape failed; falling back to sequential")
            # Fall through to the sequential loop below if batch blew up.

    from collect import collect_profile, get_next_cookie
    headful = os.environ.get("VIBECHECX_SCRAPER_HEADFUL", "false").lower() == "true"
    total = 0
    for i, handle in enumerate(handles):
        ss_hb(master_sid, phase=f"collecting @{handle}", progress_current=i,
              target_handle=handle)
        cf = get_next_cookie()
        try:
            tweets, _ = asyncio.run(collect_profile(handle, headful, limit, False, cf))
            target_n = sum(
                1 for t in tweets
                if (t.get("author_username") or "").lower() == handle.lower()
            )
            total += target_n
        except Exception:
            logger.exception("collect_profile failed for @%s", handle)
        ss_hb(master_sid, progress_current=i + 1, tweets_collected=total)
    return total


def default_patrol_stage(scope, master_sid, *, limit=50):
    from patrol import run_patrol_for_scope
    ss_hb(master_sid, phase="metric_patrol", target_handle=scope["label"])
    return run_patrol_for_scope(scope, limit=limit, session_id=master_sid)


def default_replymine_stage(scope, master_sid, *, limit=30):
    from replyminer import run_replymine_for_scope
    ss_hb(master_sid, phase="reply_mining", target_handle=scope["label"])
    return run_replymine_for_scope(scope, limit=limit, session_id=master_sid)


def default_enrich_stage(scope, master_sid, *, limit=50):
    """Run LLM enrichment (DeepSeek primary, Grok fallback)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from enrich import run_enrich_for_scope
    from vibechecx_scrape_status import heartbeat as _hb
    ss_hb(master_sid, phase="enriching", target_handle=scope["label"])
    return run_enrich_for_scope(scope, limit=limit, session_id=master_sid, _ss_hb=_hb)


def _invalidate_insights_for_scope(scope):
    """After a successful scrape, drop cached strategic-thesis insights so the
    next panel view rebuilds on fresh data. Cohort scrapes also invalidate
    every member account's cache. Silent on failure — caching is a UX win, not
    a correctness requirement."""
    try:
        import psycopg2
        from lib.storage import invalidate_insights_cache
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            with conn.cursor() as cur:
                if scope.get("cohort_id"):
                    n = invalidate_insights_cache(
                        cur, scope_type="cohort", scope_id=scope["cohort_id"],
                    )
                    logger.info("invalidated %d insights_cache rows for cohort %s",
                                n, scope["cohort_id"])
                else:
                    handles = scope.get("handles") or []
                    if handles:
                        # Look up the account ids for the handles
                        cur.execute(
                            "SELECT id FROM accounts WHERE LOWER(username) = ANY(%s)",
                            ([h.lower() for h in handles],),
                        )
                        for (account_id,) in cur.fetchall():
                            invalidate_insights_cache(
                                cur, scope_type="account", scope_id=account_id,
                            )
                        logger.info("invalidated insights_cache for %d account(s)",
                                    len(handles))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("insights cache invalidation failed (non-fatal)",
                       exc_info=True)


def _write_stage_history(scope, session_type, status, tweets_count, error=None):
    """One history row per stage, owned by the coordinator. Survives any stage
    impl (real or injected) so /scrapes always reflects what happened."""
    sid = ss_start(
        user_id=scope.get("user_id"),
        session_type=session_type,
        target_handle=scope["label"],
        cohort_id=scope.get("cohort_id"),
    )
    ss_finish(sid, status=status, tweets_collected=tweets_count, error=error)
    return sid


# ── Public entrypoint ────────────────────────────────────────────────


def run_full_scrape(scope, *, user_id=None,
                    collect_stage=None, patrol_stage=None, replymine_stage=None,
                    enrich_stage=None,
                    collect_limit=100, patrol_limit=50, replymine_limit=30,
                    enrich_limit=50):
    """Open one master session row, run all four phases. Returns the master_sid.

    Stages are pluggable so tests don't need Playwright or live LLM APIs.
    """
    collect_stage = collect_stage or default_collect_stage
    patrol_stage = patrol_stage or default_patrol_stage
    replymine_stage = replymine_stage or default_replymine_stage
    enrich_stage = enrich_stage or default_enrich_stage

    env_sid = os.environ.get("VIBECHECX_SCRAPE_SESSION_ID")
    if env_sid:
        master_sid = int(env_sid)
    else:
        master_sid = ss_start(
            user_id=user_id if user_id is not None else scope.get("user_id"),
            session_type="full_scrape",
            target_handle=scope["label"],
            cohort_id=scope.get("cohort_id"),
            progress_total=4,
        )
    try:
        ss_hb(master_sid, status="running", phase="starting", progress_current=0,
              progress_total=4)
        # 1/3 collect
        try:
            tweets = collect_stage(scope, master_sid, limit=collect_limit) or 0
            _write_stage_history(scope, "profile_scrape", "completed", tweets)
        except Exception as exc:
            _write_stage_history(scope, "profile_scrape", "failed", 0, error=str(exc)[:500])
            raise
        ss_hb(master_sid, progress_current=1, tweets_collected=tweets)
        # 2/3 patrol
        try:
            p_res = patrol_stage(scope, master_sid, limit=patrol_limit) or {}
            _write_stage_history(scope, "metric_patrol", "completed",
                                 (p_res.get("patrolled") if isinstance(p_res, dict) else 0) or 0)
        except Exception as exc:
            _write_stage_history(scope, "metric_patrol", "failed", 0, error=str(exc)[:500])
            raise
        ss_hb(master_sid, progress_current=2)
        # 3/4 reply-mine
        try:
            r_res = replymine_stage(scope, master_sid, limit=replymine_limit) or {}
            _write_stage_history(scope, "reply_mining", "completed",
                                 (r_res.get("replies_stored") if isinstance(r_res, dict) else 0) or 0)
        except Exception as exc:
            _write_stage_history(scope, "reply_mining", "failed", 0, error=str(exc)[:500])
            raise
        ss_hb(master_sid, progress_current=3)
        # 4/4 enrich (best-effort: provider outages shouldn't fail the run)
        try:
            e_res = enrich_stage(scope, master_sid, limit=enrich_limit) or {}
            if e_res.get("skipped"):
                logger.info("enrich skipped (no provider keys)")
            else:
                _write_stage_history(
                    scope, "enrichment", "completed",
                    (e_res.get("enriched") if isinstance(e_res, dict) else 0) or 0,
                )
        except Exception as exc:
            logger.warning("enrichment failed (continuing): %s", exc)
            _write_stage_history(scope, "enrichment", "failed", 0, error=str(exc)[:500])
        # Fresh scrape → stale insight cache. Drop matching rows so the next
        # view rebuilds on the new data. Non-fatal on failure.
        _invalidate_insights_for_scope(scope)
        ss_hb(master_sid, progress_current=4, phase="done")
        ss_finish(master_sid, status="completed", tweets_collected=tweets)
    except Exception as exc:
        logger.exception("full scrape failed")
        ss_finish(master_sid, status="failed",
                  error=f"{exc}\n{traceback.format_exc()}"[:2000])
        raise
    return master_sid


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run full collect+patrol+replymine cycle")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--account")
    g.add_argument("--cohort", type=int)
    g.add_argument("--profile", type=int)
    parser.add_argument("--user", type=int, default=None,
                        help="user_id to attribute the scrape to")
    parser.add_argument("--collect-limit", type=int, default=100)
    parser.add_argument("--patrol-limit", type=int, default=50)
    parser.add_argument("--replymine-limit", type=int, default=30)
    parser.add_argument("--enrich-limit", type=int, default=50)
    parser.add_argument("--days", type=int, default=None,
                        help="override CUTOFF_DAYS for this run")
    args = parser.parse_args()
    if args.days is not None:
        os.environ["VIBECHECX_CUTOFF_DAYS"] = str(args.days)

    try:
        scope = resolve_scope(args)
    except ValueError as e:
        logger.error("scope error: %s", e)
        sys.exit(1)

    try:
        run_full_scrape(
            scope, user_id=args.user or scope.get("user_id"),
            collect_limit=args.collect_limit,
            patrol_limit=args.patrol_limit,
            replymine_limit=args.replymine_limit,
            enrich_limit=args.enrich_limit,
        )
    except Exception:
        sys.exit(1)
