#!/bin/bash
# VibeChecx — Cookie setup helper
# Run ONCE to save X session cookies for the Playwright collector.
#
# This opens a headed browser window. Log into X (if not already),
# then close the browser. Cookies are saved automatically.

COOKIES_FILE="$HOME/services/vibechecx/cookies.json"

echo "VibeChecx Cookie Setup"
echo "======================"
echo "A browser window will open. Log into X if needed,"
echo "browse for a few seconds, then close the browser."
echo "Cookies will be saved to: $COOKIES_FILE"
echo ""

python3 -c "
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 1024}
        )
        page = await context.new_page()
        await page.goto('https://x.com/home', wait_until='networkidle')
        print('Browser open — log into X and wait a moment...')
        print('Close the browser when done. Cookies will be saved automatically.')

        # Wait until browser closes
        await page.wait_for_event('close', timeout=0)

        # Save cookies
        cookies = await context.cookies()
        await context.storage_state(path='$COOKIES_FILE')
        print(f'Cookies saved: {len(cookies)} cookies')
        await browser.close()

asyncio.run(main())
"
