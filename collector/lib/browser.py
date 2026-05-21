"""Stealth-enabled Playwright context factory.

Single source of truth for browser launch parameters across collect, batch,
patrol, replyminer, search_collect. Centralising this means future tweaks
(new user-agent, additional anti-detection patches, alternative timezones)
land in one file.
"""
from __future__ import annotations

import logging
import os
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger("vibechecx.browser")

# playwright-stealth 2.x ships a Stealth class with `apply_stealth_async(page)`.
# Older 1.x exposed a free `stealth_async()` function; support both shapes.
_stealth_apply = None
try:
    from playwright_stealth import Stealth as _StealthCls  # type: ignore
    _stealth_apply = _StealthCls().apply_stealth_async
except ImportError:
    try:
        from playwright_stealth import stealth_async as _stealth_apply  # type: ignore
    except ImportError:  # pragma: no cover
        logger.info("playwright-stealth not installed — anti-detection disabled")

# Mid-2025 Chromium on Linux x86_64. Update when X starts flagging the version.
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _random_viewport() -> dict:
    """Common 1080p+ desktop sizes. Randomised so two contexts in the same
    run look like distinct devices to fingerprinters."""
    return {
        "width":  random.randint(1260, 1440),
        "height": random.randint(820, 1080),
    }


async def open_context(
    browser: "Browser",
    *,
    cookie_path: str | None = None,
    locale: str = "en-US",
    timezone_id: str = "America/New_York",
    viewport: dict | None = None,
    user_agent: str | None = None,
) -> "BrowserContext":
    """Open a new Playwright BrowserContext with our standard fingerprint.

    cookie_path: Playwright storage_state JSON. None → unauthenticated.
    viewport:    overrides the random default.
    user_agent:  overrides the default UA string.
    """
    return await browser.new_context(
        storage_state=cookie_path if (cookie_path and os.path.exists(cookie_path)) else None,
        viewport=viewport or _random_viewport(),
        user_agent=user_agent or _DEFAULT_UA,
        locale=locale,
        timezone_id=timezone_id,
    )


async def apply_stealth(page: "Page") -> None:
    """Apply playwright-stealth patches to a page (WebDriver hiding, WebGL
    fingerprint smoothing, chrome.runtime stub, etc.). No-op if the library
    isn't installed."""
    if _stealth_apply is None:
        return
    try:
        await _stealth_apply(page)
    except Exception:
        logger.warning("apply_stealth failed", exc_info=True)


async def block_heavy_resources(page: "Page") -> None:
    """Abort image/media/font/stylesheet requests so the page loads ~2× faster.

    Used by patrol and replyminer — both only need the GraphQL JSON, never
    the rendered media. Don't apply to collect/batch where the visual layout
    matters for some intercepted requests.
    """
    async def _route(route):
        if route.request.resource_type in ("image", "media", "font", "stylesheet"):
            await route.abort()
        else:
            await route.continue_()
    await page.route("**/*", _route)


# Human-like timing primitives shared across scrollers.

def human_scroll_delay_ms() -> int:
    """A ~1.5s scroll pause with ±50% jitter — looks like idle reading."""
    return random.randint(1200, 2500)


def human_load_delay_ms() -> int:
    """First-load pause after navigation — slightly longer than scrolls."""
    return random.randint(2000, 3500)


def human_scroll_distance_pct() -> float:
    """How much of the page height to scroll, 85–100 %.  Robots scroll to
    `document.body.scrollHeight` every time; humans don't."""
    return random.uniform(0.85, 1.0)
