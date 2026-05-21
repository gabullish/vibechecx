#!/usr/bin/env python3
"""VibeChecx — LLM enrichment with DeepSeek primary + Grok fallback.

Adds sentiment, tags, classification, quality/inorganic scores to tweets.

Auto-fallback: tries DeepSeek first. If it fails (network, 401, 429, 500,
empty body, malformed JSON) twice in a row, switches to Grok (xAI) for the
rest of the run. Switches back next process. Either provider's credentials
can be absent; we just skip enrichment if neither is available.
"""
import argparse
import json
import logging
import os
import sys
import time
import traceback

import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
from vibechecx_config import DB_CONFIG, deepseek_api_key, xai_api_key  # noqa: E402

logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s enrich: %(message)s")
logger = logging.getLogger("vibechecx.enrich")


# ── LLM providers ────────────────────────────────────────────────────


class Provider:
    name = "none"
    model = None

    def __init__(self, api_key, base_url, model):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def call(self, prompt):
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.1,
        )
        return resp.choices[0].message.content.strip()


def make_deepseek():
    key = deepseek_api_key()
    if not key:
        return None
    p = Provider(key, "https://api.deepseek.com/v1", "deepseek-chat")
    p.name = "deepseek"
    return p


def make_grok():
    key = xai_api_key()
    if not key:
        return None
    p = Provider(key, "https://api.x.ai/v1", "grok-2-latest")
    p.name = "grok"
    return p


PROMPT = """Analyze this tweet and return JSON only:

{content}

Return exactly:
{{"sentiment": "positive|negative|neutral|funny",
 "content_type": "thread|single|media|poll|link",
 "tags": ["tag1","tag2","tag3"],
 "category": "work_related|personal|promotional|engagement|community",
 "quality_score": 0-100,
 "inorganic_score": 0-1}}

Where:
- content_type: 'thread' if part of a thread, 'single' for standalone, 'media' for image/video, 'link' for link-only
- tags: 3-5 specific keywords about the topic (e.g. 'solana', 'brazil', 'garage_cohort', 'solflare', 'web3')
- category: what type of content this is
- quality_score: how engaging/substantive (0=pure spam, 100=high value)
- inorganic_score: likelihood this is engagement-farming/astroturfing (0=organic, 1=obviously inorganic)
"""


def _parse_response(text):
    if not text:
        return None
    if text.startswith("```"):
        # strip ```json … ``` fence
        parts = text.split("\n", 1)
        if len(parts) == 2:
            text = parts[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


class LLMRouter:
    """Tries primary; on repeated failure flips to secondary for the rest of the run."""

    def __init__(self, primary, secondary):
        self.primary = primary
        self.secondary = secondary
        self.active = primary or secondary
        self.consecutive_failures = 0
        self.failure_threshold = 2

    def analyze(self, content):
        if not self.active:
            return None, "no_provider"
        prompt = PROMPT.format(content=content[:1500])
        # Up to 4 attempts: two with primary, then two with secondary after switch.
        attempts_remaining = 4
        switched = False
        while attempts_remaining > 0:
            attempts_remaining -= 1
            try:
                text = self.active.call(prompt)
                parsed = _parse_response(text)
                if parsed is None:
                    raise ValueError("malformed JSON response")
                self.consecutive_failures = 0
                return parsed, self.active.name
            except Exception as exc:
                self.consecutive_failures += 1
                logger.warning("%s call failed: %s",
                               self.active.name, str(exc)[:120])
                if (self.consecutive_failures >= self.failure_threshold
                        and not switched
                        and self.active is self.primary
                        and self.secondary is not None):
                    logger.warning("Switching enrichment provider: %s → %s",
                                   self.primary.name, self.secondary.name)
                    self.active = self.secondary
                    self.consecutive_failures = 0
                    switched = True
                    continue
                time.sleep(0.3)
        return None, self.active.name if self.active else "none"


def make_router():
    return LLMRouter(primary=make_deepseek(), secondary=make_grok())


# ── DB ───────────────────────────────────────────────────────────────


def get_unprocessed_tweets(limit=20, username=None, account_ids=None):
    """Tweets that haven't been enriched yet, scoped by username or account_ids."""
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT t.tweet_id, t.content, a.username as author_username,
                       t.likes, t.views
                FROM tweets t JOIN accounts a ON t.author_account_id = a.id
                WHERE (t.content_type IS NULL OR t.content_type = '')
                  AND t.is_retweet = false
                  AND length(t.content) > 5
            """
            params = []
            if account_ids:
                sql += " AND t.author_account_id = ANY(%s)"
                params.append(list(account_ids))
            elif username:
                sql += " AND a.username = %s"
                params.append(username)
            sql += " ORDER BY t.likes DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


_SENTIMENT_MAP = {
    "positive": 1.0, "funny": 0.8, "neutral": 0.0,
    "negative": -1.0, "mixed": 0.0,
}


def update_tweet_in_db(tweet_id, analysis):
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            sentiment_str = (analysis.get("sentiment") or "neutral").lower()
            sentiment = _SENTIMENT_MAP.get(sentiment_str, 0.0)
            tags = analysis.get("tags") or []
            # Coerce malformed score fields safely
            def _f(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            cur.execute(
                """
                UPDATE tweets SET
                    sentiment = %s,
                    content_type = %s,
                    tags = %s,
                    category = %s,
                    quality_score = %s,
                    inorganic_score = %s,
                    validation_status = 'unverified'
                WHERE tweet_id = %s
                """,
                (
                    sentiment,
                    analysis.get("content_type"),
                    tags,
                    analysis.get("category"),
                    _f(analysis.get("quality_score")),
                    _f(analysis.get("inorganic_score")),
                    tweet_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ── Stage entrypoint ─────────────────────────────────────────────────


def run_enrich_for_scope(scope, *, limit=50, session_id=None, router=None,
                         _ss_hb=None, rate_sleep=0.5):
    """Stage entrypoint used by the coordinator. Returns dict with counts."""
    router = router or make_router()
    if router.active is None:
        logger.warning("no LLM provider available (no DeepSeek or Grok key); skipping enrichment")
        return {"enriched": 0, "failed": 0, "provider": "none", "skipped": True}
    account_ids = scope.get("account_ids") if scope else None
    tweets = get_unprocessed_tweets(limit=limit, account_ids=account_ids)
    total = len(tweets)
    if _ss_hb and session_id:
        try:
            _ss_hb(session_id, phase="enriching", progress_total=total,
                   progress_current=0, target_handle=scope.get("label") if scope else None)
        except Exception:
            logger.debug("heartbeat failed", exc_info=True)
    enriched = 0
    failed = 0
    for i, t in enumerate(tweets):
        analysis, provider = router.analyze(t["content"] or "")
        if analysis:
            update_tweet_in_db(t["tweet_id"], analysis)
            enriched += 1
        else:
            failed += 1
        if _ss_hb and session_id:
            try:
                _ss_hb(session_id, progress_current=i + 1, tweets_collected=enriched)
            except Exception:
                pass
        if rate_sleep:
            time.sleep(rate_sleep)
    logger.info("enrich done: %d/%d via %s (failed %d)",
                enriched, total, router.active.name if router.active else "none", failed)
    return {"enriched": enriched, "failed": failed, "provider": router.active.name,
            "skipped": False}


# ── CLI ──────────────────────────────────────────────────────────────


def _existing_session_id():
    sid = os.environ.get("VIBECHECX_SCRAPE_SESSION_ID")
    try:
        return int(sid) if sid else None
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM enrichment with auto-fallback")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--account", help="username scope")
    g.add_argument("--cohort", type=int)
    g.add_argument("--profile", type=int)
    parser.add_argument("--limit", type=int, default=20)
    args, _ = parser.parse_known_args()

    scope = None
    if args.account or args.cohort or args.profile:
        try:
            from collector.scope import resolve as resolve_scope
        except ImportError:
            from scope import resolve as resolve_scope
        scope = resolve_scope(args)

    try:
        from web.vibechecx_scrape_status import heartbeat as ss_hb
    except Exception:
        ss_hb = None
    sid = _existing_session_id()

    try:
        result = run_enrich_for_scope(scope or {"account_ids": None, "label": "all"},
                                      limit=args.limit, session_id=sid, _ss_hb=ss_hb)
        if result.get("skipped"):
            sys.exit(0)
        print(f"Enriched {result['enriched']}/{result['enriched']+result['failed']} "
              f"via {result['provider']} (failed {result['failed']})")
    except Exception:
        logger.exception("enrich failed")
        sys.exit(1)
