"""DB-backed scrape status: round-trip, multi-user, stale reconciliation."""
import os
from vibechecx_scrape_status import (
    start_session, heartbeat, finish_session,
    current_for_user, history_for_user, reconcile_stale, is_live,
)


def test_start_and_heartbeat(make_user):
    u = make_user("alice", "pw12345")
    sid = start_session(u["id"], session_type="profile_scrape", target_handle="alpha", progress_total=50)
    heartbeat(sid, status="running", phase="scrolling", progress_current=10, tweets_collected=7)
    row = current_for_user(u["id"])
    assert row["id"] == sid
    assert row["status"] == "running"
    assert row["phase"] == "scrolling"
    assert row["progress_current"] == 10
    assert row["tweets_collected"] == 7
    assert row["target_handle"] == "alpha"
    assert is_live(row)


def test_finish_completed(make_user):
    u = make_user("bob", "pw12345")
    sid = start_session(u["id"], session_type="profile_scrape", target_handle="x", progress_total=10)
    finish_session(sid, status="completed", tweets_collected=42)
    row = current_for_user(u["id"])
    assert row["status"] == "completed"
    assert row["tweets_collected"] == 42
    assert not is_live(row)


def test_per_user_isolation(make_user):
    a = make_user("alice", "pw12345")
    b = make_user("bob", "pw12345")
    start_session(a["id"], "profile_scrape", target_handle="alpha", progress_total=10)
    b_sid = start_session(b["id"], "profile_scrape", target_handle="beta", progress_total=10)
    row = current_for_user(b["id"])
    assert row["id"] == b_sid
    assert row["target_handle"] == "beta"


def test_stale_running_row_gets_reconciled(db, make_user):
    """A 'running' row with no heartbeat for 10 minutes and a dead pid should
    be marked failed."""
    u = make_user("carol", "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, status, phase, "
            "target_handle, pid, last_heartbeat_at, started_at) "
            "VALUES (%s, 'profile_scrape', 'running', 'scrolling', 'x', %s, "
            "NOW() - INTERVAL '10 minutes', NOW() - INTERVAL '10 minutes') RETURNING id",
            (u["id"], 999999),  # 999999 unlikely to be alive
        )
        sid = cur.fetchone()["id"]
    db.commit()
    reconcile_stale()
    row = current_for_user(u["id"])
    assert row["id"] == sid
    assert row["status"] == "failed"
    assert "heartbeat timeout" in (row.get("error_log") or "")


def test_history_returns_per_user_only(make_user):
    a = make_user("alice", "pw12345")
    b = make_user("bob", "pw12345")
    start_session(a["id"], "profile_scrape", target_handle="alpha", progress_total=10)
    start_session(b["id"], "profile_scrape", target_handle="beta", progress_total=10)
    a_rows = history_for_user(a["id"])
    assert len(a_rows) == 1
    assert a_rows[0]["target_handle"] == "alpha"
