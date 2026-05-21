#!/usr/bin/env python3
"""Reprocess raw replies_*.json files with fixed parser and import to DB"""
import json, glob, sys, os, traceback

sys.path.insert(0, os.path.expanduser("~/services/vibechecx/db"))
from db import insert_tweet, insert_media, insert_observation, start_scrape_session, end_scrape_session

def extract_results(entry):
    content = entry.get("content", {})
    et = content.get("entryType")
    results = []
    if et == "TimelineTimelineItem":
        ic = content.get("itemContent", {})
        if ic.get("itemType") == "TimelineTweet":
            tr = ic.get("tweet_results", {}).get("result", {})
            if tr: results.append(tr)
    elif et == "TimelineTimelineModule":
        for item in content.get("items", []):
            inner = item.get("item", item.get("entry", {}))
            ic = inner.get("itemContent", inner.get("content", {}).get("itemContent", {}))
            if ic.get("itemType") == "TimelineTweet":
                tr = ic.get("tweet_results", {}).get("result", {})
                if tr: results.append(tr)
    return results

def parse_tweet(r):
    if not r or not isinstance(r, dict): return None
    leg = r.get("legacy", {})
    if not leg: return None
    tid = str(r.get("rest_id", leg.get("id_str", "")))
    if not tid: return None
    in_reply = leg.get("in_reply_to_status_id_str")
    is_reply = bool(in_reply)
    is_retweet = bool(leg.get("retweeted_status_result", {}).get("result"))
    if is_retweet:
        rt = leg["retweeted_status_result"]["result"]
        uname = rt.get("core",{}).get("user_results",{}).get("result",{}).get("core",{}).get("screen_name","")
    else:
        uname = r.get("core",{}).get("user_results",{}).get("result",{}).get("core",{}).get("screen_name","")
    avatar = r.get("core",{}).get("user_results",{}).get("result",{}).get("legacy",{}).get("profile_image_url_https","")
    views_obj = r.get("views", {})
    views = views_obj.get("count", 0) if isinstance(views_obj, dict) else 0
    media = []
    for m in leg.get("extended_entities",{}).get("media",[]):
        entry = {"type": m.get("type","").lower(), "url": m.get("media_url_https","")}
        if m.get("type") in ("video","animated_gif"):
            variants = m.get("video_info",{}).get("variants",[])
            if variants:
                best = max((v for v in variants if v.get("bitrate")), key=lambda x: x["bitrate"], default=variants[0])
                entry["video_url"] = best.get("url", variants[0].get("url",""))
        media.append(entry)
    return {
        "tweet_id": tid, "tweet_type": "reply" if is_reply else ("retweet" if is_retweet else "original"),
        "author_username": uname, "author_display": uname, "author_avatar": avatar,
        "content": leg.get("full_text",""), "created_at": leg.get("created_at",""), "lang": leg.get("lang",""),
        "is_reply": is_reply, "reply_to_tweet_id": in_reply or "", "reply_to_username": leg.get("in_reply_to_screen_name",""),
        "is_quote": bool(leg.get("quoted_status_result",{})), "is_retweet": is_retweet,
        "likes": leg.get("favorite_count",0), "retweets": leg.get("retweet_count",0),
        "replies": leg.get("reply_count",0), "bookmarks": leg.get("bookmark_count",0), "views": views,
        "media": media, "scrape_source": "graphql",
    }

all_tweets = {}
try:
    for f in sorted(glob.glob(os.path.expanduser("~/services/vibechecx/raw/replies_*.json"))):
        d = json.load(open(f))
        instrs = d["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
        ii = None
        for instr in instrs:
            if instr.get("type") == "TimelineAddEntries":
                ii = instr
                break
        if ii is None:
            print(f"  Skipping {os.path.basename(f)}: no TimelineAddEntries")
            continue
        for entry in ii.get("entries", []):
            for r in extract_results(entry):
                t = parse_tweet(r)
                if t and t["tweet_id"] not in all_tweets:
                    all_tweets[t["tweet_id"]] = t
except Exception as e:
    print(f"ERROR: {e}")
    traceback.print_exc()
    sys.exit(1)

solgab = [t for t in all_tweets.values() if t["author_username"] == "SolGab"]
replies = [t for t in solgab if t["is_reply"]]

print(f"TOTAL: {len(all_tweets)} tweets")
print(f"SolGab: {len(solgab)}")
print(f"SolGab replies: {len(replies)}")
for t in replies[:10]:
    print(f"  reply->{t['reply_to_tweet_id'][:15]}: {t['content'][:50]} (likes={t['likes']})")

# Import ALL tweets (parents first, then SolGab's replies)
print(f"\nImporting all {len(all_tweets)} tweets to DB...")
sid = start_scrape_session("solgab", "with_replies_import")
imported = 0
for t in all_tweets.values():
    # Clear reply_to for non-SolGab tweets so parents can be inserted first
    if t["author_username"] != "SolGab":
        t["is_reply"] = False
        t["reply_to_tweet_id"] = ""
    tid = insert_tweet(t)
    if tid:
        imported += 1
        insert_observation(tid, "solgab", "with_replies")
        if t.get("media"):
            insert_media(tid, t["media"])

# Second pass: update reply references for SolGab replies
reimported = 0
for t in replies:
    if t["is_reply"]:
        # Update the reply_to reference
        tid = insert_tweet(t)
        if tid:
            reimported += 1

end_scrape_session(sid, tweets_count=imported)
print(f"Imported {imported} tweets total (+ {reimported} reply refs updated)")
