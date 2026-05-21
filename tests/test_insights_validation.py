"""Insights validation: allow-list, retry, hallucination warning banner."""
import json
import pytest


class FakeProvider:
    """LLM stub returning pre-baked responses, one per call()."""
    def __init__(self, name, responses):
        self.name = name
        self.model = "fake"
        self._responses = list(responses)
        self.calls = 0
        self.last_prompt = None

    def call(self, prompt, **kwargs):
        self.calls += 1
        self.last_prompt = prompt
        if not self._responses:
            raise RuntimeError("FakeProvider exhausted")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _valid_response(handle_pool, tweet_pool):
    return json.dumps({
        "period_summary": "Cohort had a strong week.",
        "top_topics": [{"topic": "solana", "mentions": 5, "avg_sentiment": 0.4}],
        "top_performers": [{"tweet_id": next(iter(tweet_pool)), "content_preview": "hi", "why": "best"}],
        "whats_working": ["consistent posting"],
        "to_improve": ["more replies"],
        "kudos": [{"handle": "@" + next(iter(handle_pool)), "reason": "top engagement"}],
        "content_ideas": ["thread about X"],
    })


def _hallucinated_response():
    return json.dumps({
        "period_summary": "@ghost_handle did really well",
        "top_topics": [],
        "top_performers": [{"tweet_id": "9999999", "content_preview": "fake", "why": "made up"}],
        "whats_working": [],
        "to_improve": [],
        "kudos": [{"handle": "@nobody", "reason": "invented"}],
        "content_ideas": [],
    })


def test_allowlist_catches_hallucinated_handle():
    from web.vibechecx_insights import _scan_violations, _build_allowlist
    data = {
        "accounts": [{"username": "real_one"}],
        "top_tweets": [{"tweet_id": "12345"}],
    }
    allow_h, allow_t, external_h = _build_allowlist(data, "cohort")
    parsed = json.loads(_hallucinated_response())
    bad_h, bad_t = _scan_violations(parsed, allow_h, allow_t, external_h)
    assert "ghost_handle" in bad_h or "nobody" in bad_h or "@ghost_handle" in bad_h or "@nobody" in bad_h
    assert "9999999" in bad_t


def test_strip_removes_hallucinated_kudos_and_performers():
    from web.vibechecx_insights import _strip_violations
    parsed = json.loads(_hallucinated_response())
    cleaned = _strip_violations(parsed, {"ghost_handle", "nobody"}, {"9999999"})
    # kudos entries pointing at hallucinated handles are gone
    handles_left = [k.get("handle", "").lstrip("@") for k in cleaned.get("kudos", [])]
    assert "ghost_handle" not in handles_left and "nobody" not in handles_left
    # top_performers entries pointing at hallucinated tweet_ids are gone
    tids_left = [str(p.get("tweet_id")) for p in cleaned.get("top_performers", [])]
    assert "9999999" not in tids_left
    # prose @-mentions are now advisory (not censored) — handle stays in prose
    # but unknown handles surface via _warnings, not [removed] substitution
    summary = cleaned.get("period_summary", "")
    assert "[removed]" not in summary  # prose is no longer redacted


def test_generate_insights_retries_on_hallucination(db, make_user, monkeypatch):
    """First call hallucinates; retry returns clean output → return retry."""
    from web import vibechecx_insights as vi
    u = make_user("alice", "pw12345")
    # Seed cohort with a known account + tweet
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO cohorts(user_id, name, slug) VALUES(%s,%s,%s) RETURNING id",
            (u["id"], "x", f"x_{u['id']}"),
        )
        cid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO accounts(username) VALUES('real_one') RETURNING id"
        )
        aid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO cohort_members(cohort_id, account_id) VALUES(%s,%s)", (cid, aid),
        )
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, likes) "
            "VALUES('12345', %s, NOW(), 'hi', 100)", (aid,),
        )
    db.commit()

    fake = FakeProvider("grok", [
        _hallucinated_response(),
        _valid_response({"real_one"}, {"12345"}),
    ])
    monkeypatch.setattr(vi, "make_grok", lambda: fake)
    monkeypatch.setattr(vi, "make_deepseek", lambda: None)

    insights, provider = vi.generate_insights("cohort", cid, "30d")
    assert insights is not None
    assert provider == "grok", f"expected grok (retry succeeded), got {provider}"
    # The retry response is clean: kudos should NOT contain @nobody
    handles_in_kudos = [k.get("handle", "").lstrip("@") for k in insights.get("kudos", [])]
    assert "nobody" not in handles_in_kudos
    # Fake was called twice (once + retry)
    assert fake.calls == 2
    # The retry prompt must have included the strict-mode suffix.
    assert "STRICT RETRY" in fake.last_prompt


def test_generate_insights_persists_warnings_when_retry_also_bad(db, make_user, monkeypatch):
    from web import vibechecx_insights as vi
    u = make_user("bob", "pw12345")
    with db.cursor() as cur:
        cur.execute("INSERT INTO cohorts(user_id,name,slug) VALUES(%s,%s,%s) RETURNING id",
                    (u["id"], "y", f"y_{u['id']}"))
        cid = cur.fetchone()["id"]
        cur.execute("INSERT INTO accounts(username) VALUES('real_two') RETURNING id")
        aid = cur.fetchone()["id"]
        cur.execute("INSERT INTO cohort_members(cohort_id,account_id) VALUES(%s,%s)", (cid, aid))
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, likes) "
            "VALUES('77', %s, NOW(), 'hi', 5)", (aid,),
        )
    db.commit()

    fake = FakeProvider("grok", [
        _hallucinated_response(),
        _hallucinated_response(),  # retry still bad
    ])
    monkeypatch.setattr(vi, "make_grok", lambda: fake)
    monkeypatch.setattr(vi, "make_deepseek", lambda: None)

    insights, provider = vi.generate_insights("cohort", cid, "30d")
    assert insights is not None, "should still return cleaned insights, not None"
    assert "+warnings" in provider, f"provider should indicate warnings: {provider}"
    assert "_warnings" in insights
    w = insights["_warnings"]
    assert w.get("removed_handles") or w.get("removed_tweet_ids")
