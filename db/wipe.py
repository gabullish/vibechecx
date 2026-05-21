#!/usr/bin/env python3
"""Delete non-SolGab tweets from DB, keeping parent tweets that SolGab replied to"""

import os, sys, psycopg2
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import db_dsn
conn = psycopg2.connect(db_dsn())
cur = conn.cursor()

cur.execute("SELECT id FROM accounts WHERE username = 'SolGab'")
sid = cur.fetchone()[0]

# Parent tweets that SolGab replied to (must keep for FK integrity)
cur.execute("SELECT DISTINCT reply_to_tweet_id FROM tweets WHERE author_account_id = %s AND reply_to_tweet_id IS NOT NULL", (sid,))
to_keep = set(r[0] for r in cur.fetchall())
print(f"Keeping {len(to_keep)} parent tweets")

# Non-SolGab tweets to delete (excluding parents)
cur.execute("SELECT tweet_id FROM tweets WHERE author_account_id != %s", (sid,))
all_non_solgab = [r[0] for r in cur.fetchall()]
to_delete = [t for t in all_non_solgab if t not in to_keep]
print(f"Deleting {len(to_delete)} non-SolGab tweets ({len(to_keep)} parents kept)")

# Clean up
for tid in to_delete:
    cur.execute("DELETE FROM tweet_observations WHERE tweet_id = %s", (tid,))
    cur.execute("DELETE FROM media WHERE tweet_id = %s", (tid,))
    cur.execute("DELETE FROM tweets WHERE tweet_id = %s", (tid,))

# Delete orphan accounts
cur.execute("DELETE FROM accounts WHERE id NOT IN (SELECT author_account_id FROM tweets) AND id != %s", (sid,))

cur.execute("SELECT count(*) FROM tweets")
total = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM tweets WHERE author_account_id = %s AND is_reply = true", (sid,))
replies = cur.fetchone()[0]
conn.commit()
conn.close()
print(f"Done: {total} tweets remaining ({replies} @SolGab replies)")
