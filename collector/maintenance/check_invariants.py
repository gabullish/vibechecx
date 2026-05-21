#!/usr/bin/env python3
"""Data-integrity audit for the VibeChecx scraper tables.

Run periodically (or after any collector change) to verify no scraper bug
silently corrupted the `replies` or `tweets` tables.

Currently checks:
  1. replies whose `created_at` predates their parent tweet's `created_at`
     (impossible for a real reply — was the May-2026 replyminer bug).
  2. tweets that claim `reply_to_tweet_id` but were created before the
     referenced parent (same shape of bug; ought to be impossible for any
     code path).
  3. replies referencing tweets whose author doesn't exist (FK orphans).

Usage:
    python3 check_invariants.py                # audit only
    python3 check_invariants.py --clean        # delete invalid rows
    python3 check_invariants.py --json         # machine-readable output
    python3 check_invariants.py --max-rows 10  # how many examples to show

Exit code: 0 if clean, 1 if any check failed (handy for cron + alerting).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from vibechecx_config import DB_CONFIG


CHECKS = {
    "inverted_replies": {
        "description": "replies whose created_at predates their parent tweet",
        "count_sql": """
            SELECT COUNT(*)
              FROM replies r
              JOIN tweets parent ON parent.tweet_id = r.tweet_id
             WHERE r.created_at < parent.created_at
        """,
        "sample_sql": """
            SELECT r.reply_id, r.likes,
                   replier.username AS replier,
                   parent_acct.username AS parent_author,
                   r.created_at,
                   parent.created_at AS parent_created,
                   LEFT(r.content, 80) AS reply_text,
                   LEFT(parent.content, 80) AS parent_text
              FROM replies r
              JOIN tweets parent  ON parent.tweet_id = r.tweet_id
              JOIN accounts replier ON replier.id = r.author_account_id
              LEFT JOIN accounts parent_acct ON parent_acct.id = parent.author_account_id
             WHERE r.created_at < parent.created_at
             ORDER BY r.likes DESC NULLS LAST
             LIMIT %(limit)s
        """,
        "clean_sql": """
            DELETE FROM replies r
             USING tweets parent
             WHERE r.tweet_id = parent.tweet_id
               AND r.created_at < parent.created_at
        """,
    },
    "inverted_tweet_replies": {
        "description": "tweets whose reply_to_tweet_id points to a tweet created AFTER them",
        "count_sql": """
            SELECT COUNT(*)
              FROM tweets t
              JOIN tweets parent ON parent.tweet_id = t.reply_to_tweet_id
             WHERE t.reply_to_tweet_id IS NOT NULL
               AND t.created_at < parent.created_at
        """,
        "sample_sql": """
            SELECT t.tweet_id, t.created_at, parent.created_at AS parent_created,
                   LEFT(t.content, 80) AS tweet_text,
                   LEFT(parent.content, 80) AS parent_text
              FROM tweets t
              JOIN tweets parent ON parent.tweet_id = t.reply_to_tweet_id
             WHERE t.reply_to_tweet_id IS NOT NULL
               AND t.created_at < parent.created_at
             LIMIT %(limit)s
        """,
        # We do NOT auto-clean tweets — corrupting a tweet row would cascade
        # into observations/replies. Surface only.
        "clean_sql": None,
    },
    "reply_orphans": {
        "description": "replies whose parent tweet_id does not exist in tweets",
        "count_sql": """
            SELECT COUNT(*) FROM replies r
             LEFT JOIN tweets parent ON parent.tweet_id = r.tweet_id
             WHERE parent.tweet_id IS NULL
        """,
        "sample_sql": """
            SELECT r.reply_id, r.tweet_id AS missing_parent,
                   LEFT(r.content, 80) AS reply_text
              FROM replies r
              LEFT JOIN tweets parent ON parent.tweet_id = r.tweet_id
             WHERE parent.tweet_id IS NULL
             LIMIT %(limit)s
        """,
        # Schema has ON DELETE CASCADE so orphans shouldn't exist; if they do,
        # we want to see why before cleaning.
        "clean_sql": None,
    },
}


def run_checks(*, max_rows: int = 5, clean: bool = False) -> dict:
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    results: dict[str, dict] = {}
    try:
        with conn.cursor() as cur:
            for key, check in CHECKS.items():
                cur.execute(check["count_sql"])
                count = cur.fetchone()["count"]
                samples = []
                if count:
                    cur.execute(check["sample_sql"], {"limit": max_rows})
                    samples = [dict(r) for r in cur.fetchall()]
                cleaned = 0
                if clean and count and check.get("clean_sql"):
                    cur.execute(check["clean_sql"])
                    cleaned = cur.rowcount
                results[key] = {
                    "description": check["description"],
                    "count": count,
                    "cleanable": bool(check.get("clean_sql")),
                    "cleaned": cleaned,
                    "samples": samples,
                }
        if clean:
            conn.commit()
    finally:
        conn.close()
    return results


def _print_report(results: dict) -> int:
    """Stdout pretty-print. Returns exit code: 0 clean, 1 if any check failed."""
    failed = 0
    print("VibeChecx data-integrity audit")
    print("=" * 60)
    for key, res in results.items():
        n = res["count"]
        status = "OK" if n == 0 else f"FAIL ({n} rows)"
        print(f"\n[{status}] {key} — {res['description']}")
        if res["cleaned"]:
            print(f"  cleaned: {res['cleaned']} rows deleted")
        elif n and res["cleanable"]:
            print(f"  cleanable: yes (re-run with --clean to delete)")
        elif n and not res["cleanable"]:
            print(f"  cleanable: NO — manual review required")
        for s in res["samples"]:
            print(f"  • {json.dumps(s, default=str)[:200]}")
        if n:
            failed = 1
    print()
    print("=" * 60)
    print("CLEAN" if failed == 0 else "ISSUES FOUND")
    return failed


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true",
                    help="Delete rows that fail cleanable checks.")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON report instead of human-readable text.")
    ap.add_argument("--max-rows", type=int, default=5,
                    help="How many example rows to show per failed check.")
    args = ap.parse_args()
    results = run_checks(max_rows=args.max_rows, clean=args.clean)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
        sys.exit(0 if all(r["count"] == 0 for r in results.values()) else 1)
    sys.exit(_print_report(results))
