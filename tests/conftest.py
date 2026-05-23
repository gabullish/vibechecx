"""Shared pytest fixtures.

The test DB is named via $VIBECHECX_DB_NAME (default: vibechecx_test). It must
exist before tests run and have schema.sql + migrations applied:

    createdb vibechecx_test
    psql -d vibechecx_test -f db/schema.sql -f db/migrations/001_scrape_status.sql
"""
import os
import sys
import pytest
import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web"))
sys.path.insert(0, os.path.join(ROOT, "collector"))

# Force the test DB before any vibechecx module imports config.
os.environ.setdefault("VIBECHECX_DB_NAME", "vibechecx_test")
os.environ.setdefault("VIBECHECX_LOG_LEVEL", "WARNING")


@pytest.fixture(autouse=True)
def _truncate_tables():
    """Wipe mutable tables between tests so each starts from a known state."""
    from vibechecx_config import DB_CONFIG
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE sessions, scrape_sessions, discovery_sessions, "
            "tweet_observations, replies, media, tweets, cohort_members, "
            "cohort_interactions, profiles, cohorts, accounts, users "
            "RESTART IDENTITY CASCADE"
        )
        for tbl in ('insights_cache', 'scrape_queue'):
            cur.execute(
                f"TRUNCATE TABLE {tbl} RESTART IDENTITY"
                if _table_exists(cur, tbl)
                else "SELECT 1"
            )
    conn.commit()
    conn.close()
    yield


def _table_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
        (name,),
    )
    return cur.fetchone() is not None


@pytest.fixture
def db():
    from vibechecx_config import DB_CONFIG
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    yield conn
    conn.close()


@pytest.fixture
def make_user(db):
    """Factory: returns a callable that creates a user (with bcrypt) and returns its row."""
    from vibechecx_auth import hash_password

    def _make(username="alice", password="secret123"):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                (username, hash_password(password)),
            )
            uid = cur.fetchone()["id"]
        db.commit()
        return {"id": uid, "username": username, "password": password}

    return _make


@pytest.fixture
def make_account(db):
    def _make(username, followers=0, following=0, display_name=None):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (username, display_name, followers_count, following_count) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (username, display_name or username, followers, following),
            )
            aid = cur.fetchone()["id"]
        db.commit()
        return aid

    return _make


@pytest.fixture
def make_single_profile(db, make_account):
    def _make(user_id, handle="someone"):
        make_account(handle)
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO profiles(user_id, name, type, target_handle) "
                "VALUES (%s, %s, 'single', %s) RETURNING id",
                (user_id, f"@{handle}", handle),
            )
            pid = cur.fetchone()["id"]
        db.commit()
        return pid

    return _make


@pytest.fixture
def make_cohort_profile(db, make_account):
    def _make(user_id, name="Cohort", members=("foo", "bar")):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO cohorts (user_id, name, slug) VALUES (%s, %s, %s) RETURNING id",
                (user_id, name, f"{name}_{user_id}"),
            )
            cid = cur.fetchone()["id"]
            for m in members:
                aid = make_account(m)
                cur.execute(
                    "INSERT INTO cohort_members(cohort_id, account_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (cid, aid),
                )
            cur.execute(
                "INSERT INTO profiles(user_id, name, type, cohort_id) "
                "VALUES (%s, %s, 'cohort', %s) RETURNING id",
                (user_id, name, cid),
            )
            pid = cur.fetchone()["id"]
        db.commit()
        return {"profile_id": pid, "cohort_id": cid}

    return _make


@pytest.fixture
def client():
    """FastAPI TestClient with login helper."""
    from fastapi.testclient import TestClient
    from web.app import app
    c = TestClient(app)

    def login(username, password):
        # Hit /login so a session cookie is set.
        r = c.post(
            "/login", data={"username": username, "password": password},
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), r.text
        return r

    c.login = login  # type: ignore[attr-defined]
    return c
