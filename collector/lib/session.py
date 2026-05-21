"""scrape_sessions heartbeat / finish wrappers.

Centralises the boilerplate that was copy-pasted across every collector
script. The pattern is:
  1. Web UI passes VIBECHECX_SCRAPE_SESSION_ID env var.
  2. Collector reads it; if missing, heartbeat()/finish() become no-ops.
  3. vibechecx_scrape_status itself may not import (e.g. in unit tests),
     so we catch that too.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("vibechecx.session")

# Path setup so the shared `web/` module is importable from any collector script.
_WEB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web",
)
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)

try:
    from vibechecx_scrape_status import (  # type: ignore
        start_session as _ss_start,
        heartbeat as _ss_hb,
        finish_session as _ss_finish,
    )
except Exception:  # pragma: no cover — web/ unavailable
    _ss_start = _ss_hb = _ss_finish = None  # type: ignore


def current_session_id() -> int | None:
    """Read VIBECHECX_SCRAPE_SESSION_ID from env, return int or None."""
    raw = os.environ.get("VIBECHECX_SCRAPE_SESSION_ID")
    try:
        return int(raw) if raw else None
    except (ValueError, TypeError):
        return None


def heartbeat(**kw) -> None:
    """Update progress on the current session row. No-op when no session id
    is in env or when scrape_status itself isn't available."""
    sid = current_session_id()
    if sid is None or _ss_hb is None:
        return
    try:
        _ss_hb(sid, **kw)
    except Exception:
        logger.warning("session heartbeat failed", exc_info=True)


def finish(status: str, *, tweets_collected: int | None = None,
           error: str | None = None) -> None:
    """Mark the current session row as completed/failed."""
    sid = current_session_id()
    if sid is None or _ss_finish is None:
        return
    try:
        _ss_finish(sid, status=status, tweets_collected=tweets_collected, error=error)
    except Exception:
        logger.warning("session finish failed", exc_info=True)


def start_owned_session(*, user_id, session_type, target_handle=None,
                        cohort_id=None, progress_total=0) -> int | None:
    """Start a new scrape_sessions row for a CLI-launched (un-coordinated) run.

    Returns the new session id, or None if scrape_status isn't importable.
    """
    if _ss_start is None:
        return None
    try:
        return _ss_start(
            user_id=user_id, session_type=session_type,
            target_handle=target_handle, cohort_id=cohort_id,
            progress_total=progress_total,
        )
    except Exception:
        logger.warning("start_owned_session failed", exc_info=True)
        return None
