"""Auth: register, login, session rotation, logout clears both cookies."""
from vibechecx_auth import register, login as auth_login, get_user_from_session, hash_password


def test_register_and_login_roundtrip(db):
    ok, err = register("alice", "secret123")
    assert ok and err is None
    uid, err = auth_login("alice", "secret123")
    assert err is None and uid is not None
    bad_uid, err = auth_login("alice", "wrong")
    assert bad_uid is None and err == "Invalid credentials"


def test_legacy_sha256_password_is_upgraded(db):
    import hashlib
    salt = "abc123"
    legacy = f"{salt}:{hashlib.sha256((salt + 'oldpw').encode()).hexdigest()}"
    with db.cursor() as cur:
        cur.execute("INSERT INTO users(username, password_hash) VALUES(%s,%s) RETURNING id",
                    ("legacy_user", legacy))
        uid = cur.fetchone()["id"]
    db.commit()
    new_uid, err = auth_login("legacy_user", "oldpw")
    assert err is None and new_uid == uid
    with db.cursor() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id=%s", (uid,))
        new_hash = cur.fetchone()["password_hash"]
    assert new_hash.startswith("$2b$"), "legacy hash should be upgraded to bcrypt"


def test_logout_clears_both_cookies(client, make_user, make_single_profile):
    u = make_user("bob", "secret123")
    pid = make_single_profile(u["id"], "tracked")
    client.login("bob", "secret123")
    # Set the profile cookie like a real user would.
    r = client.get(f"/set-profile/{pid}", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert client.cookies.get("vibechecx_session")
    assert client.cookies.get("vibechecx_profile") == str(pid)

    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    # Both cookies should be cleared via Set-Cookie headers.
    set_cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else [r.headers.get("set-cookie", "")]
    joined = "\n".join(set_cookies)
    assert "vibechecx_session=" in joined
    assert "vibechecx_profile=" in joined


def test_login_rotates_prior_sessions(db, client, make_user):
    u = make_user("carol", "secret123")
    client.login("carol", "secret123")
    first_sid = client.cookies.get("vibechecx_session")
    assert first_sid
    # second client, same user
    from fastapi.testclient import TestClient
    from web.app import app
    c2 = TestClient(app)
    c2.post("/login", data={"username": "carol", "password": "secret123"}, follow_redirects=False)
    # The first session should be invalidated.
    assert get_user_from_session(first_sid) is None
