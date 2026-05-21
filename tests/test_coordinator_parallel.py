"""§10.4A — coordinator's collect stage uses batch.py when scope has >1 handle."""
import sys


def test_collect_stage_delegates_to_batch_for_multi_handle(monkeypatch):
    """Multi-handle: coordinator should call batch.batch_scrape, not the
    sequential collect_profile path."""
    from collector import coordinator
    called = {"batch": False, "collect_profile": 0}

    async def fake_batch_scrape(handles, cohort_name="batch"):
        called["batch"] = True
        return (42, len(handles))

    # The coordinator does `from batch import batch_scrape` lazily inside
    # default_collect_stage, so we patch the source module. sys.modules is
    # the most reliable hook because importlib will return the cached module.
    import batch as batch_module
    monkeypatch.setattr(batch_module, "batch_scrape", fake_batch_scrape)

    async def fake_collect_profile(*args, **kwargs):
        called["collect_profile"] += 1
        return ([], [])
    import collect as collect_module
    monkeypatch.setattr(collect_module, "collect_profile", fake_collect_profile)
    monkeypatch.setattr(collect_module, "get_next_cookie", lambda: "/tmp/fake.json")

    monkeypatch.setattr(coordinator, "ss_hb", lambda *a, **k: None)

    result = coordinator.default_collect_stage(
        {"handles": ["alpha", "beta", "gamma"], "label": "TestCohort"},
        master_sid=1,
        limit=50,
    )
    assert called["batch"] is True
    assert called["collect_profile"] == 0
    assert result == 42


def test_collect_stage_uses_collect_profile_for_single_handle(monkeypatch):
    """Single-handle: coordinator should NOT call batch.batch_scrape."""
    from collector import coordinator
    called = {"collect_profile": 0, "batch": False}

    async def fake_collect_profile(handle, headful, limit, fresh, cf):
        called["collect_profile"] += 1
        return ([{"author_username": handle, "tweet_id": "x"}], [])

    async def fake_batch_scrape(handles, cohort_name="batch"):
        called["batch"] = True
        return (0, 0)

    import batch as batch_module
    import collect as collect_module
    monkeypatch.setattr(batch_module, "batch_scrape", fake_batch_scrape)
    monkeypatch.setattr(collect_module, "collect_profile", fake_collect_profile)
    monkeypatch.setattr(collect_module, "get_next_cookie", lambda: "/tmp/fake.json")
    monkeypatch.setattr(coordinator, "ss_hb", lambda *a, **k: None)

    result = coordinator.default_collect_stage(
        {"handles": ["solo"], "label": "@solo"},
        master_sid=1,
        limit=50,
    )
    assert called["batch"] is False
    assert called["collect_profile"] == 1
    assert result == 1
