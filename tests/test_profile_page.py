"""Regression: /profile shows the active profile's identity, never @SolGab."""
import pytest


@pytest.fixture
def alice(make_user):
    return make_user("alice", "pw12345")


def test_single_profile_shows_handle(client, alice, make_single_profile, db):
    pid = make_single_profile(alice["id"], handle="quokka")
    # update follower count so we can assert it
    with db.cursor() as cur:
        cur.execute("UPDATE accounts SET followers_count=4242 WHERE username=%s", ("quokka",))
    db.commit()
    client.login("alice", "pw12345")
    client.get(f"/set-profile/{pid}", follow_redirects=False)
    r = client.get("/profile")
    assert r.status_code == 200
    assert "@quokka" in r.text
    assert "@SolGab" not in r.text
    # The headline shows compact format (4.2k) with full value in the title attribute.
    assert "4.2k" in r.text or "4,242" in r.text


def test_cohort_profile_shows_cohort_header(client, alice, make_cohort_profile):
    info = make_cohort_profile(alice["id"], name="Crew", members=("foo", "bar", "baz"))
    client.login("alice", "pw12345")
    client.get(f"/set-profile/{info['profile_id']}", follow_redirects=False)
    r = client.get("/profile")
    assert r.status_code == 200
    assert "3 members" in r.text or "members" in r.text
    assert "@SolGab" not in r.text


def test_no_profile_redirects_to_profiles(client, alice):
    client.login("alice", "pw12345")
    # no profile cookie set
    r = client.get("/profile", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/profiles" in r.headers.get("location", "")
