#!/usr/bin/env python3
"""Import collected tweets JSON into Postgres"""

import sys, json, os

sys.path.insert(0, os.path.expanduser("~/services/vibechecx/db"))
from db import insert_tweet, insert_media, insert_observation, start_scrape_session, end_scrape_session, upsert_account

RAW_DIR = os.path.expanduser("~/services/vibechecx/raw")

def import_json(filepath):
    with open(filepath) as f:
        tweets = json.load(f)

    print(f"Importing {len(tweets)} tweets from {os.path.basename(filepath)}...")

    session_id = start_scrape_session("solgab")
    imported = 0

    for t in tweets:
        tid = insert_tweet(t)
        if tid:
            imported += 1
            # Log observation: this tweet was seen on @solgab's profile
            insert_observation(tid, "solgab", context="profile_scrape")
            if t.get("media"):
                insert_media(tid, t["media"])

    end_scrape_session(session_id, tweets_count=imported)
    print(f"Imported {imported}/{len(tweets)} tweets into DB")
    return imported

if __name__ == "__main__":
    # Find latest collected file
    files = sorted([f for f in os.listdir(RAW_DIR) if f.startswith("collected_") and f.endswith(".json")])
    if not files:
        print("No collected JSON files found. Run the collector first.")
        sys.exit(1)

    latest = os.path.join(RAW_DIR, files[-1])
    import_json(latest)
