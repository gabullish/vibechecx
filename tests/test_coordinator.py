"""Coordinator: full collect → patrol → reply-mine cycle, scope-aware.

These tests inject fake stage runners so they don't need Playwright. They
prove that:
  - one master `full_scrape` session row is created and finishes 'completed'
  - one history row per session_type lands in scrape_sessions
  - the patrol stage bumps tweets.last_measured_at
  - the reply-mine stage actually inserts rows in `replies`
  - the scope resolves correctly for both single and cohort profiles
  - failure in any stage marks the master row failed (no lying)
"""
import psycopg2
from psycopg2.extras import RealDictCursor

from vibechecx_config import DB_CONFIG
from collector.coordinator import run_full_scrape
from collector.scope import resolve as resolve_scope


def _session_rows():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM scrape_sessions ORDER BY id")
        rows = cur.fetchall()
    conn.close()
    return rows


def _add_tweet(db, account_id, tweet_id, replies=0, days_ago=1):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, "
            "is_retweet, replies, last_measured_at) "
            "VALUES (%s, %s, NOW() - INTERVAL %s, %s, FALSE, %s, NOW() - INTERVAL '8 days')",
            (tweet_id, account_id, f"{days_ago} days", f"tweet {tweet_id}", replies),
        )
    db.commit()


def test_full_cycle_writes_all_three_session_types(db, make_user, make_cohort_profile):
    """End-to-end: coordinator opens a master + 3 child session rows."""
    u = make_user("alice", "pw12345")
    info = make_cohort_profile(u["id"], name="Crew", members=("foo", "bar"))

    # Seed: each member has one tweet, one of which has replies > 0
    with db.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE username='foo'")
        foo_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM accounts WHERE username='bar'")
        bar_id = cur.fetchone()["id"]
    _add_tweet(db, foo_id, "t_foo_1", replies=5)
    _add_tweet(db, bar_id, "t_bar_1", replies=0)

    scope = resolve_scope({"profile": info["profile_id"]})
    assert set(scope["handles"]) == {"foo", "bar"}

    def fake_collect(scope, sid, **_):
        return 2  # pretend we stored 2 tweets

    def fake_patrol(scope, sid, **_):
        # Bump last_measured_at to simulate a metric refresh.
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tweets SET last_measured_at=NOW() "
                "WHERE author_account_id = ANY(%s)",
                (scope["account_ids"],),
            )
        conn.commit()
        conn.close()
        return {"patrolled": 1, "total": 2}

    def fake_replymine(scope, sid, **_):
        # Insert a reply for t_foo_1.
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE username='bar'")
            replier_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO replies(tweet_id, reply_id, author_account_id, content, created_at) "
                "VALUES (%s, %s, %s, %s, NOW())",
                ("t_foo_1", "r_001", replier_id, "great point!"),
            )
        conn.commit()
        conn.close()
        return {"replies_stored": 1, "tweets_mined": 1}

    def fake_enrich(scope, sid, **_):
        return {"enriched": 1, "failed": 0, "provider": "fake", "skipped": False}

    master_sid = run_full_scrape(
        scope, user_id=u["id"],
        collect_stage=fake_collect,
        patrol_stage=fake_patrol,
        replymine_stage=fake_replymine,
        enrich_stage=fake_enrich,
    )

    rows = _session_rows()
    types = {r["session_type"]: r for r in rows}
    assert "full_scrape" in types
    assert "profile_scrape" in types
    assert "metric_patrol" in types
    assert "reply_mining" in types
    assert "enrichment" in types
    assert types["full_scrape"]["id"] == master_sid
    assert types["full_scrape"]["status"] == "completed"
    for t in ("profile_scrape", "metric_patrol", "reply_mining", "enrichment"):
        assert types[t]["status"] == "completed"

    # Patrol stage bumped last_measured_at.
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*)::int AS c FROM tweets "
            "WHERE last_measured_at > NOW() - INTERVAL '1 minute'"
        )
        assert cur.fetchone()["c"] >= 2

    # Reply-mine stage stored a reply.
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*)::int AS c FROM replies WHERE tweet_id=%s", ("t_foo_1",))
        assert cur.fetchone()["c"] == 1


def test_coordinator_marks_failed_on_stage_crash(db, make_user, make_single_profile):
    u = make_user("bob", "pw12345")
    pid = make_single_profile(u["id"], handle="alpha")
    scope = resolve_scope({"profile": pid})

    def boom(scope, sid, **_):
        raise RuntimeError("synthetic patrol failure")

    import pytest
    with pytest.raises(RuntimeError):
        run_full_scrape(
            scope, user_id=u["id"],
            collect_stage=lambda s, sid, **_: 0,
            patrol_stage=boom,
            replymine_stage=lambda s, sid, **_: {"replies_stored": 0, "tweets_mined": 0},
        )

    rows = _session_rows()
    master = next(r for r in rows if r["session_type"] == "full_scrape")
    assert master["status"] == "failed"
    assert "synthetic patrol failure" in (master["error_log"] or "")


def test_scope_resolves_single_profile(db, make_user, make_single_profile):
    u = make_user("carol", "pw12345")
    pid = make_single_profile(u["id"], handle="solo")
    scope = resolve_scope({"profile": pid})
    assert scope["handles"] == ["solo"]
    assert scope["user_id"] == u["id"]
    assert scope["cohort_id"] is None


def test_scope_select_tweets_with_replies_excludes_already_mined(db, make_user, make_single_profile):
    """The replymine selector must skip tweets we've already gotten replies for."""
    from collector.scope import select_tweets_in_scope
    u = make_user("dave", "pw12345")
    pid = make_single_profile(u["id"], handle="popular")
    scope = resolve_scope({"profile": pid})
    aid = scope["account_ids"][0]
    _add_tweet(db, aid, "fresh", replies=10)
    _add_tweet(db, aid, "already_mined", replies=5)
    # Mark `already_mined` as having a reply in DB.
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO replies(tweet_id, reply_id, author_account_id, content, created_at) "
            "VALUES (%s, %s, %s, %s, NOW())",
            ("already_mined", "r_pre", aid, "existing"),
        )
    db.commit()
    targets = select_tweets_in_scope(
        scope, days=14, only_with_replies=True, exclude_already_mined=True, limit=10
    )
    tids = {t["tweet_id"] for t in targets}
    assert "fresh" in tids
    assert "already_mined" not in tids
