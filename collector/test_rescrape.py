#!/usr/bin/env python3
"""Test optimized re-scrape — should stop after hitting known tweets"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/services/vibechecx/collector"))
import asyncio, time
from collect import collect_profile

async def run():
    start = time.time()
    tweets, gql = await collect_profile("solgab", headful=False, limit=0, fresh=False)
    elapsed = time.time() - start
    print(f"\nRe-scrape: {len(tweets)} new tweets from {len(gql)} GraphQL responses in {elapsed:.1f}s")

if __name__ == "__main__":
    asyncio.run(run())
