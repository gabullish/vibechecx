"""Enrichment: DeepSeek primary, Grok fallback, scope-aware, honest failure."""
import json
import pytest

from enrich import (
    LLMRouter, _parse_response, get_unprocessed_tweets,
    update_tweet_in_db, run_enrich_for_scope,
)


class FakeProvider:
    def __init__(self, name, responses):
        self.name = name
        self.model = "fake"
        self._responses = list(responses)
        self.calls = 0

    def call(self, prompt):
        self.calls += 1
        if not self._responses:
            raise RuntimeError("exhausted")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


_OK = json.dumps({
    "sentiment": "positive", "content_type": "single",
    "tags": ["solana", "test"], "category": "community",
    "quality_score": 80, "inorganic_score": 0.1,
})


def test_parse_response_strips_codefence():
    text = "```json\n" + _OK + "\n```"
    parsed = _parse_response(text)
    assert parsed["sentiment"] == "positive"


def test_router_uses_primary_on_success():
    p = FakeProvider("ds", [_OK])
    s = FakeProvider("grok", [])
    router = LLMRouter(p, s)
    out, provider = router.analyze("hello")
    assert out["sentiment"] == "positive"
    assert provider == "ds"
    assert p.calls == 1 and s.calls == 0


def test_router_falls_back_after_two_failures():
    p = FakeProvider("ds", [RuntimeError("down"), RuntimeError("down"), RuntimeError("still down")])
    s = FakeProvider("grok", [_OK])
    router = LLMRouter(p, s)
    out, provider = router.analyze("hello")
    assert out is not None
    assert provider == "grok"
    # primary tried twice (one analyze() call = 2 attempts), then switch happened
    assert p.calls >= 2
    assert s.calls >= 1


def test_router_returns_none_when_both_fail():
    p = FakeProvider("ds", [RuntimeError("down"), RuntimeError("down")])
    s = FakeProvider("grok", [RuntimeError("also down"), RuntimeError("also down"),
                              RuntimeError("still down")])
    router = LLMRouter(p, s)
    out, _ = router.analyze("hello")
    assert out is None


def test_router_handles_malformed_json_as_failure():
    p = FakeProvider("ds", ["not json", "still not json", "still bad"])
    s = FakeProvider("grok", [_OK])
    router = LLMRouter(p, s)
    out, provider = router.analyze("hello")
    assert out is not None
    assert provider == "grok"


def test_run_enrich_for_scope_writes_db(db, make_user, make_single_profile):
    u = make_user("alice", "pw12345")
    pid = make_single_profile(u["id"], handle="alpha")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE username='alpha'")
        aid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO tweets(tweet_id, author_account_id, created_at, content, "
            "is_retweet) VALUES (%s, %s, NOW(), %s, FALSE)",
            ("ttt_1", aid, "Solana is cool"),
        )
    db.commit()
    router = LLMRouter(FakeProvider("ds", [_OK]), None)
    res = run_enrich_for_scope(
        {"account_ids": [aid], "label": "@alpha"},
        limit=10, router=router, rate_sleep=0,
    )
    assert res["enriched"] == 1
    assert res["provider"] == "ds"
    with db.cursor() as cur:
        cur.execute("SELECT sentiment, category, tags FROM tweets WHERE tweet_id='ttt_1'")
        row = cur.fetchone()
    assert row["sentiment"] == 1.0
    assert row["category"] == "community"
    assert "solana" in row["tags"]


def test_enrich_skips_when_no_provider():
    res = run_enrich_for_scope(
        {"account_ids": [], "label": "x"},
        router=LLMRouter(None, None), rate_sleep=0,
    )
    assert res["skipped"] is True
    assert res["enriched"] == 0
