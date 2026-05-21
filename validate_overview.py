#!/usr/bin/env python3
"""Compare scraped daily aggregates against X native overview CSV"""
import csv, os, sys, psycopg2
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vibechecx_config import db_dsn

conn = psycopg2.connect(db_dsn())
cur = conn.cursor()

cur.execute("""
    SELECT created_at::date as day,
           count(*) as posts,
           COALESCE(sum(likes), 0) as likes,
           COALESCE(sum(views), 0) as views,
           COALESCE(sum(replies), 0) as replies
    FROM tweets WHERE author_account_id = (SELECT id FROM accounts WHERE username = 'SolGab')
    AND created_at >= '2026-04-19' AND created_at <= '2026-05-16'
    GROUP BY day ORDER BY day
""")
db_by_day = {}
for r in cur.fetchall():
    day = str(r[0])
    db_by_day[day] = {"posts": r[1], "likes_sum": r[2], "views": r[3], "replies_sum": r[4]}
conn.close()

csv_by_day = {}
with open("/home/boto/services/vibechecx/validation/account_overview_analytics.csv") as f:
    for row in csv.DictReader(f):
        day = row["Date"].strip()
        try:
            dt = datetime.strptime(day, "%a, %b %d, %Y")
            day_key = dt.strftime("%Y-%m-%d")
            csv_by_day[day_key] = {
                "impressions": int(row["Impressions"]),
                "likes": int(row["Likes"]),
                "replies": int(row["Replies"]),
            }
        except:
            pass

total_db_likes = total_csv_likes = total_db_posts = total_db_replies = total_csv_replies = 0
total_csv_impressions = total_db_views = 0
matches = mismatches = 0

print(f"{'Day':<14} {'CSV likes':>9} {'DB likes':>9} {'CSV repl':>8} {'DB repl':>8} {'DB posts':>8}")
print("-" * 60)

for day in sorted(set(list(db_by_day.keys()) + list(csv_by_day.keys()))):
    csv_d = csv_by_day.get(day, {})
    db_d = db_by_day.get(day, {})
    c_likes = csv_d.get("likes", 0)
    d_likes = db_d.get("likes_sum", 0)
    c_replies = csv_d.get("replies", 0)
    d_replies = db_d.get("replies_sum", 0)
    d_posts = db_d.get("posts", 0)

    total_csv_likes += c_likes
    total_db_likes += d_likes
    total_csv_replies += c_replies
    total_db_replies += d_replies
    total_db_posts += d_posts
    total_csv_impressions += csv_d.get("impressions", 0)
    total_db_views += db_d.get("views", 0)

    diff = abs(c_likes - d_likes)
    if diff <= 2: matches += 1
    else: mismatches += 1

    flag = " <<<" if diff > 2 else ""
    print(f"{day:<14} {c_likes:>9} {d_likes:>9} {c_replies:>8} {d_replies:>8} {d_posts:>8}{flag}")

print("-" * 60)
print(f"{'TOTAL':<14} {total_csv_likes:>9} {total_db_likes:>9} {total_csv_replies:>8} {total_db_replies:>8} {total_db_posts:>8}")
print(f"\nCSV impressions (total): {total_csv_impressions}")
print(f"DB views (total):       {total_db_views}")
print(f"Ratio (DB views/CSV imp): {total_db_views/total_csv_impressions*100:.1f}%" if total_csv_impressions else "")
print(f"\nDays matched (likes): {matches}/{matches+mismatches}")
print(f"Match rate: {matches/(matches+mismatches)*100:.1f}%")
