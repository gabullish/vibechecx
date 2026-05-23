"""web/routes/insights.py — /account/{handle}/generate-insights, /account/{handle}/insights, export, library"""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

import vibechecx_insights as vi  # noqa: E402

from web.core import q, get_user, require_login  # noqa: E402
from web.render_insights import _render_insights, _insight_export_response, _render_timely_angles, _ai_error_card  # noqa: E402
from web.ui import rel_time  # noqa: E402

router = APIRouter()


@router.post("/account/{handle}/generate-insights", response_class=HTMLResponse)
def account_generate_insights(handle: str, r: Request, period: str = "7d"):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    h = handle.lower().lstrip("@")
    ac = q("SELECT id FROM accounts WHERE username=%s", (h,))
    if not ac:
        return "<span class='text-red-400'>Account not found</span>"
    insight, *_ = vi.cached_insights(
        "account", ac[0]["id"], period, force=True,
        user_id=user["id"], display_name=f"@{h} · {period}",
    )
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


@router.get("/insights/library", response_class=HTMLResponse)
def insights_library(r: Request):
    """HTMX fragment: list of current user's cached insights."""
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    rows = q(
        "SELECT id, display_name, scope_type, scope_id, period, provider, generated_at "
        "FROM insights_cache WHERE user_id=%s "
        "ORDER BY generated_at DESC LIMIT 50",
        (user["id"],),
    )
    if not rows:
        return "<p class='text-gray-500 text-xs text-center py-4'>No insights generated yet.</p>"

    def _row(row):
        name = row.get("display_name") or f"{row['scope_type']} #{row['scope_id']} · {row['period']}"
        provider = row.get("provider") or "?"
        age = rel_time(row["generated_at"]) if row.get("generated_at") else "?"
        dl_url = f"/insights/library/download/{row['id']}?format=md"
        dl_json_url = f"/insights/library/download/{row['id']}?format=json"
        return (
            "<tr class='border-b border-gray-800 text-sm hover:bg-gray-800/30'>"
            f"<td class='py-2 px-3 text-gray-200'>{row['scope_type']}</td>"
            f"<td class='py-2 px-3 font-medium'>{row.get('display_name') or '—'}</td>"
            f"<td class='py-2 px-3 text-gray-400'>{row['period']}</td>"
            f"<td class='py-2 px-3 text-gray-400'>{age}</td>"
            f"<td class='py-2 px-3 text-gray-500 text-xs'>{provider[:20]}</td>"
            f"<td class='py-2 px-3'>"
            f"<a href='{dl_url}' class='text-emerald-400 hover:underline text-xs mr-2'>↓ MD</a>"
            f"<a href='{dl_json_url}' class='text-blue-400 hover:underline text-xs'>↓ JSON</a>"
            f"</td></tr>"
        )

    rows_html = "".join(_row(r2) for r2 in rows)
    return (
        "<div class='overflow-x-auto rounded-lg border border-gray-800'>"
        "<table class='w-full text-sm'>"
        "<thead><tr class='text-[11px] text-gray-500 uppercase bg-gray-900/80 border-b border-gray-800'>"
        "<th class='py-2 px-3 text-left'>Type</th>"
        "<th class='py-2 px-3 text-left'>Name</th>"
        "<th class='py-2 px-3 text-left'>Period</th>"
        "<th class='py-2 px-3 text-left'>Generated</th>"
        "<th class='py-2 px-3 text-left'>Provider</th>"
        "<th class='py-2 px-3 text-left'>Download</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table></div>"
    )


@router.get("/insights/library/download/{cache_id}")
def insights_library_download(cache_id: int, r: Request, format: str = "md"):
    """Download a cached insight by its ID. Verifies ownership (or admin)."""
    from fastapi.responses import Response
    import json as _json
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    rows = q(
        "SELECT id, scope_type, scope_id, period, insights, provider, display_name, user_id "
        "FROM insights_cache WHERE id=%s",
        (cache_id,),
    )
    if not rows:
        return Response("Not found", status_code=404, media_type="text/plain")
    row = rows[0]
    if row["user_id"] != user["id"] and not user.get("is_admin"):
        return Response("Access denied", status_code=403, media_type="text/plain")

    display = row.get("display_name") or f"{row['scope_type']} · {row['period']}"
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", display)[:40]
    filename_base = f"vibechecx_{safe_slug}_{row['period']}"
    insights_data = row["insights"] if isinstance(row["insights"], dict) else _json.loads(row["insights"])
    provider = row.get("provider") or ""

    if format == "md":
        from web.render_insights import _insight_to_markdown
        body = _insight_to_markdown(insights_data, display, row["period"], provider)
        return Response(
            content=body, media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.md"'},
        )
    payload = {
        "scope_type": row["scope_type"], "scope_display": display,
        "period": row["period"], "provider": provider, "insight": insights_data,
    }
    return Response(
        content=_json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.json"'},
    )
