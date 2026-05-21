# VibeChecx

Twitter/X analytics for cohorts. Scrapes tweet data via Playwright browser automation, stores it in Postgres, and serves a web dashboard with rankings, insights, and enrichment powered by LLMs.

---

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌───────────┐
│  Collector   │────▶│   Postgres   │◀────│  Web App  │
│  (scraper)   │     │  (database)  │     │  (FastAPI) │
└─────────────┘     └──────────────┘     └───────────┘
       │                                        │
       │  Playwright (headless/headful)          │  Uvicorn :5050
       │  Twitter GraphQL API                    │  HTML + HTMX + Alpine
       ▼                                        ▼
  Twitter/X                                Browser (user)
```

### Collector (`collector/`)

Playwright-based browser scraper that logs into Twitter/X using saved cookies and intercepts GraphQL API responses. Runs per-account, collects tweets, likes, views, retweets, and reply threads.

- **`collect.py`** — Main scraper for a single Twitter account. Opens two parallel browser contexts (Posts tab + Replies tab), scrolls to load tweets, intercepts GraphQL responses, parses and inserts into Postgres.
- **`batch.py`** — Batch coordinator that collects or re-processes all accounts in a cohort.
- **`coordinator.py`** — Web-triggered scrape runner with queue support, heartbeats, and status tracking.
- **`discover.py`** — Profile discovery: given a cohort, finds new accounts to add via LLM-driven search.
- **`patrol.py`** — Continuous re-scrape loop: crawls cohorts on a schedule, checks cookie health, manages rate limits.
- **`replyminer.py`** — Deep reply thread scraper for community analysis.
- **`reprocess.py`** — Re-parse raw GraphQL JSON responses from disk and re-insert into DB.
- **`search_collect.py`** — Keyword-based search collection.

**`collector/lib/`** — Shared infrastructure:
  - `browser.py` — Playwright browser launch/context management
  - `cookies.py` — Cookie pool with automatic rotation and cooling
  - `parser.py` — GraphQL response parser → tweet structs
  - `storage.py` — PostgreSQL insert helpers (batch upsert)
  - `session.py` — Scrape session state tracking
  - `xapi.py` — Twitter/X API interaction helpers

### Web App (`web/`)

FastAPI + Uvicorn web server serving HTML with HTMX and Alpine.js.

- **`app.py`** — App entry point, mounts all route modules, error handlers, startup tasks.
- **`core.py`** — DB connection, auth helpers, profile context management.
- **`ui.py`** — HTML template helpers: nav bar, cookie health pill, period buttons, tooltips.
- **`manage.py`** — CLI admin tool: `create-user`, `make-admin`, `list-users`, `rotate-sessions`.
- **`vibechecx_auth.py`** — Password hashing (bcrypt), session tokens, login/logout/register.
- **`vibechecx_config.py`** — Single source of truth for config. Reads from env vars (see [Configuration](#configuration)).
- **`vibechecx_insights.py`** — LLM-powered insight generation from tweet data.
- **`vibechecx_precision.py`** — Data freshness/precision badges (e.g. "24h stale").
- **`vibechecx_scrape_status.py`** — Real-time scrape progress tracking.
- **`queue_worker.py`** — Background queue worker for scrape jobs.
- **`security.py`** — Rate limiting for failed login attempts.

**`web/routes/`**:
  - `dash.py` — `/`, `/posts`, `/tags`, `/profile`, `/profiles`, `/leaderboard`
  - `cohorts.py` — `/cohort/{id}`, cohort leaderboard, member management, share tokens, CSV export
  - `accounts.py` — `/account/{handle}` — individual account view with sparklines and stats
  - `auth.py` — `/login`, `/register`, `/logout`
  - `admin.py` — Admin panel: user/cookie/cohort management
  - `scraping.py` — Scrape trigger endpoints, queue status
  - `discovery.py` — Account discovery and ecosystem analysis
  - `insights.py` — DeepSeek/Grok/OpenAI-powered cohort insights
  - `misc.py` — `/robots.txt`, `/favicon.ico`, health check

### Database (`db/`)

PostgreSQL with migrations in `db/migrations/`. Key tables:
- `tweets` — All scraped tweets with likes, views, retweets, reply metadata
- `accounts` — Twitter accounts with follower counts, display names
- `cohorts` — Named groups of accounts
- `cohort_members` — Account-to-cohort mapping
- `profiles` — Named workspaces (single account or cohort)
- `scrape_status` — Per-account scrape session tracking
- `insights_cache` — Cached LLM insight responses
- `scrape_queue` — Queued scrape jobs

---

## Scraping

### How it works

1. The scraper uses saved Twitter cookies (in `cookies/`) to authenticate as a real browser.
2. It opens a headless Chromium via Playwright, navigates to the target's profile, and intercepts XHR responses matching `x.com/i/api/graphql/`.
3. Both the **Posts** tab and **With replies** tab are scraped in parallel for maximum coverage.
4. Parsed tweets are upserted into PostgreSQL via batch operations.
5. Raw GraphQL JSON is saved to `collector/raw/` as a fallback.

### Usage

```bash
# Single account — headless (default)
cd collector && python3 collect.py <username>

# Visible browser (--headful)
cd collector && python3 collect.py <username> --headful

# Limit to 200 tweets, only fresh accounts
cd collector && python3 collect.py <username> --headful --limit 200 --fresh

# Re-process raw JSON from disk
cd collector && python3 reprocess.py

# Continuous patrol mode (scheduled re-scrapes)
cd collector && python3 patrol.py
```

### Cookies

Cookies are stored as JSON files in `cookies/`. The cookie pool rotates between `main.json`, `scraper1.json`, `scraper2.json` to avoid rate limits. Run the setup script to create initial cookies:

```bash
./vibechecx-setup-cookies.sh
```

### Headful Mode

By default, `SCRAPER_HEADFUL=true` in the config, so the browser window appears when scraping. Set `VIBECHECX_SCRAPER_HEADFUL=false` or pass `--headless` to suppress the window.

---

## Running the Web App

```bash
python3 web/app.py
# Serves on http://0.0.0.0:5050
```

Uses Uvicorn with multiple workers. Run via systemd or `nohup`.

### Admin CLI

```bash
python3 web/manage.py create-user <username> <password>
python3 web/manage.py make-admin <username>
python3 web/manage.py list-users
python3 web/manage.py rotate-sessions
```

---

## Configuration

All via environment variables (or defaults, see `vibechecx_config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `VIBECHECX_DB_HOST` | `localhost` | Postgres host |
| `VIBECHECX_DB_PORT` | `5432` | Postgres port |
| `VIBECHECX_DB_NAME` | `vibechecx` | Database name |
| `VIBECHECX_DB_USER` | `vibechecx` | DB user |
| `VIBECHECX_DB_PASSWORD` | `vibechecx_pass` | DB password |
| `DEEPSEEK_API_KEY` | — | DeepSeek API key (insights) |
| `XAI_API_KEY` / `GROK_API_KEY` | — | Grok fallback for insights |
| `OPENAI_API_KEY` | — | OpenAI fallback for insights |
| `VIBECHECX_CUTOFF_DAYS` | `30` | Max age of tweets to collect |
| `VIBECHECX_REGISTRATION_OPEN` | `true` | Allow new user signups |
| `VIBECHECX_SCRAPER_HEADFUL` | `true` | Show browser window during scrape |

API keys are also read from `~/.openclaw/credentials/{provider}.key` as fallback.

---

## Exposing via Cloudflare Tunnel

```bash
cloudflared tunnel --url http://localhost:5050
```

Gives a random `*.trycloudflare.com` URL with HTTPS. No setup required.

---

## Tech Stack

- **Language:** Python 3.13
- **Web framework:** FastAPI + Uvicorn
- **Frontend:** Server-rendered HTML, HTMX 2.x, Alpine.js 3.x, TailwindCSS (CDN)
- **Scraper:** Playwright (Chromium headless shell)
- **Database:** PostgreSQL with psycopg2
- **Auth:** bcrypt + session tokens
- **LLM Insights:** DeepSeek (primary), Grok/xAI (fallback), OpenAI (third fallback)
- **OS:** Debian 13, NVIDIA GTX 1650 (Optimus via envycontrol)

---

## Project Structure

```
vibechecx/
├── collector/         # Playwright scraper + tools
│   ├── lib/           # Shared: browser, cookies, parser, storage, session, xapi
│   ├── collect.py     # Single-account scraper
│   ├── batch.py       # Batch cohort scraper
│   ├── coordinator.py # Web-triggered scrape runner
│   ├── patrol.py      # Continuous re-scrape loop
│   ├── replyminer.py  # Deep reply thread scraper
│   ├── discover.py    # Account discovery
│   └── raw/           # Raw GraphQL JSON dumps (gitignored)
├── web/               # FastAPI web application
│   ├── app.py         # App entry point
│   ├── manage.py      # CLI admin commands
│   ├── routes/        # Route handlers
│   ├── ui.py          # HTML template helpers
│   ├── core.py        # DB + auth + profile helpers
│   └── vibechecx_*.py # Auth, config, insights, precision
├── db/                # SQL schema + migrations
├── cookies/           # Twitter auth cookies (gitignored)
├── tests/             # Python tests
└── vibechecx_config.py # Shared configuration
```
