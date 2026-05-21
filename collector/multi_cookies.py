#!/usr/bin/env python3
"""VibeChecx — Capture cookies for multiple X scraper accounts

Opens 3 Chromium windows. Log into each account, close the window.
Cookies are saved to separate files for parallel scraping.
"""

import asyncio

COOKIE_FILES = [
    "/home/boto/services/vibechecx/cookies/main.json",
    "/home/boto/services/vibechecx/cookies/scraper1.json",
    "/home/boto/services/vibechecx/cookies/scraper2.json",
]

ACCOUNT_LABELS = [
    "Main scraper (logged in Firefox)",
    "Scraper account 2",
    "Scraper account 3",
]

async def capture():
    from playwright.async_api import async_playwright

    for idx, (label, path) in enumerate(zip(ACCOUNT_LABELS, COOKIE_FILES)):
        print(f"\n{'='*50}")
        print(f"Window {idx + 1}: {label}")
        print(f"{'='*50}")
        print("A browser window will open.")
        print("1. Log into X with this account")
        print("2. Wait a moment after login")
        print("3. Close the browser window")
        input("   Press Enter to open this window...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()
            await page.goto("https://x.com/login", wait_until="domcontentloaded")
            print("   Browser open — log in, then close it.\n")
            # Wait for the browser to close
            try:
                async with page.expect_event("close", timeout=0):
                    pass
            except:
                pass
            # Save cookies
            await context.storage_state(path=path)
            cookie_count = len(await context.cookies())
            print(f"   ✓ Saved {cookie_count} cookies to {path.split('/')[-1]}")
            await browser.close()

    print(f"\n{'='*50}")
    print("All cookies captured!")
    print(f"  {COOKIE_FILES[0].split('/')[-1]}")
    print(f"  {COOKIE_FILES[1].split('/')[-1]}")
    print(f"  {COOKIE_FILES[2].split('/')[-1]}")
    print("Ready for parallel scraping.")

if __name__ == "__main__":
    asyncio.run(capture())
