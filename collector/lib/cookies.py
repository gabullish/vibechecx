"""CookiePool — process-wide cookie scheduler with per-file cooldown state.

Replaces the per-script `_cookie_idx` global counter and the per-Worker
cooldown tracking in batch.py. One pool instance is shared by everything in
a single Python process (collect_profile, batch_scrape, patrol two-context,
etc.) so when one cookie gets 429'd, every consumer routes around it.

Threading note: Python collectors are single-process; we use an asyncio
lock for safe sharing across coroutines. If a future caller wants to use
this from threads, swap to threading.Lock.

Usage:
    pool = CookiePool(COOKIE_DIR)            # picks up main/scraper1/scraper2 by default
    handle = await pool.acquire()            # blocks if all cooled down, returns handle
    try:
        ... use handle.path ...
        pool.report_success(handle)
    except RateLimited:
        pool.report_429(handle)
    except AuthFailure:
        pool.report_auth_failure(handle)
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger("vibechecx.cookies")


# Exponential backoff: consecutive 429s on the same cookie ramp the cooldown
# from 2 min → 5 min → 15 min → 30 min. A single success resets the ladder.
_BACKOFF_SECONDS = [120, 300, 900, 1800]
_DEFAULT_FILES = ("main.json", "scraper1.json", "scraper2.json")


@dataclass
class CookieHandle:
    """One cookie file's state inside the pool."""
    path: str
    name: str  # file basename, for logging
    consecutive_429s: int = 0
    cooldown_until: float = 0.0
    last_used_at: float = 0.0
    in_use: bool = False
    disabled: bool = False  # set permanently on auth failure
    success_count: int = 0
    failure_count: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def available(self) -> bool:
        return (not self.disabled) and (time.time() >= self.cooldown_until)

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())


class NoCookiesAvailable(Exception):
    """Raised when acquire(timeout=...) gives up waiting."""


class CookiePool:
    def __init__(self, cookie_dir: str, files: Iterable[str] | None = None):
        files = list(files) if files else list(_DEFAULT_FILES)
        handles: list[CookieHandle] = []
        for fname in files:
            path = os.path.join(cookie_dir, fname)
            if os.path.exists(path) and os.path.getsize(path) > 100:
                handles.append(CookieHandle(path=path, name=fname))
        if not handles:
            raise FileNotFoundError(
                f"No usable cookie files in {cookie_dir} (looked for {files})"
            )
        self._handles = handles
        self._dir = cookie_dir
        self._pool_lock = asyncio.Lock()
        logger.info("CookiePool initialised with %d cookies: %s",
                    len(handles), [h.name for h in handles])

    @property
    def total_count(self) -> int:
        return len(self._handles)

    @property
    def usable_count(self) -> int:
        return sum(1 for h in self._handles if h.available)

    @property
    def active_count(self) -> int:
        """Cookies neither disabled nor in cooldown — usable + in_use."""
        return sum(1 for h in self._handles if not h.disabled)

    def status(self) -> list[dict]:
        """Snapshot of every cookie's state — for logging / debugging."""
        return [
            {
                "name": h.name,
                "available": h.available,
                "in_use": h.in_use,
                "disabled": h.disabled,
                "cooldown_remaining": round(h.cooldown_remaining, 1),
                "consecutive_429s": h.consecutive_429s,
                "success_count": h.success_count,
                "failure_count": h.failure_count,
            }
            for h in self._handles
        ]

    # ─── acquire / release ─────────────────────────────────────────────

    async def acquire(self, *, timeout: float | None = None,
                      exclusive: bool = False) -> CookieHandle:
        """Return an available cookie handle.

        timeout: wait up to N seconds for a cookie to become available.
                 None = block forever. Raises NoCookiesAvailable on timeout.
        exclusive: mark the handle as in_use (other acquire() calls won't
                   return it until release()).  Off by default — most callers
                   only read the path, so sharing is fine.
        """
        start = time.time()
        while True:
            async with self._pool_lock:
                # Hard fail if every cookie is permanently disabled
                if all(h.disabled for h in self._handles):
                    raise NoCookiesAvailable("All cookies are permanently disabled (auth failures)")

                candidates = [h for h in self._handles if h.available
                              and (not exclusive or not h.in_use)]
                if candidates:
                    # Pick the least-recently-used to spread load.
                    handle = min(candidates, key=lambda h: h.last_used_at)
                    handle.last_used_at = time.time()
                    if exclusive:
                        handle.in_use = True
                    return handle

                # All cooled down or busy → compute soonest wake-up
                if exclusive:
                    soonest = min(
                        (h.cooldown_remaining for h in self._handles
                         if not h.disabled and (h.in_use or h.cooldown_remaining > 0)),
                        default=0,
                    )
                else:
                    soonest = min(
                        (h.cooldown_remaining for h in self._handles if not h.disabled),
                        default=0,
                    )

            wait = max(1.0, min(soonest or 5.0, 30.0))
            if timeout is not None:
                if time.time() - start + wait > timeout:
                    raise NoCookiesAvailable(
                        f"No cookies available within {timeout:.0f}s "
                        f"(pool status: {self.status()})"
                    )
            logger.info("CookiePool: all cookies cooling down, sleeping %.1fs", wait)
            await asyncio.sleep(wait)

    def release(self, handle: CookieHandle) -> None:
        """Clear the in_use flag set by acquire(exclusive=True). Safe to call
        always, even on non-exclusive handles."""
        handle.in_use = False

    # ─── reporting ──────────────────────────────────────────────────────

    def report_success(self, handle: CookieHandle) -> None:
        """Reset the backoff ladder for this cookie."""
        handle.success_count += 1
        if handle.consecutive_429s > 0:
            logger.info("CookiePool: @%s recovered after %d 429s",
                        handle.name, handle.consecutive_429s)
        handle.consecutive_429s = 0
        handle.cooldown_until = 0.0
        handle.in_use = False

    def report_429(self, handle: CookieHandle) -> float:
        """Escalate this cookie's backoff and apply a cooldown. Returns the
        cooldown duration applied (seconds)."""
        handle.failure_count += 1
        level = min(handle.consecutive_429s, len(_BACKOFF_SECONDS) - 1)
        wait = _BACKOFF_SECONDS[level] + random.uniform(0, 30)
        handle.cooldown_until = time.time() + wait
        handle.consecutive_429s += 1
        handle.in_use = False
        logger.warning(
            "CookiePool: %s rate-limited, cooldown %.0fs (level %d, total 429s=%d)",
            handle.name, wait, level, handle.consecutive_429s,
        )
        return wait

    def report_auth_failure(self, handle: CookieHandle) -> None:
        """Disable this cookie permanently for the rest of the process.
        Cookie files don't recover from a 401 mid-run — they need fresh login."""
        handle.disabled = True
        handle.in_use = False
        logger.error("CookiePool: %s disabled (auth failure)", handle.name)


# ─── module-level convenience for one-shot scripts ─────────────────────

_default_pool: CookiePool | None = None


def default_pool(cookie_dir: str | None = None) -> CookiePool:
    """Return a process-wide singleton pool. Most callers want this — only
    construct your own CookiePool if you need isolation."""
    global _default_pool
    if _default_pool is None:
        if cookie_dir is None:
            # Resolve relative to repo root
            here = os.path.dirname(os.path.abspath(__file__))
            cookie_dir = os.path.join(
                os.path.dirname(os.path.dirname(here)), "cookies",
            )
        _default_pool = CookiePool(cookie_dir)
    return _default_pool


def reset_default_pool() -> None:
    """For tests — drop the singleton so the next default_pool() rebuilds it."""
    global _default_pool
    _default_pool = None
