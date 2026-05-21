#!/usr/bin/env python3
import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state="/home/boto/services/vibechecx/cookies/main.json",
        )
        page = await ctx.new_page()

        urls_seen = []
        async def capture(response):
            url = response.url
            if "graphql" not in url:
                return
            name = url.split("/")[-1].split("?")[0]
            urls_seen.append(name)
            if "Affiliates" in url or "affiliate" in url.lower():
                try:
                    body = await response.json()
                    with open("/tmp/aff_data.json", "w") as f:
                        json.dump(body, f)
                    print(f"✓ Captured Affiliates: {name} ({len(str(body))}b)")
                except Exception as e:
                    print(f"✗ Failed: {name}: {e}")

        page.on("response", capture)
        await page.goto("https://x.com/solflare/affiliates", timeout=30000)
        await page.wait_for_timeout(5000)

        print(f"\nAll GraphQL endpoints seen ({len(urls_seen)}):")
        for u in set(urls_seen):
            print(f"  {u}")

        await browser.close()

asyncio.run(main())
