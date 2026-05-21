# VibeChecx — X API Integration Plan (BYOK)
_May 2026 — saved for later_

## Concept

Not "replace scraper with API." Use API for what it's genuinely better at, keep scraper for everything else. Users bring their own Bearer Token ($100/mo Basic tier, their cost).

---

## What the X API v2 actually gives (Basic, $100/mo)

| Data point | Scraper | Official API (Basic) |
|---|---|---|
| Timeline (own tweets) | ✅ unlimited scroll | ✅ up to 3,200 |
| Timeline (other accounts) | ✅ | ✅ but counts against quota |
| Likes, retweets, replies | ✅ | ✅ exact |
| Views/impressions | ✅ (GraphQL intercept) | ❌ Pro only ($5k/mo) |
| Inbound mentions | ⚠️ replyminer (slow, brittle) | ✅ clean endpoint |
| Reply threads | ⚠️ replyminer | ❌ not in v2 easily |
| Search | ✅ | ✅ 7-day window only |
| Historical >3200 tweets | ✅ | ❌ |
| TOS risk | ❌ real | ✅ none |

**Key insight:** the scraper is more capable than Basic API for most things. The one place API cleanly wins is **inbound mentions** — exactly what replyminer struggles with.

---

## The right hybrid

```
User provides Bearer Token (for their own account)
         ↓
Coordinator detects key present
         ↓
┌──────────────────────────────┬─────────────────────────────┐
│ API handles                  │ Scraper handles             │
├──────────────────────────────┼─────────────────────────────┤
│ Inbound mentions             │ Cohort member timelines     │
│ Own account metric precision │ Competitor data             │
│ Profile data accuracy        │ Views (not in Basic API)    │
│ No cookie risk for own acct  │ Reply thread content        │
└──────────────────────────────┴─────────────────────────────┘
```

The killer unlock is **inbound mentions**. One API call to `GET /2/users/:id/mentions` replaces 200 Playwright permalink navigations. Feeds `kudos`, `community_mascot`, and hidden patterns with real data.

---

## What to build

**New pieces (no changes to existing scraper):**

- `user_api_keys` table — `user_id`, `bearer_token` (encrypted), `x_user_id`, `verified_at`
- Settings UI — paste Bearer Token + "Test connection" button calling `/2/users/me`
- `collector/lib/xapi_v2.py` — official v2 client (separate from internal GraphQL `xapi.py`)
- `collector/mentions_collect.py` — replaces replyminer for API-keyed accounts
- Coordinator hook — if user has key, run `mentions_collect` instead of / after `replyminer`

---

## Effort

| Piece | Time |
|---|---|
| DB table + encryption helper | 2h |
| Settings UI + test-connection endpoint | 2h |
| `xapi_v2.py` client | 2h |
| `mentions_collect.py` | 3h |
| Coordinator hook + fallback logic | 2h |
| **Total** | **~1.5 days** |

---

## BYOK model

Users enter their own $100/mo key in settings. VibeChecx stores it encrypted and uses it during their scrapes. You don't pay the API cost — they do. Accounts with a key get richer insight data (real inbound mentions) than accounts without one. For your own account, you put in your own key.
