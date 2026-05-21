"""Leaderboard pure SQL — ordering, latency, empty state."""
import time
import pytest


def _seed_cohort_with_tweets(db, owner_id, name, members_with_tweets):
    """members_with_tweets: list of (handle, followers, [(likes, views, is_reply), ...])"""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO cohorts(user_id, name, slug) VALUES(%s,%s,%s) RETURNING id",
            (owner_id, name, f"{name}_{owner_id}"),
        )
        cid = cur.fetchone()["id"]
        for handle, followers, tweets in members_with_tweets:
            cur.execute(
                "INSERT INTO accounts(username, followers_count) VALUES(%s,%s) "
                "ON CONFLICT(username) DO UPDATE SET followers_count=EXCLUDED.followers_count RETURNING id",
                (handle, followers),
            )
            aid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO cohort_members(cohort_id, account_id) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING",
                (cid, aid),
            )
            for i, (likes, views, is_reply) in enumerate(tweets):
                cur.execute(
                    "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, "
                    "likes, views, is_reply, is_retweet) "
                    "VALUES(%s,%s, NOW() - INTERVAL '2 days', %s, %s, %s, %s, FALSE)",
                    (f"t_{handle}_{i}", aid, f"text {i}", likes, views, is_reply),
                )
    db.commit()
    return cid


def test_leaderboard_orders_by_composite(db, make_user):
    from web.app import _leaderboard_query
    u = make_user("alice", "pw12345")
    cid = _seed_cohort_with_tweets(db, u["id"], "engcohort", [
        # high-likes, high-views — should top the leaderboard
        ("topdog", 1000, [(500, 5000, False), (400, 4000, False)]),
        ("middog", 5000, [(50, 1000, False), (40, 800, False)]),
        ("lowdog", 50, [(2, 100, False)]),
    ])
    rows = _leaderboard_query(cid, "30d")
    assert len(rows) == 3
    # composite is log-scaled engagement / log-scaled audience; topdog has
    # the strongest engagement signal even though middog has more followers.
    assert rows[0]["username"] == "topdog"
    # Reply ratio should be 0 (no replies seeded).
    assert all((r.get("reply_ratio") or 0) == 0 for r in rows)


def test_leaderboard_engagement_rate_is_likes_over_views(db, make_user):
    from web.app import _leaderboard_query
    u = make_user("bob", "pw12345")
    cid = _seed_cohort_with_tweets(db, u["id"], "ratecohort", [
        ("efficient", 100, [(100, 200, False)]),   # 50% engagement
        ("inefficient", 100, [(10, 200, False)]),  # 5% engagement
    ])
    rows = {r["username"]: r for r in _leaderboard_query(cid, "30d")}
    assert abs((rows["efficient"]["engagement_rate"] or 0) - 0.5) < 1e-6
    assert abs((rows["inefficient"]["engagement_rate"] or 0) - 0.05) < 1e-6


def test_leaderboard_reply_ratio(db, make_user):
    from web.app import _leaderboard_query
    u = make_user("carol", "pw12345")
    cid = _seed_cohort_with_tweets(db, u["id"], "replycohort", [
        # 3 originals + 1 reply -> 25% reply ratio
        ("mixed", 100, [(10, 100, False), (10, 100, False), (10, 100, False), (5, 50, True)]),
    ])
    rows = _leaderboard_query(cid, "30d")
    assert rows
    assert abs((rows[0]["reply_ratio"] or 0) - 0.25) < 1e-6


def test_leaderboard_sparkline_has_seven_points(db, make_user):
    from web.app import _leaderboard_query
    u = make_user("dave", "pw12345")
    cid = _seed_cohort_with_tweets(db, u["id"], "sparkcohort", [
        ("only", 100, [(10, 100, False)]),
    ])
    rows = _leaderboard_query(cid, "30d")
    assert rows
    spark = rows[0]["daily_likes"]
    assert isinstance(spark, list) and len(spark) == 7


def test_leaderboard_runs_under_500ms_for_typical_cohort(db, make_user):
    from web.app import _leaderboard_query
    u = make_user("emma", "pw12345")
    members = [
        (f"acct_{i}", 1000 * i, [(i * 10, i * 100, False) for _ in range(5)])
        for i in range(1, 21)  # 20 accounts × 5 tweets = 100 tweets
    ]
    cid = _seed_cohort_with_tweets(db, u["id"], "perfcohort", members)
    t0 = time.time()
    rows = _leaderboard_query(cid, "30d")
    elapsed_ms = (time.time() - t0) * 1000
    assert len(rows) == 20
    assert elapsed_ms < 500, f"leaderboard took {elapsed_ms:.0f}ms, expected <500"
