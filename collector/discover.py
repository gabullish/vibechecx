#!/usr/bin/env python3
"""VibeChecx — discover affiliates via GraphQL cursor pagination."""
import asyncio, json, os

COOKIES = os.path.expanduser("~/services/vibechecx/cookies/main.json")
IGNORE_EXT = (".css",".js",".svg",".ico",".png",".jpg",".webp",".woff2",".woff")

def bottom_cursor(payload):
    result = [None]
    def walk(n):
        if isinstance(n, dict):
            if n.get("cursorType") == "Bottom":
                result[0] = n.get("value")
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(payload)
    return result[0]

def walk_entries(payload):
    try:
        instrs = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
    except (KeyError, TypeError):
        return
    for instr in instrs:
        for entry in instr.get("entries", []):
            if entry.get("entryId", "").startswith("user-"):
                yield entry

def is_business_account(user_result):
    try:
        if user_result.get("is_verified_organization") is True:
            return True
        if user_result.get("__typename") == "UserVerifiedOrganization":
            return True
        return False
    except:
        return False

async def discover_affiliates(handle):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=COOKIES if os.path.exists(COOKIES) else None,
            viewport={"width": 1280, "height": 1024},
        )
        page = await ctx.new_page()

        timeline_responses = []
        user_result = None

        async def on_response(resp):
            nonlocal user_result
            url = resp.url
            if url.endswith(IGNORE_EXT):
                return
            if "graphql" not in url and "api" not in url:
                return
            try:
                j = await resp.json()
            except:
                return
            jstr = json.dumps(j)

            # UserByScreenName check
            if not user_result and ("result_by_screen_name" in jstr or "userByScreenName" in jstr):
                ur = j
                for key in ("data",):
                    if isinstance(ur, dict):
                        ur = ur.get(key, {})
                if isinstance(ur, dict):
                    for k in ("user_result_by_screen_name", "user", "userByScreenName"):
                        if k in ur:
                            inner = ur[k]
                            if isinstance(inner, dict) and "result" in inner:
                                user_result = inner["result"]
                            elif isinstance(inner, dict):
                                user_result = inner
                            break
                return

            # Timeline entries check (affiliates data)
            try:
                data = j.get("data", {})
                for key in data:
                    val = data[key]
                    if not isinstance(val, dict):
                        continue
                    result = val.get("result", val)
                    if not isinstance(result, dict):
                        continue
                    tl = result.get("timeline", {})
                    if not isinstance(tl, dict):
                        continue
                    instrs = tl.get("timeline", tl).get("instructions", [])
                    if not instrs:
                        continue
                    for instr in instrs:
                        entries = instr.get("entries", [])
                        if entries and any(e.get("entryId","").startswith("user-") for e in entries):
                            timeline_responses.append(j)
                            return
            except:
                pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        await page.goto(f"https://x.com/{handle}/affiliates", timeout=30000)
        await page.wait_for_timeout(3000)

        if not timeline_responses:
            if user_result is not None:
                biz = is_business_account(user_result)
                if not biz:
                    print(f"  @{handle} is personal (no affiliates)", flush=True)
                    await browser.close()
                    return {"members": [], "seed_avatar": ""}
            print(f"  Could not check @{handle} (no timeline)", flush=True)
            await browser.close()
            return None

        # Scroll loop — capture more pages
        last_cursor = None
        stale = 0
        for i in range(50):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            cur = bottom_cursor(timeline_responses[-1]) if timeline_responses else None
            if cur == last_cursor:
                stale += 1
                if stale >= 3:
                    print(f"  Cursor settled after {i+1}/{len(timeline_responses)} scrolls/responses", flush=True)
                    break
            else:
                stale = 0
                last_cursor = cur

        await browser.close()

    # Extract seed avatar
    seed_avatar = ""
    if user_result and isinstance(user_result, dict):
        leg = user_result.get("legacy", {})
        if leg:
            seed_avatar = leg.get("profile_image_url_https", "") or leg.get("profile_image_url", "")

    # Extract all users
    users = {}
    for body in timeline_responses:
        for entry in walk_entries(body):
            try:
                ic = entry["content"]["itemContent"]
                ur = ic.get("user_results", {}).get("result", {})
                if not ur:
                    continue
                sn = ur.get("core", {}).get("screen_name") or ur.get("legacy", {}).get("screen_name", "")
                if sn and sn not in users:
                    leg = ur.get("legacy", {})
                    users[sn] = {
                        "username": sn,
                        "display_name": leg.get("name", ""),
                        "avatar": leg.get("profile_image_url_https", ""),
                        "bio": leg.get("description", "")[:120],
                        "followers": leg.get("followers_count", 0),
                    }
            except (KeyError, TypeError, AttributeError):
                pass

    return {"members": list(users.values()), "seed_avatar": seed_avatar}

if __name__ == "__main__":
    import sys
    handle = sys.argv[1] if len(sys.argv) > 1 else "solflare"
    result = asyncio.run(discover_affiliates(handle))
    if result is None:
        print(f"Could not determine account type for @{handle}")
    else:
        members = result["members"]
        if not members:
            print(f"@{handle} is not a Verified Organization.")
        else:
            print(f"Found {len(members)} members for @{handle}:")
            for m in members:
                print(f"  @{m['username']:20s} {m.get('display_name','')[:30]}")
