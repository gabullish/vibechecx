"""Precision meter: per-profile, honest about missing data."""
from vibechecx_precision import compute, precision_badge_html


def _add_tweets(db, account_id, count, days_ago=1):
    with db.cursor() as cur:
        for i in range(count):
            cur.execute(
                "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, "
                "is_retweet, last_measured_at) "
                "VALUES (%s, %s, NOW() - INTERVAL %s, %s, FALSE, NOW())",
                (f"t_{account_id}_{i}_{days_ago}",
                 account_id, f"{days_ago} days", f"tweet {i}"),
            )
    db.commit()


def test_no_profile_returns_no_data():
    res = compute(None)
    assert res["overall"] is None


def test_freshness_high_when_recent_scrape(db, make_user, make_single_profile):
    u = make_user()
    pid = make_single_profile(u["id"], handle="alpha")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE username=%s", ("alpha",))
        aid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, target_account_id, session_type, status, ended_at) "
            "VALUES(%s, %s, 'profile_scrape', 'completed', NOW())",
            (u["id"], aid),
        )
    db.commit()
    _add_tweets(db, aid, 5, days_ago=1)
    prof = {"id": pid, "type": "single", "cohort_id": None, "target_handle": "alpha"}
    res = compute(prof)
    assert res["components"]["freshness"]["score"] is not None
    assert res["components"]["freshness"]["score"] >= 0.9


def test_no_data_components_are_none(db, make_user, make_single_profile):
    u = make_user()
    pid = make_single_profile(u["id"], handle="empty")
    prof = {"id": pid, "type": "single", "cohort_id": None, "target_handle": "empty"}
    res = compute(prof)
    assert res["components"]["freshness"]["score"] is None
    assert res["components"]["coverage"]["score"] is None
    assert res["components"]["accuracy"]["score"] is None
    assert res["overall"] is None


def test_badge_html_says_no_data_when_empty(make_user, make_single_profile):
    u = make_user()
    pid = make_single_profile(u["id"], handle="empty")
    prof = {"id": pid, "type": "single", "cohort_id": None, "target_handle": "empty"}
    html = precision_badge_html(prof)
    assert "No data yet" in html
    # never the old fake number
    assert "97.5" not in html


def test_badge_never_mentions_solgab(make_user, make_single_profile):
    u = make_user()
    pid = make_single_profile(u["id"], handle="someoneelse")
    prof = {"id": pid, "type": "single", "cohort_id": None, "target_handle": "someoneelse"}
    html = precision_badge_html(prof)
    assert "SolGab" not in html
