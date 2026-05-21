"""Test reply parser fix"""
import json, glob, sys
sys.path.insert(0, "/home/boto/services/vibechecx/collector")

def _extract_tweet_results(entry):
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

def parse_tweet_result(r):
    if not r or not isinstance(r, dict): return None
    if r.get("__typename") == "TweetUnavailable": return None
    leg = r.get("legacy", {})
    if not leg: return None
    tid = str(r.get("rest_id", leg.get("id_str", "")))
    if not tid: return None
    is_retweet = bool(leg.get("retweeted_status_result", {}).get("result"))
    in_reply = leg.get("in_reply_to_status_id_str")
    is_reply = bool(in_reply)
    if is_retweet:
        rt = leg["retweeted_status_result"]["result"]
        user_core = rt.get("core", {}).get("user_results", {}).get("result", {}).get("core", {})
    else:
        user_core = r.get("core", {}).get("user_results", {}).get("result", {}).get("core", {})
    uname = user_core.get("screen_name", "")
    likes = leg.get("favorite_count", 0)
    views = r.get("views", {}).get("count", 0) if isinstance(r.get("views"), dict) else 0
    return {"tweet_id": tid, "author_username": uname, "content": leg.get("full_text",""),
            "is_reply": is_reply, "reply_to_tweet_id": in_reply or "", "likes": likes, "views": views}

solgab_total = 0
solgab_replies = 0

for f in sorted(glob.glob("/home/boto/services/vibechecx/raw/replies_*.json")):
    d = json.load(open(f))
    instrs = d["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][2]
    for entry in instrs.get("entries", []):
        results = _extract_tweet_results(entry)
        for r in results:
            tweet = parse_tweet_result(r)
            if tweet and tweet["author_username"] == "SolGab":
                solgab_total += 1
                if tweet["is_reply"]:
                    solgab_replies += 1

print(f"Total: {solgab_total} | Replies: {solgab_replies}")
