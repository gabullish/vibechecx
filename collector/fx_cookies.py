#!/usr/bin/env python3
"""Extract X cookies from Firefox for scraper accounts."""

import sqlite3, json, os

FX_PROFILE = os.path.expanduser("~/.mozilla/firefox/7wqiblu4.default-esr")
COOKIE_DIR = os.path.expanduser("~/services/vibechecx/cookies")
COOKIE_DB = os.path.join(FX_PROFILE, "cookies.sqlite")

OUTPUTS = [
    ("main.json", "main scraper (currently in Firefox)"),
    ("scraper1.json", "scraper account 2 (@sadieson22)"),
    ("scraper2.json", "scraper account 3 (@barbeiraoda10)"),
]

print("=== VibeChecx Multi-Account Cookies ===\n")

for fname, label in OUTPUTS:
    outpath = os.path.join(COOKIE_DIR, fname)
    if os.path.exists(outpath) and os.path.getsize(outpath) > 100:
        print(f"  [{fname}] already has cookies, skipping")
        continue

    input(f"  Switch Firefox to {label}, then press Enter...")

    db = sqlite3.connect(COOKIE_DB)
    cur = db.cursor()
    cur.execute("SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies WHERE host LIKE '%x.com%' OR host LIKE '%twitter.com%'")
    ss_map = {0: "None", 1: "Lax", 2: "Strict"}
    cookies = []
    for row in cur.fetchall():
        name, value, host, cpath, expiry, is_sec, is_http, ss = row
        domain = host[1:] if host.startswith(".") else host
        cookies.append({"name": name, "value": value, "domain": domain, "path": cpath, "expires": expiry if expiry > 0 else -1, "httpOnly": bool(is_http), "secure": bool(is_sec), "sameSite": ss_map.get(ss, "None")})
    db.close()

    auth = [c["name"] for c in cookies if c["name"] in ("auth_token", "ct0", "twid")]
    with open(outpath, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f, indent=2)

    print(f"  ✓ {fname}: {len(cookies)} cookies saved ({len(auth)} auth tokens)\n")

print("All accounts captured!")
for f in ["main.json", "scraper1.json", "scraper2.json"]:
    p = os.path.join(COOKIE_DIR, f)
    sz = os.path.getsize(p) if os.path.exists(p) else 0
    print(f"  {f}: {sz}b")
