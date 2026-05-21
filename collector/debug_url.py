#!/usr/bin/env python3
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state="/home/boto/services/vibechecx/cookies/main.json",
            viewport={"width": 1280, "height": 1024}
        )
        page = await ctx.new_page()
        await page.goto("https://x.com/solflare/affiliates", timeout=30000)
        await page.wait_for_timeout(5000)
        print(f"URL: {page.url}")

        # Check for any aria-label containing "Affiliate"
        found = await page.evaluate("""
            () => {
                const els = document.querySelectorAll('[aria-label*="Affiliate"], [aria-label*="affiliate"]');
                return els.length;
            }
        """)
        print(f"Affiliate-labeled elements: {found}")

        # Check page for key handles
        text = await page.evaluate("() => document.body.innerText")
        print(f"Page contains 'laquarta': {'laquarta' in text}")
        print(f"Page contains 'TheRealEmko': {'TheRealEmko' in text}")
        print(f"Page contains 'SolflareEmpire': {'SolflareEmpire' in text}")

        await browser.close()

asyncio.run(main())
