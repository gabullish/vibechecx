"""Shared scope resolver for collector scripts.

All collector entrypoints accept the same `--account / --cohort / --profile`
flags. This module turns those flags into:

    {
      "handles":     [username, ...],   # for navigating to profile pages
      "account_ids": [aid, ...],        # for tweet/cohort_members joins
      "label":       "human readable",  # for status display
      "user_id":     <int or None>,     # owner if --profile resolved
    }

Used by collect.py (multi-handle batch), patrol.py, replyminer.py, and
coordinator.py.
"""
import argparse
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG  # noqa: E402


def add_scope_args(parser: argparse.ArgumentParser):
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--account", help="single X handle (no @)")
    g.add_argument("--cohort", type=int, help="cohort_id")
    g.add_argument("--profile", type=int, help="profile_id")


def resolve(args) -> dict:
    """Resolve argparse Namespace (or dict-like) into a scope dict."""
    if hasattr(args, "get"):
        account = args.get("account")
        cohort = args.get("cohort")
        profile = args.get("profile")
    else:
        account = getattr(args, "account", None)
        cohort = getattr(args, "cohort", None)
        profile = getattr(args, "profile", None)

    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            if account:
                handle = account.lstrip("@").lower()
                cur.execute(
                    "INSERT INTO accounts(username) VALUES(%s) "
                    "ON CONFLICT(username) DO UPDATE SET username=EXCLUDED.username "
                    "RETURNING id",
                    (handle,),
                )
                aid = cur.fetchone()["id"]
                conn.commit()
                return {
                    "handles": [handle],
                    "account_ids": [aid],
                    "label": f"@{handle}",
                    "user_id": None,
                    "cohort_id": None,
                    "profile_id": None,
                }
            if cohort:
                cur.execute(
                    "SELECT a.id, a.username FROM cohort_members cm "
                    "JOIN accounts a ON a.id = cm.account_id "
                    "WHERE cm.cohort_id = %s",
                    (cohort,),
                )
                rows = cur.fetchall()
                cur.execute("SELECT name, user_id FROM cohorts WHERE id=%s", (cohort,))
                meta = cur.fetchone()
                return {
                    "handles": [r["username"] for r in rows],
                    "account_ids": [r["id"] for r in rows],
                    "label": (meta and meta["name"]) or f"cohort#{cohort}",
                    "user_id": meta["user_id"] if meta else None,
                    "cohort_id": cohort,
                    "profile_id": None,
                }
            if profile:
                cur.execute(
                    "SELECT p.*, c.name AS cohort_name FROM profiles p "
                    "LEFT JOIN cohorts c ON c.id = p.cohort_id "
                    "WHERE p.id = %s",
                    (profile,),
                )
                p = cur.fetchone()
                if not p:
                    raise ValueError(f"profile {profile} not found")
                if p["cohort_id"]:
                    cur.execute(
                        "SELECT a.id, a.username FROM cohort_members cm "
                        "JOIN accounts a ON a.id = cm.account_id "
                        "WHERE cm.cohort_id = %s",
                        (p["cohort_id"],),
                    )
                    rows = cur.fetchall()
                    return {
                        "handles": [r["username"] for r in rows],
                        "account_ids": [r["id"] for r in rows],
                        "label": p["name"] or p["cohort_name"] or f"profile#{profile}",
                        "user_id": p["user_id"],
                        "cohort_id": p["cohort_id"],
                        "profile_id": profile,
                    }
                if p["target_handle"]:
                    handle = p["target_handle"].lstrip("@")
                    cur.execute(
                        "INSERT INTO accounts(username) VALUES(%s) "
                        "ON CONFLICT(username) DO UPDATE SET username=EXCLUDED.username "
                        "RETURNING id",
                        (handle,),
                    )
                    aid = cur.fetchone()["id"]
                    conn.commit()
                    return {
                        "handles": [handle],
                        "account_ids": [aid],
                        "label": p["name"] or f"@{handle}",
                        "user_id": p["user_id"],
                        "cohort_id": None,
                        "profile_id": profile,
                    }
                raise ValueError(f"profile {profile} has no resolvable accounts")
        raise ValueError("no scope flag provided")
    finally:
        conn.close()


def select_tweets_in_scope(scope: dict, *, days: int = 14, only_with_replies: bool = False,
                            exclude_already_mined: bool = False, limit: int = 200) -> list:
    """Return list of dicts: {tweet_id, username, replies, likes}."""
    if not scope["account_ids"]:
        return []
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT t.tweet_id, a.username, t.replies, t.likes
                FROM tweets t JOIN accounts a ON a.id = t.author_account_id
                WHERE t.author_account_id = ANY(%s)
                  AND t.created_at >= NOW() - INTERVAL %s
            """
            params = [scope["account_ids"], f"{int(days)} days"]
            if only_with_replies:
                sql += " AND t.replies > 0"
            if exclude_already_mined:
                sql += " AND t.tweet_id NOT IN (SELECT DISTINCT tweet_id FROM replies WHERE tweet_id IS NOT NULL)"
            sql += " ORDER BY t.replies DESC, t.likes DESC LIMIT %s"
            params.append(int(limit))
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
