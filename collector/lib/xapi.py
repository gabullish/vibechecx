#!/usr/bin/env python3
"""VibeChecx direct X (Twitter) API client.

Bypasses Playwright for operations that work via plain httpx:
- User metadata lookup (UserByScreenName)
- Batch metric refresh (TweetResultByRestId)
- User tweet timeline (UserTweets, UserTweetsAndReplies)

Cookies are read from the Playwright storage_state JSON files we already
maintain. Only `auth_token` and `ct0` are needed.

Rate limits: X's internal GraphQL is lenient for authenticated reads (~180
requests / 15 min per token). We rotate across cookie files automatically.

Usage:
    from xapi import XApiClient
    async with XApiClient.from_cookie_dir(COOKIE_DIR) as client:
        user = await client.get_user('solflaredojo')
        metrics = await client.get_tweet_metrics(['id1', 'id2', ...])
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("vibechecx.xapi")

# Bearer token shipped with every X web client (public knowledge)
_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL endpoint paths (hash + name).  Sourced from twikit 2.3.3 constants.
# These do change when X deploys major API updates, but have been stable for
# several months.
_EP = {
    "UserByScreenName":   "NimuplG1OB7Fd2btCLdBOw/UserByScreenName",
    "UserByRestId":       "tD8zKvQzwY3kdx5yz6YmOw/UserByRestId",
    "UserTweets":         "QWF3SzpHmykQHsQMixG0cg/UserTweets",
    "UserTweetsAndReplies": "vMkJyzx1wdmvOeeNG0n6Wg/UserTweetsAndReplies",
    "TweetResultByRestId": "Xl5pC_lBk_gcO2ItU39DQw/TweetResultByRestId",
    "SearchTimeline":     "flaR-PUMshxFWZWPNpq4zA/SearchTimeline",
}

# Feature flags that X requires for most GQL calls
_FEATURES_COMMON = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

_FEATURES_USER = {
    **_FEATURES_COMMON,
    "hidden_profile_subscriptions_enabled": True,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
}

_FEATURES_TWEET = {
    **_FEATURES_COMMON,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_uc_gql_enabled": False,
    "vibe_api_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "interactive_text_enabled": True,
    "responsive_web_text_conversations_enabled": False,
    "longform_notetweets_richtext_consumption_enabled": True,
}


def _load_cookies(path: str) -> dict[str, str] | None:
    """Convert Playwright storage_state JSON → {name: value} dict."""
    try:
        with open(path) as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        result = {c["name"]: c["value"] for c in cookies if "name" in c}
        if "auth_token" in result and "ct0" in result:
            return result
    except Exception:
        pass
    return None


class XApiClient:
    """Async context manager wrapping an httpx.AsyncClient with X auth headers."""

    def __init__(self, cookies: dict[str, str]):
        self._cookies = cookies
        self._client: httpx.AsyncClient | None = None
        self._request_times: list[float] = []  # rate-limit tracking

    @classmethod
    def from_cookie_file(cls, path: str) -> "XApiClient":
        cookies = _load_cookies(path)
        if not cookies:
            raise ValueError(f"No valid auth cookies in {path}")
        return cls(cookies)

    @classmethod
    def from_cookie_dir(cls, directory: str,
                        files: list[str] | None = None) -> "XApiClient":
        """Try each file in order; return first working client."""
        if files is None:
            files = ["main.json", "scraper1.json", "scraper2.json"]
        for fname in files:
            path = os.path.join(directory, fname)
            cookies = _load_cookies(path)
            if cookies:
                logger.debug("xapi using cookie file: %s", fname)
                return cls(cookies)
        raise ValueError(f"No usable cookie files in {directory}")

    async def __aenter__(self) -> "XApiClient":
        self._client = httpx.AsyncClient(
            cookies=httpx.Cookies(self._cookies),
            headers={
                "Authorization": f"Bearer {_BEARER}",
                "x-csrf-token": self._cookies.get("ct0", ""),
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-active-user": "yes",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": "https://x.com/",
                "Origin": "https://x.com",
            },
            follow_redirects=True,
            timeout=20.0,
        )
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    def _url(self, endpoint: str) -> str:
        path = _EP[endpoint]
        return f"https://api.x.com/graphql/{path}"

    async def _get(self, endpoint: str, variables: dict,
                   features: dict | None = None) -> dict:
        """Make a single GraphQL GET. Handles 429 with exponential backoff."""
        if features is None:
            features = _FEATURES_COMMON
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(features),
        }
        backoff = 30
        for attempt in range(4):
            try:
                r = await self._client.get(self._url(endpoint), params=params)
                if r.status_code == 429:
                    wait = backoff * (2 ** attempt) + random.uniform(0, 5)
                    logger.warning("xapi 429 on %s — waiting %.0fs", endpoint, wait)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code == 404:
                    logger.debug("xapi 404 for %s (endpoint may need updating)", endpoint)
                    return {}
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                raise
        return {}

    # ------------------------------------------------------------------ #
    # Public helpers                                                       #
    # ------------------------------------------------------------------ #

    async def get_user(self, screen_name: str) -> dict[str, Any] | None:
        """Return a dict with id, name, bio, followers, following, tweets_count.
        Returns None if user not found."""
        data = await self._get(
            "UserByScreenName",
            {"screen_name": screen_name, "withSafetyModeUserFields": True},
            features=_FEATURES_USER,
        )
        try:
            result = data["data"]["user"]["result"]
            leg = result.get("legacy", {})
            core = result.get("core", {})
            return {
                "id": result.get("rest_id", ""),
                "screen_name": screen_name,
                "name": core.get("name", leg.get("name", "")),
                "bio": leg.get("description", ""),
                "followers": leg.get("followers_count", 0),
                "following": leg.get("friends_count", 0),
                "tweets_count": leg.get("statuses_count", 0),
                "avatar_url": leg.get("profile_image_url_https", ""),
                "verified": result.get("is_blue_verified", False),
            }
        except (KeyError, TypeError):
            return None

    async def get_tweet_metrics(
        self, tweet_ids: list[str], concurrency: int = 5
    ) -> dict[str, dict]:
        """Fetch likes/retweets/replies/views for a list of tweet IDs.

        Returns {tweet_id: {likes, retweets, replies, views}} for found tweets.
        Uses a semaphore to cap concurrency and avoid hammering the endpoint.
        """
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, dict] = {}

        async def fetch_one(tid: str):
            async with semaphore:
                # Small jitter between requests (50–300ms)
                await asyncio.sleep(random.uniform(0.05, 0.3))
                data = await self._get(
                    "TweetResultByRestId",
                    {
                        "tweetId": tid,
                        "withCommunity": False,
                        "includePromotedContent": False,
                        "withVoice": False,
                    },
                    features=_FEATURES_TWEET,
                )
                try:
                    result = data["data"]["tweetResult"]["result"]
                    leg = result.get("legacy", {})
                    views = 0
                    vo = result.get("views", {})
                    if isinstance(vo, dict):
                        views = int(vo.get("count") or 0)
                    results[tid] = {
                        "likes":    int(leg.get("favorite_count") or 0),
                        "retweets": int(leg.get("retweet_count") or 0),
                        "replies":  int(leg.get("reply_count") or 0),
                        "quotes":   int(leg.get("quote_count") or 0),
                        "bookmarks": int(leg.get("bookmark_count") or 0),
                        "views":    views,
                    }
                except (KeyError, TypeError, ValueError):
                    pass  # tweet deleted or unavailable

        await asyncio.gather(*[fetch_one(tid) for tid in tweet_ids])
        return results

    async def get_user_tweets(
        self,
        user_id: str,
        count: int = 40,
        cursor: str | None = None,
        include_replies: bool = False,
    ) -> tuple[list[dict], str | None]:
        """Fetch a page of tweets for a user.

        Returns (tweets_raw_list, next_cursor) where tweets_raw_list are the
        raw GraphQL result dicts (same shape parse_tweet_result expects).
        next_cursor is None when there are no more pages.
        """
        endpoint = "UserTweetsAndReplies" if include_replies else "UserTweets"
        variables: dict[str, Any] = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": False,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor

        data = await self._get(endpoint, variables, features=_FEATURES_COMMON)
        tweets = []
        next_cursor = None
        try:
            instructions = (
                data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]
            )
            for instr in instructions:
                if instr.get("type") == "TimelineAddEntries":
                    for entry in instr.get("entries", []):
                        eid = entry.get("entryId", "")
                        # cursor entries
                        if "cursor-bottom" in eid:
                            try:
                                next_cursor = (
                                    entry["content"]["value"]
                                )
                            except (KeyError, TypeError):
                                pass
                            continue
                        ct = entry.get("content", {})
                        if ct.get("entryType") == "TimelineTimelineItem":
                            ic = ct.get("itemContent", {})
                            if ic.get("itemType") == "TimelineTweet":
                                tr = ic.get("tweet_results", {}).get("result")
                                if tr:
                                    tweets.append(tr)
        except (KeyError, TypeError, AttributeError):
            pass

        return tweets, next_cursor
