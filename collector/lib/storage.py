"""DB upserts for the collector layer.

Functions take an open psycopg2 cursor; transaction boundaries stay with
the caller because different scrapers have different needs (collect.py
commits per-tweet to survive partial failures; batch.py commits per-row;
patrol.py commits per-batch).

Single source for the INSERT/UPDATE shapes — when the schema changes, only
this file needs editing.
"""
from __future__ import annotations

import logging
from typing import Iterable, Mapping

logger = logging.getLogger("vibechecx.storage")


# ── accounts ────────────────────────────────────────────────────────────


def ensure_account(cur, username: str) -> int | None:
    """INSERT-or-no-op an accounts row; return the id."""
    cur.execute(
        "INSERT INTO accounts(username) VALUES(%s) "
        "ON CONFLICT(username) DO UPDATE SET username=EXCLUDED.username "
        "RETURNING id",
        (username,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def update_account_profile(cur, account_id: int, *, tweet: Mapping) -> None:
    """Defensive UPDATE — only writes fields we actually have, to avoid the
    historical bug where context-tweet shapes with missing follower counts
    clobbered real values to 0.
    """
    sets: list[str] = []
    vals: list = []
    if tweet.get("author_display"):
        sets.append("display_name=%s"); vals.append(str(tweet["author_display"])[:80])
    if tweet.get("author_avatar"):
        sets.append("avatar_url=%s"); vals.append(tweet["author_avatar"])
    if tweet.get("author_followers"):
        sets.append("followers_count=%s"); vals.append(tweet["author_followers"])
    if tweet.get("author_following"):
        sets.append("following_count=%s"); vals.append(tweet["author_following"])
    if tweet.get("author_tweets"):
        sets.append("tweets_count=%s"); vals.append(tweet["author_tweets"])
    if not sets:
        return
    sets.append("last_updated_at=NOW()")
    vals.append(account_id)
    cur.execute("UPDATE accounts SET " + ", ".join(sets) + " WHERE id=%s", vals)


# ── tweets ──────────────────────────────────────────────────────────────


def upsert_tweet(cur, tweet: Mapping, *, author_account_id: int,
                 scrape_source: str = "collect") -> str | None:
    """Upsert one normalised tweet record. Returns its tweet_id, or None on
    failure (caller is responsible for rollback if appropriate)."""
    try:
        cur.execute(
            """
            INSERT INTO tweets(
                tweet_id, author_account_id, created_at, content, lang,
                is_reply, reply_to_tweet_id, is_quote, is_retweet,
                likes, retweets, replies, quotes, bookmarks, views,
                scrape_source, last_measured_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT(tweet_id) DO UPDATE SET
                likes    = GREATEST(EXCLUDED.likes,    tweets.likes),
                retweets = GREATEST(EXCLUDED.retweets, tweets.retweets),
                replies  = GREATEST(EXCLUDED.replies,  tweets.replies),
                views    = GREATEST(EXCLUDED.views,    tweets.views),
                last_measured_at = NOW()
            RETURNING tweet_id
            """,
            (
                tweet["tweet_id"], author_account_id,
                tweet.get("created_at"),
                (tweet.get("content") or "")[:500],
                tweet.get("lang", ""),
                bool(tweet.get("is_reply")),
                tweet.get("reply_to_tweet_id") or None,
                bool(tweet.get("is_quote")),
                bool(tweet.get("is_retweet")),
                tweet.get("likes",     0),
                tweet.get("retweets",  0),
                tweet.get("replies",   0),
                tweet.get("quotes",    0),
                tweet.get("bookmarks", 0),
                tweet.get("views",     0),
                scrape_source,
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        logger.warning("upsert_tweet failed for %s", tweet.get("tweet_id"),
                       exc_info=True)
        return None


def update_tweet_metrics_batch(
    cur,
    metrics: Mapping[str, Mapping[str, int]],
) -> int:
    """Bulk UPDATE just the engagement metrics. Used by patrol.

    metrics: {tweet_id: {likes, retweets, replies, views}}.
    Returns count of rows actually updated.
    """
    updated = 0
    for tweet_id, m in metrics.items():
        cur.execute(
            """
            UPDATE tweets SET
                likes    = GREATEST(%s, likes),
                retweets = GREATEST(%s, retweets),
                replies  = GREATEST(%s, replies),
                views    = GREATEST(%s, views),
                last_measured_at = NOW()
            WHERE tweet_id = %s
            """,
            (
                int(m.get("likes")    or 0),
                int(m.get("retweets") or 0),
                int(m.get("replies")  or 0),
                int(m.get("views")    or 0),
                tweet_id,
            ),
        )
        updated += cur.rowcount
    return updated


# ── media ──────────────────────────────────────────────────────────────


def insert_media(cur, tweet_id: str, media: Iterable[Mapping]) -> None:
    """ON CONFLICT DO NOTHING insert of attached media for a tweet."""
    for m in media:
        cur.execute(
            "INSERT INTO media(tweet_id, media_type, url) "
            "VALUES(%s, %s, %s) ON CONFLICT DO NOTHING",
            (
                tweet_id,
                m.get("type", "photo"),
                m.get("video_url") or m.get("url", ""),
            ),
        )


# ── tweet observations (cohort visibility) ─────────────────────────────


def log_observation(cur, tweet_id: str, observer_account_id: int,
                    context: str = "scrape") -> None:
    """Record that `observer_account_id` saw this tweet during a scrape.
    Used to track cohort context — who has whom in their timeline."""
    cur.execute(
        "INSERT INTO tweet_observations(tweet_id, observer_account_id, context) "
        "VALUES(%s, %s, %s) ON CONFLICT DO NOTHING",
        (tweet_id, observer_account_id, context),
    )


# ── replies ────────────────────────────────────────────────────────────


def insert_reply(cur, *, parent_tweet_id: str, parent_username: str,
                 reply: Mapping, reply_author_account_id: int) -> str | None:
    """Insert one reply tweet. Used by reply miner.

    Defensive checks (belt-and-suspenders with the DB-level trigger in
    migration 006):
      1. Refuses to insert if the reply has its own `reply_to_tweet_id` and it
         doesn't equal the parent we're attributing it to. This is the
         primary invariant violated by the May-2026 replyminer bug.
      2. Refuses to insert when the reply's created_at predates the parent's
         (impossible for a real reply).
    Both checks return None silently with a warning — callers don't need to
    handle these as errors, they just don't get a row.
    """
    try:
        from datetime import datetime

        reply_parent_ref = reply.get("reply_to_tweet_id")
        if reply_parent_ref and reply_parent_ref != parent_tweet_id:
            logger.warning(
                "insert_reply refused: reply %s claims parent %s, but caller "
                "attributed it to %s — inversion guard",
                reply.get("tweet_id"), reply_parent_ref, parent_tweet_id,
            )
            return None

        # Check timestamp invariant before sending to DB. The trigger will
        # also block this, but we get a clearer log line here and avoid the
        # noisy psycopg2 error.
        reply_created = reply.get("created_at")
        if reply_created:
            try:
                cur.execute(
                    "SELECT created_at FROM tweets WHERE tweet_id=%s",
                    (parent_tweet_id,),
                )
                parent_row = cur.fetchone()
            except Exception:
                parent_row = None
            if parent_row:
                parent_created = parent_row[0]
                # Parse reply_created into datetime if it's a string
                rc = reply_created
                if isinstance(rc, str):
                    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z",
                                "%Y-%m-%d %H:%M:%S%z"):
                        try:
                            rc = datetime.strptime(reply_created, fmt)
                            break
                        except (ValueError, TypeError):
                            continue
                if isinstance(rc, datetime) and parent_created and rc < parent_created:
                    logger.warning(
                        "insert_reply refused: reply %s created at %s predates "
                        "parent %s (%s) — inversion guard",
                        reply.get("tweet_id"), rc, parent_tweet_id, parent_created,
                    )
                    return None

        cur.execute(
            """
            INSERT INTO replies (
                tweet_id, reply_id, author_account_id, content, created_at,
                likes, is_author_reply
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (reply_id) DO UPDATE SET likes = EXCLUDED.likes
            RETURNING reply_id
            """,
            (
                parent_tweet_id,
                reply["tweet_id"],
                reply_author_account_id,
                (reply.get("content") or "")[:1000],
                reply.get("created_at") or datetime.utcnow().isoformat(),
                reply.get("likes", 0),
                (reply.get("author_username", "").lower()
                 == (parent_username or "").lower()),
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        logger.warning("insert_reply failed for %s", reply.get("tweet_id"),
                       exc_info=True)
        return None


# ── account snapshots (follower growth tracking) ───────────────────────


def record_account_snapshot(cur, account_id: int) -> None:
    """Capture current followers/following/tweets_count for growth tracking.
    Reads from accounts (just-updated) and writes a new row in
    account_snapshots. Called after every per-account scrape.
    """
    cur.execute(
        "SELECT followers_count, following_count, tweets_count "
        "FROM accounts WHERE id=%s",
        (account_id,),
    )
    row = cur.fetchone()
    if not row:
        return
    followers, following, tweets_count = row
    cur.execute(
        "INSERT INTO account_snapshots(account_id, followers, following, tweets_count) "
        "VALUES(%s, %s, %s, %s)",
        (account_id, followers or 0, following or 0, tweets_count or 0),
    )


def stamp_account_updated(cur, account_id: int) -> None:
    cur.execute("UPDATE accounts SET last_updated_at=NOW() WHERE id=%s",
                (account_id,))


# ── known-IDs preload ───────────────────────────────────────────────────


def load_known_tweet_ids(cur, username: str, days: int) -> set[str]:
    """Return tweet_ids already in DB for this user within the cutoff window.
    Scoped query — never scan the full tweets table."""
    cur.execute(
        """
        SELECT t.tweet_id FROM tweets t
        JOIN accounts a ON a.id = t.author_account_id
        WHERE LOWER(a.username) = LOWER(%s)
          AND t.created_at > NOW() - INTERVAL '%s days'
        """,
        (username, days),
    )
    return {r[0] for r in cur.fetchall()}


# ── insights cache invalidation ────────────────────────────────────────


def invalidate_insights_cache(cur, *, scope_type: str, scope_id: int) -> int:
    """Delete cached insights so the next view rebuilds on fresh data.

    Called by the coordinator after enrich completes — fresh scrape implies
    stale insights. Drops all periods (24h/7d/14d/30d) for the scope in one
    DELETE.

    For cohort scopes, ALSO invalidate every member account's cached insights
    (someone viewing a member account after a cohort scrape would otherwise
    see stale data). Returns total rows deleted.
    """
    cur.execute(
        "DELETE FROM insights_cache WHERE scope_type=%s AND scope_id=%s",
        (scope_type, scope_id),
    )
    deleted = cur.rowcount
    if scope_type == "cohort":
        cur.execute(
            """
            DELETE FROM insights_cache
            WHERE scope_type='account'
              AND scope_id IN (
                  SELECT account_id FROM cohort_members WHERE cohort_id=%s
              )
            """,
            (scope_id,),
        )
        deleted += cur.rowcount
    return deleted
