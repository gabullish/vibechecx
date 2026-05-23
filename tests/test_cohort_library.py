"""Cohort library: fork, dedup, and discover-page library check."""
import pytest


def _seed_public_cohort(db, owner_id, seed_handle, members=("alpha", "beta", "gamma")):
    """Create a is_public cohort owned by owner_id with given members."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO cohorts(user_id, name, slug, seed_handle, is_public) "
            "VALUES(%s, %s, %s, %s, TRUE) RETURNING id",
            (owner_id, f"@{seed_handle}", f"{seed_handle}_{owner_id}", seed_handle),
        )
        cid = cur.fetchone()["id"]
        for m in members:
            cur.execute(
                "INSERT INTO accounts(username) VALUES(%s) ON CONFLICT(username) DO NOTHING RETURNING id",
                (m,),
            )
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT id FROM accounts WHERE username=%s", (m,))
                row = cur.fetchone()
            cur.execute(
                "INSERT INTO cohort_members(cohort_id, account_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (cid, row["id"]),
            )
    db.commit()
    return cid


def test_fork_creates_cohort_and_profile(db, make_user):
    owner = make_user("alice", "pw12345")
    src_cid = _seed_public_cohort(db, owner["id"], "solflare")

    from web.routes.discovery import _fork_cohort
    bob = make_user("bob", "pw12345")
    cid, pid = _fork_cohort(src_cid, bob["id"])

    assert cid is not None and pid is not None

    with db.cursor() as cur:
        cur.execute("SELECT * FROM cohorts WHERE id=%s", (cid,))
        cohort = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM cohort_members WHERE cohort_id=%s", (cid,))
        mc = cur.fetchone()["n"]
        cur.execute("SELECT * FROM profiles WHERE id=%s", (pid,))
        prof = cur.fetchone()

    assert cohort["user_id"] == bob["id"]
    assert cohort["seed_handle"] == "solflare"
    assert mc == 3
    assert prof["cohort_id"] == cid
    assert prof["user_id"] == bob["id"]


def test_fork_is_idempotent(db, make_user):
    """Forking the same source cohort twice returns the same cohort and profile."""
    owner = make_user("alice", "pw12345")
    src_cid = _seed_public_cohort(db, owner["id"], "phantom")

    from web.routes.discovery import _fork_cohort
    bob = make_user("bob", "pw12345")
    cid1, pid1 = _fork_cohort(src_cid, bob["id"])
    cid2, pid2 = _fork_cohort(src_cid, bob["id"])

    assert cid1 == cid2
    assert pid1 == pid2

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM profiles WHERE user_id=%s AND cohort_id=%s", (bob["id"], cid1))
        assert cur.fetchone()["n"] == 1


def test_fork_non_public_cohort_returns_none(db, make_user):
    owner = make_user("alice", "pw12345")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO cohorts(user_id, name, slug, is_public) VALUES(%s,'private','private_1',FALSE) RETURNING id",
            (owner["id"],),
        )
        cid = cur.fetchone()["id"]
    db.commit()

    from web.routes.discovery import _fork_cohort
    bob = make_user("bob", "pw12345")
    result_cid, result_pid = _fork_cohort(cid, bob["id"])
    assert result_cid is None and result_pid is None


def test_discover_page_shows_library_chips(client, db, make_user):
    """Library cohorts appear as chips on the discover page."""
    owner = make_user("alice", "pw12345")
    _seed_public_cohort(db, owner["id"], "magiceden")
    client.login("alice", "pw12345")
    resp = client.get("/discover")
    assert resp.status_code == 200
    assert "magiceden" in resp.text
    assert "Already in the library" in resp.text


def test_discover_start_intercepts_library_match(client, db, make_user):
    """POST /discover/start returns library card instead of starting a scan."""
    owner = make_user("alice", "pw12345")
    _seed_public_cohort(db, owner["id"], "testproject")
    bob = make_user("bob", "pw12345")
    client.login("bob", "pw12345")
    resp = client.post("/discover/start", data={"handle": "testproject"})
    assert resp.status_code == 200
    assert "Found in cohort library" in resp.text
    assert "Use from library" in resp.text


def test_discover_start_force_skips_library(client, db, make_user):
    """POST /discover/start with force=1 skips library check and starts scan."""
    owner = make_user("alice", "pw12345")
    _seed_public_cohort(db, owner["id"], "forcetest")
    bob = make_user("bob", "pw12345")
    client.login("bob", "pw12345")
    resp = client.post("/discover/start", data={"handle": "forcetest", "force": "1"})
    assert resp.status_code == 200
    # Should show the scanning spinner, not the library card
    assert "Found in cohort library" not in resp.text
    assert "affiliates" in resp.text.lower() or "scanning" in resp.text.lower()
