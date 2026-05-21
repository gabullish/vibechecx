#!/usr/bin/env python3
import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state="/home/boto/services/vibechecx/cookies/main.json")
        page = await ctx.new_page()
        await page.goto("https://x.com/solflare/affiliates", timeout=30000)
        await page.wait_for_timeout(3000)

        for i in range(30):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

        handles = ["laquarta","devmajesty","solquicks","gmiko88","mariosuperg","rustedcrane","SolGab","mauricedotxyz","dapson_kro","__Lorso","ItsQuacken","Codyboy101","0racle0racle","Maky_sol","LegendlarrySol","Crypt0_Yoda","filip_solflare","vidor_solflare","SatoshiTriangle","callahan_dks","TheRealEmko_","mirogrk","kasparas_sol","K8FromState","bibatheking","leClop_sol","tommysol_","SolflareEmpire","SolflareKingdom","byndspeculation"]

        present = await page.evaluate(f"""() => {{
            const t = document.body.textContent;
            return {json.dumps(handles)}.filter(h => t.includes('@' + h) || t.includes('/' + h));
        }}""")

        print(f"Found {len(present)}/30 handles:")
        for h in handles:
            print(f"  {'P' if h in present else 'A'} @{h}")

        await browser.close()

asyncio.run(main())
