"""parse_tweet_result handles realistic shapes from collected/ raw fixtures."""
import json
import os
import glob
import pytest

from collector.collect import parse_tweet_result, extract_tweets_from_graphql

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "raw")


def _find_raw_fixture():
    """Pick any existing GraphQL response JSON from raw/."""
    matches = sorted(glob.glob(os.path.join(RAW_DIR, "UserTweets_*.json")))
    if not matches:
        matches = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    return matches[-1] if matches else None


def test_parse_handles_empty_or_garbage():
    assert parse_tweet_result(None) is None
    assert parse_tweet_result({}) is None
    assert parse_tweet_result({"__typename": "TweetUnavailable"}) is None
    assert parse_tweet_result({"rest_id": "1"}) is None  # no legacy


def test_parse_minimal_tweet():
    result = {
        "rest_id": "1234567",
        "core": {"user_results": {"result": {
            "core": {"screen_name": "alice", "name": "Alice"},
            "avatar": {"image_url": "http://x/a.jpg"},
            "legacy": {},
        }}},
        "legacy": {
            "full_text": "hello world",
            "created_at": "Mon Jan 01 12:00:00 +0000 2024",
            "favorite_count": 5,
            "retweet_count": 2,
            "reply_count": 1,
            "quote_count": 0,
            "bookmark_count": 0,
        },
    }
    t = parse_tweet_result(result)
    assert t["tweet_id"] == "1234567"
    assert t["author_username"] == "alice"
    assert t["content"] == "hello world"
    assert t["likes"] == 5
    assert t["tweet_type"] == "original"
    assert not t["is_reply"] and not t["is_retweet"] and not t["is_quote"]


def test_extract_from_real_fixture():
    fx = _find_raw_fixture()
    if not fx:
        pytest.skip("no raw GraphQL fixtures available")
    with open(fx) as f:
        body = json.load(f)
    tweets = extract_tweets_from_graphql(body)
    # Parser should not crash on real data even if no tweets are found.
    assert isinstance(tweets, list)
    for t in tweets:
        assert "tweet_id" in t
        assert "author_username" in t
