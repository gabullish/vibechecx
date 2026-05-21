"""User A's data never leaks to User B."""
from fastapi.testclient import TestClient


def test_scrapes_per_user(client, make_user, make_single_profile, db):
    alice = make_user("alice", "pw12345")
    bob = make_user("bob", "pw12345")
    # alice has a scrape
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_sessions(user_id, session_type, target_handle, status) "
            "VALUES(%s, 'profile_scrape', 'alpha', 'completed') RETURNING id",
            (alice["id"],),
        )
    db.commit()
    client.login("bob", "pw12345")
    r = client.get("/scrapes")
    assert r.status_code == 200
    assert "alpha" not in r.text, "bob should not see alice's scrape rows"
    assert "No scrapes yet" in r.text


def test_cohorts_per_user(client, make_user, make_cohort_profile, db):
    alice = make_user("alice", "pw12345")
    bob = make_user("bob", "pw12345")
    make_cohort_profile(alice["id"], name="AliceCohort", members=("xyz",))
    client.login("bob", "pw12345")
    r = client.get("/cohorts")
    assert r.status_code == 200
    assert "AliceCohort" not in r.text


def test_profiles_per_user(client, make_user, make_single_profile):
    alice = make_user("alice", "pw12345")
    bob = make_user("bob", "pw12345")
    make_single_profile(alice["id"], handle="alpha")
    client.login("bob", "pw12345")
    r = client.get("/profiles", follow_redirects=False)
    # bob has no profiles -> redirect to wizard
    assert r.status_code in (302, 303)
    assert "/wizard/1" in r.headers.get("location", "")
