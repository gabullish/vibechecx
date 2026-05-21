"""profile_account_ids resolves real scopes; no SolGab fallback ever."""
import pytest

from web.app import profile_account_ids, NoActiveProfile


def test_no_profile_raises():
    with pytest.raises(NoActiveProfile):
        profile_account_ids(None)
    with pytest.raises(NoActiveProfile):
        profile_account_ids({})


def test_single_profile_resolves(make_user, make_single_profile, db):
    u = make_user()
    make_single_profile(u["id"], handle="alpha")
    prof = {"id": 1, "type": "single", "cohort_id": None, "target_handle": "alpha"}
    ids = profile_account_ids(prof)
    assert len(ids) == 1


def test_cohort_profile_resolves(make_user, make_cohort_profile):
    u = make_user()
    info = make_cohort_profile(u["id"], name="MyCohort", members=("a", "b", "c"))
    prof = {"id": info["profile_id"], "type": "cohort",
            "cohort_id": info["cohort_id"], "target_handle": None}
    ids = profile_account_ids(prof)
    assert len(ids) == 3


def test_empty_cohort_raises(db, make_user):
    u = make_user()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO cohorts(user_id,name,slug) VALUES(%s,%s,%s) RETURNING id",
            (u["id"], "Empty", f"empty_{u['id']}"),
        )
        cid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO profiles(user_id,name,type,cohort_id) VALUES(%s,%s,'cohort',%s) RETURNING id",
            (u["id"], "Empty", cid),
        )
        pid = cur.fetchone()["id"]
    db.commit()
    prof = {"id": pid, "type": "cohort", "cohort_id": cid, "target_handle": None}
    with pytest.raises(NoActiveProfile):
        profile_account_ids(prof)
