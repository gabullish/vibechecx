"""/scrape-progress sets HX-Trigger header on terminal states (§10.5)."""
import json


def test_completed_session_sets_hx_trigger_scrape_complete(client, make_user, db):
    u = make_user("alice", "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, status, target_handle, "
            "tweets_collected, ended_at, last_heartbeat_at, started_at) "
            "VALUES(%s, 'profile_scrape', 'completed', 'alpha', 47, NOW(), NOW(), NOW())",
            (u["id"],),
        )
    db.commit()
    client.login("alice", "pw12345")
    r = client.get("/scrape-progress")
    assert r.status_code == 200
    trig = r.headers.get("hx-trigger") or r.headers.get("HX-Trigger")
    assert trig, "expected HX-Trigger header on completed session"
    payload = json.loads(trig)
    assert "scrape-complete" in payload
    assert payload["scrape-complete"]["handle"] == "alpha"
    assert payload["scrape-complete"]["new_tweets"] == 47


def test_failed_session_sets_hx_trigger_scrape_failed(client, make_user, db):
    u = make_user("bob", "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, status, error_log, "
            "ended_at, last_heartbeat_at, started_at) "
            "VALUES(%s, 'profile_scrape', 'failed', 'cookie expired', NOW(), NOW(), NOW())",
            (u["id"],),
        )
    db.commit()
    client.login("bob", "pw12345")
    r = client.get("/scrape-progress")
    assert r.status_code == 200
    trig = r.headers.get("hx-trigger") or r.headers.get("HX-Trigger")
    assert trig, "expected HX-Trigger header on failed session"
    payload = json.loads(trig)
    assert "scrape-failed" in payload
    assert "cookie expired" in payload["scrape-failed"]["error"]


def test_running_session_does_not_set_hx_trigger(client, make_user, db):
    u = make_user("carol", "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, status, phase, target_handle, "
            "progress_current, progress_total, last_heartbeat_at, started_at) "
            "VALUES(%s, 'profile_scrape', 'running', 'scrolling_posts', 'beta', 3, 10, NOW(), NOW())",
            (u["id"],),
        )
    db.commit()
    client.login("carol", "pw12345")
    r = client.get("/scrape-progress")
    assert r.status_code == 200
    trig = r.headers.get("hx-trigger") or r.headers.get("HX-Trigger")
    assert not trig
    # Running view contains vitals fields
    assert "phase" in r.text.lower()
    assert "tweets/min" in r.text
