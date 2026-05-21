"""VibeChecx collector library — shared infrastructure for the scraper army.

Modules:
- parser:  X GraphQL response extraction (tweets, authors, conversations)
- browser: stealth-enabled Playwright context factory
- cookies: CookiePool with per-cookie cooldown tracking
- storage: DB upserts for accounts, tweets, replies, snapshots, metrics
- session: scrape_sessions heartbeat / finish wrappers
- xapi:    direct httpx X GraphQL client (use sparingly — flag-prone)
"""
