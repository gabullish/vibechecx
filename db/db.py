#!/usr/bin/env python3
"""VibeChecx DB helper — insert/query functions for all tables"""

import os, sys, json, psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG  # noqa: E402

def get_conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)

def upsert_account(username, display_name="", avatar_url="", bio="",
                   followers_count=0, following_count=0):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO accounts (username, display_name, avatar_url, bio,
                                      followers_count, following_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    avatar_url = COALESCE(EXCLUDED.avatar_url, accounts.avatar_url),
                    bio = COALESCE(EXCLUDED.bio, accounts.bio),
                    followers_count = EXCLUDED.followers_count,
                    following_count = EXCLUDED.following_count,
                    last_updated_at = NOW()
                RETURNING id
            """, (username, display_name, avatar_url, bio, followers_count, following_count))
            return cur.fetchone()["id"]
    finally:
        conn.commit()
        conn.close()

def insert_tweet(tweet_data):
    """Insert a single tweet. Returns tweet_id or None if duplicate."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Get or create author account
            author_id = upsert_account(
                username=tweet_data["author_username"],
                display_name=tweet_data.get("author_display", ""),
                avatar_url=tweet_data.get("author_avatar", ""),
            )

            # Parse created_at from X format
            created_at = tweet_data.get("created_at", "")

            cur.execute("""
                INSERT INTO tweets (
                    tweet_id, author_account_id, created_at, content, lang,
                    is_reply, reply_to_tweet_id, reply_to_account_id,
                    is_quote, is_retweet,
                    likes, retweets, replies, quotes, bookmarks, views,
                    scrape_source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tweet_id) DO UPDATE SET
                    likes = EXCLUDED.likes,
                    retweets = EXCLUDED.retweets,
                    replies = EXCLUDED.replies,
                    quotes = EXCLUDED.quotes,
                    bookmarks = EXCLUDED.bookmarks,
                    views = GREATEST(EXCLUDED.views, tweets.views),
                    last_measured_at = NOW()
                RETURNING tweet_id
            """, (
                tweet_data["tweet_id"], author_id, created_at, tweet_data["content"],
                tweet_data.get("lang"),
                tweet_data.get("is_reply", False),
                tweet_data.get("reply_to_tweet_id") or None,
                None,  # reply_to_account_id — resolved later
                tweet_data.get("is_quote", False),
                tweet_data.get("is_retweet", False),
                tweet_data.get("likes", 0), tweet_data.get("retweets", 0),
                tweet_data.get("replies", 0), tweet_data.get("quotes", 0),
                tweet_data.get("bookmarks", 0), tweet_data.get("views", 0),
                tweet_data.get("scrape_source", "graphql"),
            ))
            return cur.fetchone()["tweet_id"]
    except Exception as e:
        print(f"  [DB insert_tweet error] {e}")
        return None
    finally:
        conn.commit()
        conn.close()

def insert_observation(tweet_id, observer_username, context="profile_scrape"):
    """Log that a tweet was observed on a profile"""
    conn = get_conn()
    try:
        observer_id = upsert_account(username=observer_username)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tweet_observations (tweet_id, observer_account_id, context)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (tweet_id, observer_id, context))
    finally:
        conn.commit()
        conn.close()

def insert_media(tweet_id, media_list):
    """Insert media entries for a tweet"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for m in media_list:
                cur.execute("""
                    INSERT INTO media (tweet_id, media_type, url, alt_text, width, height,
                                       duration_seconds, view_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    tweet_id, m.get("type", "photo"),
                    m.get("video_url", m.get("url", "")),
                    m.get("alt_text", ""),
                    m.get("width", 0), m.get("height", 0),
                    m.get("duration", 0), m.get("view_count", 0),
                ))
    finally:
        conn.commit()
        conn.close()

def start_scrape_session(target_username, session_type="profile_scrape"):
    """Create a scrape session entry"""
    conn = get_conn()
    try:
        target_id = upsert_account(username=target_username)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scrape_sessions (target_account_id, session_type)
                VALUES (%s, %s) RETURNING id
            """, (target_id, session_type))
            return cur.fetchone()["id"]
    finally:
        conn.commit()
        conn.close()

def end_scrape_session(session_id, tweets_count=0, status="completed", error=""):
    """Mark scrape session as complete"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scrape_sessions SET
                    ended_at = NOW(), tweets_collected = %s, status = %s, error_log = %s
                WHERE id = %s
            """, (tweets_count, status, error, session_id))
    finally:
        conn.commit()
        conn.close()
