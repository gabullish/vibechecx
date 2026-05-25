#!/usr/bin/env python3
"""web/app.py — FastAPI assembler.  ~200 lines; all logic lives in sub-modules.

Imports that must keep working after this refactor:
    from web.app import app, NoActiveProfile, profile_account_ids,
                        _leaderboard_query, tag_chip, period_buttons
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request  # noqa: E402
import html as _html
from fastapi.responses import HTMLResponse  # noqa: E402

# ── Create the FastAPI application ────────────────────────────────────
# docs_url=None disables /docs, /redoc, /openapi.json — don't expose the API blueprint.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# ── Mount all routers ─────────────────────────────────────────────────
from web.routes.auth import router as auth_router          # noqa: E402
from web.routes.dash import router as dash_router          # noqa: E402
from web.routes.scraping import router as scraping_router  # noqa: E402
from web.routes.cohorts import router as cohorts_router    # noqa: E402
from web.routes.accounts import router as accounts_router  # noqa: E402
from web.routes.discovery import router as discovery_router  # noqa: E402
from web.routes.insights import router as insights_router  # noqa: E402
from web.routes.misc import router as misc_router          # noqa: E402
from web.routes.admin import router as admin_router        # noqa: E402
from web.security import SecurityHeadersMiddleware          # noqa: E402

app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth_router)
app.include_router(dash_router)
app.include_router(scraping_router)
app.include_router(cohorts_router)
app.include_router(accounts_router)
app.include_router(discovery_router)
app.include_router(insights_router)
app.include_router(misc_router)
app.include_router(admin_router)

# ── Back-compat re-exports ─────────────────────────────────────────────
# Tests and external callers import these names directly from web.app.
# Re-export from their new homes so those imports keep working.

from web.core import NoActiveProfile, profile_account_ids  # noqa: E402, F401
from web.routes.cohorts import _leaderboard_query           # noqa: E402, F401
from web.ui import tag_chip, period_buttons                 # noqa: E402, F401

# DB config alias (back-compat for any helper still reading from module scope)
from vibechecx_config import DB_CONFIG as DB                # noqa: E402, F401

# Re-export COOKIE_DIR at module scope so tests can monkeypatch it here.
# cookie_health_pill_html is also re-exported; the wrapper below reads
# COOKIE_DIR from *this* module so monkeypatching web.app.COOKIE_DIR works.
from vibechecx_config import COOKIE_DIR                     # noqa: E402, F401
import web.ui as _ui                                        # noqa: E402


def cookie_health_pill_html() -> str:
    """Thin re-export that reads COOKIE_DIR from this module's namespace,
    so tests can monkeypatch web.app.COOKIE_DIR and see the effect."""
    import sys as _sys
    _this = _sys.modules[__name__]
    cd = getattr(_this, "COOKIE_DIR", COOKIE_DIR)
    # Temporarily swap ui's COOKIE_DIR, call, restore.
    _old = _ui.COOKIE_DIR
    try:
        _ui.COOKIE_DIR = cd
        return _ui.cookie_health_pill_html()
    finally:
        _ui.COOKIE_DIR = _old


logging.basicConfig(
    level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from web.queue_worker import start as _start_queue_worker  # noqa: E402
from web.patrol_scheduler import start_patrol_scheduler as _start_patrol_scheduler  # noqa: E402
from web.metric_refresh_worker import start as _start_metric_refresh_worker  # noqa: E402
import traceback as _traceback
import logging as _logging

_err_log = _logging.getLogger("vibechecx.errors")

@app.exception_handler(Exception)
async def _global_error_handler(request: Request, exc: Exception):
    tb = _traceback.format_exc()
    _err_log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, tb)
    msg = _html.escape(str(exc)[:500])
    return HTMLResponse(
        f"<html><body style='font-family:sans-serif;padding:2rem;background:#111;color:#ccc'>"
        f"<h2>Something went wrong.</h2>"
        f"<p style='color:#f87171;background:#1e1e1e;padding:1rem;border-radius:8px;font-family:monospace;font-size:13px;'>{msg}</p>"
        f"<details><summary style='cursor:pointer;color:#60a5fa;margin-top:1rem;'>Full traceback</summary>"
        f"<pre style='background:#1e1e1e;padding:1rem;border-radius:8px;overflow:auto;font-size:12px;color:#ccc;margin-top:0.5rem;'>{_html.escape(tb[:8000])}</pre>"
        f"</details></body></html>",
        status_code=500,
    )

_queue_worker_lockfile = None  # module-scope so the lock survives _startup()

@app.on_event("startup")
async def _startup():
    # With multiple uvicorn workers each process runs this handler. We only
    # want one queue worker. Use a lock file so only the first process to
    # start claims the queue worker role. The file handle MUST live at module
    # scope — if it goes out of scope the OS releases the flock and every
    # worker ends up running the queue (double-spawn bug).
    global _queue_worker_lockfile
    import fcntl, tempfile

    # On Render, the queue worker runs on the local Boto machine instead
    # (Boto has the actual Playwright browser + cookies).
    if os.environ.get("RENDER"):
        _err_log.info("RENDER=1 — queue worker disabled; Boto handles scrapes")
        return

    lock_path = os.path.join(tempfile.gettempdir(), "vibechecx_queue_worker.lock")
    try:
        _queue_worker_lockfile = open(lock_path, "w")
        fcntl.flock(_queue_worker_lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _start_queue_worker()
        _start_patrol_scheduler()
        _start_metric_refresh_worker()
    except BlockingIOError:
        _queue_worker_lockfile = None  # another worker already owns the queue

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("VIBECHECX_PORT", "5050"))
    workers = int(os.environ.get("VIBECHECX_WORKERS", "2"))
    uvicorn.run("web.app:app", host="0.0.0.0", port=port, log_level="info", workers=workers,
                server_header=False)
