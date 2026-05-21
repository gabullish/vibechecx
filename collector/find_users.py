#!/usr/bin/env python3
"""Check ALL GraphQL responses for user data on affiliates page."""
import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state="/home/boto/services/vibechecx/cookies/main.json"
        )
        page = await ctx.new_page()

        # Try 3 different approaches and see which gives the most users

        # Approach 1: Capture ALL graphql with any business/team/user endpoint
        gql_by_url = {}

        async def capture(response):
            url = response.url
            # Check the GraphQL endpoint name (last part of URL)
            parts = url.split("/")
            opname = parts[-1].split("?")[0] if parts else ""
            # Capture any graphql response that might have user data
            if opname and "graphql" not in opname:
                try:
                    body = await response.json()
                    gql_by_url[opname] = body
                except: pass

        page.on("response", capture)
        await page.goto("https://x.com/solflare/affiliates", timeout=30000)
        await page.wait_for_timeout(3000)

        for i in range(20):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

        await page.wait_for_timeout(3000)

        # Now check each captured response for the specific handles we want
        search_handles = ["laquarta","devmajesty","solquicks","gmiko88","mariosuperg","rustedcrane","SolGab","mauricedotxyz","dapson_kro","__Lorso","ItsQuacken","Codyboy101","0racle0racle","Maky_sol","LegendlarrySol","Crypt0_Yoda","filip_solflare","vidor_solflare","SatoshiTriangle","callahan_dks","TheRealEmko_","mirogrk","kasparas_sol","K8FromState","bibatheking","leClop_sol","tommysol_","SolflareEmpire","SolflareKingdom","byndspeculation"]

        print(f"\nGraphQL endpoints captured: {len(gql_by_url)}")
        for opname, body in sorted(gql_by_url.items()):
            s = json.dumps(body)
            found = [h for h in search_handles if h in s]
            print(f"  {opname[:40]:40s} {len(found)}/30 matches: {', '.join(found[:5])}{'...' if len(found)>5 else ''}")

        # Approach 2: Check window.__INITIAL_STATE__
        print("\nChecking window.__INITIAL_STATE__...")
        init = await page.evaluate("""
            () => {
                try {
                    return JSON.stringify(window.__INITIAL_STATE__);
                } catch(e) { return null; }
            }
        """)
        if init:
            found = [h for h in search_handles if h in init]
            print(f"  __INITIAL_STATE__: {len(found)}/30 matches")

        # Approach 3: innerText
        print("\nChecking innerText...")
        text = await page.evaluate("() => document.body.innerText")
        found = [h for h in search_handles if h in text]
        print(f"  innerText: {len(found)}/30 matches")
        for h in search_handles:
            print(f"  {'P' if h in found else 'A'} @{h}")

        await browser.close()

asyncio.run(main())
