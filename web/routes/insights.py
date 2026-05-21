"""web/routes/insights.py — /account/{handle}/generate-insights, /account/{handle}/insights, export"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

import vibechecx_insights as vi  # noqa: E402

from web.core import q, require_login  # noqa: E402
from web.render_insights import _render_insights, _insight_export_response, _render_timely_angles, _ai_error_card  # noqa: E402

router = APIRouter()


@router.post("/account/{handle}/generate-insights", response_class=HTMLResponse)
def account_generate_insights(handle: str, r: Request, period: str = "7d"):
    redir = require_login(r)
    if redir:
        return redir
    h = handle.lower().lstrip("@")
    ac = q("SELECT id FROM accounts WHERE username=%s", (h,))
    if not ac:
        return "<span class='text-red-400'>Account not found</span>"
    insight, *_ = vi.cached_insights("account", ac[0]["id"], period, force=True)
    if not insight:
        return _ai_error_card(
            f"/account/{h}/generate-insights?period={period}",
            "account-insights-content",
        )
    return account_insights(handle, r, period)


@router.get("/account/{handle}/insights", response_class=HTMLResponse)
def account_insights(handle: str, r: Request, period: str = "7d"):
    redir = require_login(r)
    if redir:
        return redir
    h = handle.lower().lstrip("@")
    ac = q("SELECT id, username FROM accounts WHERE username=%s", (h,))
    if not ac:
        return "<p class='text-red-400'>Not found</p>"
    aid = ac[0]["id"]
    result, provider, _from_cache, age_min = vi.cached_insights("account", aid, period, generate_if_missing=False)
    angles, angles_exist = vi.get_cached_timely_angles("account", aid, period)
    return _render_insights(
        result=result, scope_type="account", scope_key=aid,
        scope_display=f"@{ac[0]['username']}",
        period=period, provider=provider, age_min=age_min,
        regen_endpoint=f"/account/{h}/generate-insights?period={period}",
        period_get_endpoint=f"/account/{h}/insights",
        target_id="account-insights-content",
        timely_angles=angles,
        timely_angles_exists=angles_exist,
        timely_angles_poll_url=f"/account/{h}/timely-angles?period={period}",
    )


@router.get("/account/{handle}/timely-angles", response_class=HTMLResponse)
def account_timely_angles(handle: str, r: Request, period: str = "7d"):
    redir = require_login(r)
    if redir:
        return redir
    h = handle.lower().lstrip("@")
    ac = q("SELECT id FROM accounts WHERE username=%s", (h,))
    if not ac:
        return ""
    aid = ac[0]["id"]
    angles, exists = vi.get_cached_timely_angles("account", aid, period)
    return _render_timely_angles(angles, exists,
                                  poll_url=f"/account/{h}/timely-angles?period={period}")


@router.get("/account/{handle}/insights/export")
def account_insights_export(handle: str, r: Request, period: str = "7d", format: str = "json"):
    redir = require_login(r)
    if redir:
        return redir
    h = handle.lower().lstrip("@")
    ac = q("SELECT id, username FROM accounts WHERE username=%s", (h,))
    if not ac:
        return Response("Not found", status_code=404, media_type="text/plain")
    return _insight_export_response(
        "account", ac[0]["id"], f"@{ac[0]['username']}", period, format.lower(),
    )
