#!/usr/bin/env python3
"""Clean non-SolGab tweets from DB"""
import psycopg2, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import db_dsn
conn = psycopg2.connect(db_dsn())
cur = conn.cursor()

# Get SolGab account id
cur.execute("SELECT id FROM accounts WHERE username = 'SolGab'")
r = cur.fetchone()
if not r:
    print("SolGab account not found")
    exit(1)
solgab_id = r[0]

# Find all non-SolGab tweets that are referred to by SolGab replies
# We need to keep those (parent tweets)
cur.execute("""
    SELECT DISTINCT reply_to_tweet_id FROM tweets
    WHERE author_account_id = %s AND reply_to_tweet_id IS NOT NULL
""", (solgab_id,))
referred = [r[0] for r in cur.fetchall() if r[0]]

print(f"SolGab ID: {solgab_id}")
print(f"SolGab replies reference {len(referred)} parent tweets")

# Clear reply_to for those that aren't the referrred parents
cur.execute("""
    UPDATE tweets SET reply_to_tweet_id = NULL, reply_to_account_id = NULL
    WHERE author_account_id != %s
      AND tweet_id NOT IN (SELECT reply_to_tweet_id FROM tweets WHERE author_account_id = %s AND reply_to_tweet_id IS NOT NULL)
""", (solgab_id, solgab_id))
print(f"Cleared reply_to for non-SolGab tweets")

# Now delete non-SolGab tweets (excluding parent tweets SolGab replied to)
cur.execute("""
    DELETE FROM tweets
    WHERE author_account_id != %s
      AND tweet_id NOT IN (SELECT reply_to_tweet_id FROM tweets WHERE author_account_id = %s AND reply_to_tweet_id IS NOT NULL)
""", (solgab_id, solgab_id))
deleted = cur.rowcount
print(f"Deleted {deleted} non-SolGab tweets")

# Also delete orphaned accounts
cur.execute("DELETE FROM accounts WHERE id NOT IN (SELECT author_account_id FROM tweets) AND username != 'SolGab'")
print(f"Cleaned up orphaned accounts")

# Final count
cur.execute("SELECT count(*) FROM tweets")
total = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM tweets WHERE is_reply = true AND author_account_id = %s", (solgab_id,))
replies = cur.fetchone()[0]
conn.commit()
conn.close()
print(f"\nRemaining: {total} tweets ({replies} @SolGab replies)")
