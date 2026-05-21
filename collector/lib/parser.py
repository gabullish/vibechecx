"""X GraphQL response parser.

Extracts tweet/author/conversation data from the JSON bodies that X serves
through `x.com/i/api/graphql/...`. Used by every scraper module — Playwright
interception (collect, batch, replyminer, search_collect) AND direct API
(patrol --fast, xapi).

All exceptions are caught and logged; one malformed entry never breaks the
whole page (X periodically ships scalar fields where dicts used to be —
seen in `original_info` for older media).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("vibechecx.parser")


# ── public helpers ─────────────────────────────────────────────────────


def extract_tweets_from_graphql(data: dict) -> list[dict]:
    """Parse a GraphQL response body and return a list of normalised tweet dicts.

    Handles every shape we've seen: UserTweets, UserTweetsAndReplies,
    SearchTimeline, list timelines, and single-tweet TweetDetail bodies.
    """
    tweets: list[dict] = []
    try:
        entry_paths = [
            ["data", "user", "result", "timeline", "timeline", "instructions"],
            ["data", "user", "result", "timeline_v2", "timeline", "instructions"],
            ["data", "list", "tweets_timeline", "timeline", "instructions"],
            ["data", "search_by_raw_query", "search_timeline", "timeline", "instructions"],
        ]
        entries = None
        for path in entry_paths:
            curr = data
            for key in path:
                if isinstance(curr, dict):
                    curr = curr.get(key)
                else:
                    curr = None
                    break
            if curr and isinstance(curr, list):
                entries = curr
                break

        if not entries:
            for path in [
                ["data", "threaded_conversation_with_injections_v2", "instructions"],
                ["data", "tweetResult", "result"],
            ]:
                curr = data
                for key in path:
                    if isinstance(curr, dict):
                        curr = curr.get(key)
                    else:
                        curr = None
                        break
                if curr:
                    return extract_single_tweet(curr) or []
            return tweets

        for instruction in entries:
            if not isinstance(instruction, dict):
                continue
            for entry in instruction.get("entries", []):
                if not isinstance(entry, dict):
                    continue
                eid = entry.get("entryId", "")
                if not any(p in eid for p in ["tweet-", "profile-conversation-", "profile-grid-"]):
                    continue
                try:
                    results = _extract_tweet_results(entry)
                except Exception:
                    logger.warning("entry-extract failed; skipping one entry",
                                   exc_info=True)
                    continue
                for result in results:
                    try:
                        tweet = parse_tweet_result(result)
                    except Exception:
                        logger.warning("parse_tweet_result failed for one tweet; skipping",
                                       exc_info=True)
                        continue
                    if tweet:
                        tweets.append(tweet)
    except Exception:
        logger.exception("extract error")
    return tweets


def parse_conversation_response(body: dict) -> dict[str, dict]:
    """Extract tweets from a TweetDetail (reply thread) response.

    Returns {tweet_id: tweet_dict}. Used by the reply miner — preserves
    the dedup-by-id behaviour the original implementation relied on.
    """
    results: dict[str, dict] = {}
    try:
        instructions = body["data"]["threaded_conversation_with_injections_v2"]["instructions"]
    except (KeyError, TypeError, AttributeError):
        return results
    for instruction in instructions:
        for entry in instruction.get("entries", []):
            try:
                content = entry["content"]
                et = content.get("entryType")
                if et == "TimelineTimelineItem":
                    ic = content.get("itemContent", {})
                    if ic.get("itemType") == "TimelineTweet":
                        tr = ic.get("tweet_results", {}).get("result", {})
                        t = parse_tweet_result(tr)
                        if t:
                            results[t["tweet_id"]] = t
                elif et == "TimelineTimelineModule":
                    for item in content.get("items", []):
                        inner = item.get("item", item.get("entry", {}))
                        # Modern X (2025+) puts itemContent directly on `inner`.
                        # Older shape nested it under `content.itemContent`.
                        # Try the modern shape first, then fall back. Without
                        # this dual lookup, all conversationthread-* entries
                        # (which is how X groups reply trees) were silently
                        # dropped — replyminer stored 0 replies per tweet.
                        ic = (inner.get("itemContent")
                              or inner.get("content", {}).get("itemContent", {}))
                        if ic.get("itemType") == "TimelineTweet":
                            tr = ic.get("tweet_results", {}).get("result", {})
                            t = parse_tweet_result(tr)
                            if t:
                                results[t["tweet_id"]] = t
            except (KeyError, TypeError, AttributeError):
                continue
    return results


def parse_tweet_result(result: dict | None) -> dict | None:
    """Convert one tweet result dict (the GraphQL `result` shape) to our
    normalised tweet record. Returns None for unavailable/empty results."""
    if not result or not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetUnavailable":
        return None
    legacy = result.get("legacy", {})
    if not legacy:
        return None
    tweet_id = str(result.get("rest_id", legacy.get("id_str", "")))
    if not tweet_id:
        return None

    tweet_type = "original"
    retweeted_status = legacy.get("retweeted_status_result", {})
    is_retweet = bool(retweeted_status and retweeted_status.get("result"))
    is_quote = bool(legacy.get("is_quote_status", False))
    in_reply_to = legacy.get("in_reply_to_status_id_str")
    is_reply = bool(in_reply_to)
    if is_retweet and isinstance(retweeted_status, dict):
        rt_result = retweeted_status.get("result", {})
        real_author = extract_author(rt_result)
        tweet_type = "retweet"
    else:
        real_author = extract_author(result)
        if is_quote:
            tweet_type = "quote"
        elif is_reply:
            tweet_type = "reply"

    likes = legacy.get("favorite_count", 0)
    retweets = legacy.get("retweet_count", 0)
    reply_count = legacy.get("reply_count", 0)
    quote_count = legacy.get("quote_count", 0)
    bookmark_count = legacy.get("bookmark_count", 0)
    views_obj = result.get("views", {})
    views = views_obj.get("count", 0) if isinstance(views_obj, dict) else 0

    def _d(v):
        """Coerce a value to dict — X occasionally ships scalar where a dict
        was historically returned (e.g. `original_info: "<url>"`)."""
        return v if isinstance(v, dict) else {}

    media_entities = []
    ext = _d(legacy.get("extended_entities"))
    for m in (ext.get("media") or []):
        if not isinstance(m, dict):
            continue
        oi = _d(m.get("original_info"))
        alt = _d(m.get("ext_alt_text"))
        media_item = {
            "type": (m.get("type") or "").lower(),
            "url": m.get("media_url_https", ""),
            "alt_text": alt.get("string_val", ""),
            "width": oi.get("width", 0),
            "height": oi.get("height", 0),
        }
        if m.get("type") in ("video", "animated_gif"):
            vi = _d(m.get("video_info"))
            variants = vi.get("variants") or []
            variants = [v for v in variants if isinstance(v, dict)]
            if variants:
                best = max(
                    (v for v in variants if v.get("bitrate")),
                    key=lambda x: x["bitrate"], default=variants[0],
                )
                media_item["video_url"] = best.get("url", variants[0].get("url", ""))
            media_item["duration"] = (vi.get("duration_millis", 0) or 0) / 1000
        media_entities.append(media_item)

    return {
        "tweet_id": tweet_id,
        "tweet_type": tweet_type,
        "author_username":  real_author.get("screen_name", ""),
        "author_display":   real_author.get("name", ""),
        "author_avatar":    real_author.get("avatar_url", ""),
        "author_followers": real_author.get("followers_count", 0),
        "author_following": real_author.get("following_count", 0),
        "author_tweets":    real_author.get("tweets_count", 0),
        "content":    legacy.get("full_text", ""),
        "created_at": legacy.get("created_at", ""),
        "lang":       legacy.get("lang", ""),
        "is_reply":   is_reply,
        "reply_to_tweet_id": in_reply_to or "",
        "reply_to_username": legacy.get("in_reply_to_screen_name", ""),
        "is_quote":   is_quote,
        "is_retweet": is_retweet,
        "likes":      likes,
        "retweets":   retweets,
        "replies":    reply_count,
        "quotes":     quote_count,
        "bookmarks":  bookmark_count,
        "views":      views,
        "media":      media_entities,
    }


def extract_author(result: dict) -> dict:
    core = result.get("core", {})
    user_results = core.get("user_results", {})
    user_result = user_results.get("result", {})
    user_legacy = user_result.get("legacy", {})
    user_core = user_result.get("core", {})
    avatar = ""
    avatar_obj = user_result.get("avatar", {})
    if isinstance(avatar_obj, dict):
        avatar = avatar_obj.get("image_url",
                                user_legacy.get("profile_image_url_https", ""))
    return {
        "screen_name": (
            user_core.get("screen_name", user_legacy.get("screen_name", "")) or ""
        ).lower(),
        "name":   user_core.get("name", user_legacy.get("name", "")),
        "avatar_url": avatar,
        "followers_count": user_legacy.get("followers_count", 0),
        "following_count": user_legacy.get("friends_count", 0),
        "tweets_count":    user_legacy.get("statuses_count", 0),
    }


def extract_single_tweet(result: dict) -> list[dict]:
    tweet = parse_tweet_result(result)
    return [tweet] if tweet else []


# ── internal helpers ───────────────────────────────────────────────────


def _extract_tweet_results(entry: dict) -> list[dict]:
    content = entry.get("content", {})
    et = content.get("entryType")
    results: list[dict] = []
    if et == "TimelineTimelineItem":
        ic = content.get("itemContent", {})
        if ic.get("itemType") == "TimelineTweet":
            tr = ic.get("tweet_results", {}).get("result", {})
            if tr:
                results.append(tr)
    elif et == "TimelineTimelineModule":
        for item in content.get("items", []):
            inner = item.get("item", item.get("entry", {}))
            ic = inner.get("itemContent", inner.get("content", {}).get("itemContent", {}))
            if ic.get("itemType") == "TimelineTweet":
                tr = ic.get("tweet_results", {}).get("result", {})
                if tr:
                    results.append(tr)
    return results
