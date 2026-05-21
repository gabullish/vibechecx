"""Cancel-scrape, cookie health pill, /cookies page."""
import os
import pytest


def test_cancel_marks_session_cancelled(client, make_user, db):
    u = make_user("alice", "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, status, phase, pid, "
            "started_at, last_heartbeat_at) "
            "VALUES (%s, 'profile_scrape', 'running', 'scrolling', %s, NOW(), NOW()) "
            "RETURNING id",
            (u["id"], 999999),  # bogus pid; kill will silently fail, that's fine
        )
        sid = cur.fetchone()["id"]
    db.commit()
    client.login("alice", "pw12345")
    r = client.post(f"/cancel-scrape/{sid}")
    assert r.status_code == 200
    with db.cursor() as cur:
        cur.execute("SELECT status, error_log FROM scrape_sessions WHERE id=%s", (sid,))
        row = cur.fetchone()
    assert row["status"] == "cancelled"
    assert "cancelled by user" in (row["error_log"] or "")


def test_cancel_rejects_other_users(client, make_user, db):
    a = make_user("alice", "pw12345")
    b = make_user("bob",   "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, status, started_at, last_heartbeat_at) "
            "VALUES (%s, 'profile_scrape', 'running', NOW(), NOW()) RETURNING id",
            (a["id"],),
        )
        sid = cur.fetchone()["id"]
    db.commit()
    client.login("bob", "pw12345")
    r = client.post(f"/cancel-scrape/{sid}")
    assert "Not found" in r.text
    with db.cursor() as cur:
        cur.execute("SELECT status FROM scrape_sessions WHERE id=%s", (sid,))
        assert cur.fetchone()["status"] == "running"


def test_cancel_no_op_if_already_finished(client, make_user, db):
    u = make_user("carol", "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, status, started_at, ended_at) "
            "VALUES (%s, 'profile_scrape', 'completed', NOW(), NOW()) RETURNING id",
            (u["id"],),
        )
        sid = cur.fetchone()["id"]
    db.commit()
    client.login("carol", "pw12345")
    r = client.post(f"/cancel-scrape/{sid}")
    assert r.status_code == 200
    assert "Already finished" in r.text


def test_cookies_page_renders(client, make_user):
    u = make_user("dave", "pw12345")
    client.login("dave", "pw12345")
    r = client.get("/cookies")
    assert r.status_code == 200
    assert "Cookie health" in r.text
    # one row per known cookie slot
    for slot in ("main.json", "scraper1.json", "scraper2.json"):
        assert slot in r.text


def test_cookie_health_pill_only_when_unhealthy(tmp_path, monkeypatch):
    """If all cookies are fresh and big, the nav pill stays empty (no clutter)."""
    from web import app as app_module
    fake_dir = tmp_path
    # Make 3 fake-fresh cookie files
    for name in ("main.json", "scraper1.json", "scraper2.json"):
        p = fake_dir / name
        p.write_text("x" * 500)
    monkeypatch.setattr(app_module, "COOKIE_DIR", str(fake_dir))
    assert app_module.cookie_health_pill_html() == ""

    # Remove one — pill should appear
    (fake_dir / "main.json").unlink()
    assert "cookies 2/3" in app_module.cookie_health_pill_html()
