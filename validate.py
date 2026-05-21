#!/usr/bin/env python3
"""Compare DB tweets against CSV export"""
import csv, os, sys
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vibechecx_config import db_dsn

conn = psycopg2.connect(db_dsn())
cur = conn.cursor()
cur.execute("""
    SELECT t.tweet_id, t.likes, t.views, t.replies
    FROM tweets t JOIN accounts a ON t.author_account_id = a.id
    WHERE a.username = 'SolGab' AND t.is_retweet = false
""")
db_tweets = dict((str(r[0]), {"likes": r[1], "views": r[2], "replies": r[3]}) for r in cur.fetchall())
conn.close()

with open("/home/boto/services/vibechecx/validation/account_analytics_content_2026-04-19_2026-05-16.csv") as f:
    csv_tweets = dict((r["Post id"].strip(), {"likes": int(r["Likes"]), "replies": int(r["Replies"])}) for r in csv.DictReader(f))

matched = sum(1 for tid in csv_tweets if tid in db_tweets)
mismatches = []
for tid in csv_tweets:
    if tid in db_tweets:
        d, c = db_tweets[tid], csv_tweets[tid]
        likes_ok = abs(d["likes"] - c["likes"]) <= 1
        if not likes_ok or d["replies"] != c["replies"]:
            mismatches.append((tid, d["likes"], c["likes"], d["replies"], c["replies"]))

db_only = [t for t in db_tweets if t not in csv_tweets]
csv_only = [t for t in csv_tweets if t not in db_tweets]

print(f"CSV posts (Apr 19 - May 16): {len(csv_tweets)}")
print(f"DB @SolGab originals:        {len(db_tweets)}")
print(f"Overlapping tweets:           {matched}")
print(f"DB-only (pre-Apr 19):         {len(db_only)}")
print(f"CSV-only (not yet scraped):   {len(csv_only)}")
print()

if mismatches:
    print(f"MISMATCHES FOUND ({len(mismatches)}):")
    for tid, dl, cl, dr, cr in mismatches[:10]:
        print(f"  {tid}: likes DB={dl} CSV={cl} | replies DB={dr} CSV={cr}")
else:
    print("ZERO mismatches on likes and replies for all overlapping tweets!")

print()
print(f"DB-only sample: {db_only[:3]}...")
print(f"CSV-only sample: {list(csv_only)[:3]}...")
