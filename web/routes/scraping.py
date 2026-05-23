"""web/routes/scraping.py — /trigger-scrape, /scrape-progress, /scrapes, /cancel-scrape/{sid}, /cookies, /queue-status"""
import os
import sys
import json
import html
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

from web.core import q, get_user, require_login, get_active_profile, active_profile_name
from web.ui import header_html, fmt, rel_time, _cookie_health, cookie_health_pill_html, HF, scrape_depth_picker_html

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vibechecx_config import COOKIE_DIR  # noqa: E402
from vibechecx_scrape_status import (  # noqa: E402
    current_for_user as ss_current,
    history_for_user as ss_history,
    is_live as ss_is_live,
)
from web.queue_worker import (  # noqa: E402
    enqueue, user_queue_row, position_in_queue, cancel_queue_row, queue_depth,
)

router = APIRouter()


@router.get("/scrapes", response_class=HTMLResponse)
def scrapes(r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    rows = ss_history(user["id"], limit=20)

    def _row(x):
        when = x["started_at"].strftime("%b %d %H:%M") if x.get("started_at") else "?"
        end = x["ended_at"].strftime("%H:%M") if x.get("ended_at") else "running"
        target = x.get("target_handle") or x.get("target_username") or ""
        status = x.get("status") or "?"
        status_class = {
            "completed": "bg-green-900/50 text-green-300",
            "failed": "bg-red-900/50 text-red-300",
        }.get(status, "bg-yellow-900/50 text-yellow-300")
        return (
            "<tr class='border-b border-gray-800 text-sm'>"
            f"<td class='py-2 px-3 text-gray-400'>#{x['id']}</td>"
            f"<td class='py-2 px-3'>{html.escape(str(x.get('session_type') or ''))}</td>"
            f"<td class='py-2 px-3 text-gray-300'>{html.escape(str(target))}</td>"
            f"<td class='py-2 px-3'>{when} → {end}</td>"
            f"<td class='py-2 px-3 text-center'>{x.get('tweets_collected') or 0}</td>"
            f"<td class='py-2 px-3'><span class='text-xs px-2 py-0.5 rounded {status_class}'>{html.escape(status)}</span></td>"
            "</tr>"
        )

    rws = "".join(_row(x) for x in rows) or (
        "<tr><td colspan='6' class='py-6 text-center text-gray-500 text-sm'>No scrapes yet. "
        "Run one from your dashboard.</td></tr>"
    )
    return header_html(0, active_profile_name(r), is_admin=user.get("is_admin", False)) + (
        "<h1 class='text-xl font-semibold mb-6'>Your Scrape Sessions</h1>"
        "<div class='bg-gray-900 rounded-xl border border-gray-800 overflow-hidden'>"
        "<table class='w-full'><thead><tr class='text-xs text-gray-500 uppercase bg-gray-800/50'>"
        "<th class='text-left p-3'>ID</th><th class='text-left p-3'>Type</th>"
        "<th class='text-left p-3'>Target</th><th class='text-left p-3'>Time</th>"
        "<th class='p-3'>Tweets</th><th class='p-3'>Status</th></tr></thead>"
        f"<tbody>{rws}</tbody></table></div>"
    ) + HF


_PHASE_LABELS = {
    "starting": "Starting",
    "navigating": "Loading X.com",
    "scrolling_posts": "Scrolling Posts tab",
    "scrolling_replies": "Scrolling Replies tab",
    "scrolling": "Scrolling",
    "storing": "Storing tweets",
    "collecting": "Collecting",
    "metric_patrol": "Refreshing metrics",
    "reply_mining": "Mining replies",
    "enriching": "AI enrichment",
    "done": "Done",
    "batch_scraping": "Batch scraping",
}
# Rough ETA per phase in seconds (used to set the user's expectation).
_PHASE_ETA = {
    "starting": 5, "navigating": 10, "scrolling_posts": 90, "scrolling_replies": 90,
    "scrolling": 60, "storing": 5, "metric_patrol": 30, "reply_mining": 120,
    "enriching": 25, "batch_scraping": 300,
}


def _phase_label(phase):
    if not phase:
        return "Working…"
    # phase may contain a handle like "collecting @foo".
    base = phase.split(" ")[0].split("@")[0].strip() if phase else ""
    label = _PHASE_LABELS.get(phase) or _PHASE_LABELS.get(base) or phase
    return label


def _tweets_per_minute(row):
    """Compute throughput from the row's started_at and tweets_collected."""
    started = row.get("started_at")
    n = row.get("tweets_collected") or 0
    if not started:
        return 0
    try:
        elapsed = (datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc)
                   ).total_seconds() / 60.0
    except (AttributeError, TypeError):
        return 0
    if elapsed <= 0.05:
        return 0
    return round(n / elapsed, 1)


def _heartbeat_age(row):
    hb = row.get("last_heartbeat_at")
    if not hb:
        return None
    try:
        return int((datetime.now(timezone.utc) - hb.replace(tzinfo=timezone.utc)).total_seconds())
    except (AttributeError, TypeError):
        return None


@router.post("/trigger-scrape", response_class=HTMLResponse)
async def trigger_scrape(r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    prof = get_active_profile(r)
    if not prof:
        return PlainTextResponse(
            '<p class="text-xs text-red-400">No active profile.</p>',
            media_type="text/html",
        )

    form = await r.form()
    try:
        days = int(form.get("days") or 30)
        if days not in (1, 7, 14, 30):
            days = 30
    except (ValueError, TypeError):
        days = 30

    # Enqueue — deduplicates if user already has an active row for this profile.
    enqueue(user["id"], prof["id"], days)

    # Immediately return a polling shell — /scrape-progress will show queue
    # position if pending, or live progress once the worker starts it.
    return HTMLResponse(
        '<div id="scrape-progress" class="fixed top-14 right-4 z-50 w-80 pointer-events-none" '
        'hx-get="/scrape-progress" hx-trigger="load delay:300ms, every 3s" '
        'hx-swap="outerHTML transition:true"></div>'
    )


@router.get("/queue-status", response_class=HTMLResponse)
def queue_status(r: Request):
    """Global queue state chip for the header. No auth required."""
    depth = queue_depth()
    running = depth["running"]
    waiting = depth["waiting"]
    if not running and not waiting:
        return HTMLResponse('<span id="queue-status-chip"></span>')
    parts = []
    if running:
        parts.append(f'<span class="text-cyan-300">🔄 {running} scraping</span>')
    if waiting:
        parts.append(f'<span class="text-gray-400">{waiting} waiting</span>')
    inner = ' · '.join(parts)
    return HTMLResponse(
        f'<span id="queue-status-chip" '
        f'class="text-[11px] px-2 py-0.5 rounded-full bg-gray-800 border border-gray-700 '
        f'flex items-center gap-1">{inner}</span>'
    )


@router.get("/scrape-progress", response_class=HTMLResponse)
def scrape_progress(r: Request):
    user = get_user(r)
    if not user:
        return PlainTextResponse("", media_type="text/html")

    # Check queue first — if pending, show position card.
    qrow = user_queue_row(user["id"])
    if qrow and qrow["status"] == "pending":
        pos = position_in_queue(qrow["id"])
        wait_min = max(1, (pos - 1) * 3)
        target = html.escape(qrow.get("target_handle") or "scope")
        cancel_btn = (
            f'<button hx-post="/cancel-queue/{qrow["id"]}" hx-target="#scrape-progress" '
            'hx-swap="outerHTML transition:true" '
            'class="text-[11px] px-2 py-1 rounded text-gray-500 hover:text-red-400 '
            'hover:bg-red-950/40 transition">Cancel</button>'
        )
        return HTMLResponse(
            '<div id="scrape-progress" '
            'class="fixed top-14 right-4 z-50 w-80 pointer-events-auto '
            'rounded-xl border border-gray-700 bg-gray-900/95 p-4 shadow-xl" '
            'hx-get="/scrape-progress" hx-trigger="every 5s" hx-swap="outerHTML transition:true">'
            '<div class="flex items-center gap-3 text-sm">'
            '<span class="text-2xl font-bold text-gray-300 leading-none tabular-nums">'
            f'#{pos}</span>'
            '<div class="flex-1">'
            f'<div class="text-gray-300 font-medium">In queue{" — " + target if target else ""}</div>'
            f'<div class="text-xs text-gray-500">~{wait_min} min wait · scrapes run one at a time</div>'
            '</div>'
            f'<div class="flex items-center gap-2">{cancel_btn}</div>'
            '</div>'
            '</div>'
        )

    row = ss_current(user["id"])
    if not row:
        return HTMLResponse(
            '<div id="scrape-progress" '
            'hx-get="/scrape-progress" hx-trigger="every 5s" hx-swap="outerHTML transition:true"></div>'
        )

    status = row.get("status") or ""
    phase = row.get("phase") or status
    target = row.get("target_handle") or ""
    tweets = row.get("tweets_collected") or 0
    pcur = row.get("progress_current") or 0
    ptot = row.get("progress_total") or 0
    pct = int(min(100, (pcur / ptot * 100) if ptot else 0))
    rate = _tweets_per_minute(row)
    hb_age = _heartbeat_age(row)

    if ss_is_live(row):
        # Stall detection: amber >30s, red border + banner >90s
        stall_color = "text-cyan-100"
        stall_banner = ""
        card_border = "border-cyan-800/60"
        card_bg = "bg-cyan-950/30"
        if hb_age is not None and hb_age > 90:
            stall_color = "text-red-300"
            card_border = "border-amber-700"
            card_bg = "bg-amber-950/40"
            stall_banner = (
                '<div class="text-xs text-amber-300 mb-1">'
                f'⚠ Stalled — last update {hb_age}s ago'
                '</div>'
            )
        elif hb_age is not None and hb_age > 30:
            stall_color = "text-amber-300"

        eta = _PHASE_ETA.get(phase.split(" ")[0].split("@")[0].strip() or phase, 60)
        cancel_btn = (
            f'<button hx-post="/cancel-scrape/{row["id"]}" hx-target="#scrape-progress" '
            'hx-swap="outerHTML transition:true" hx-confirm="Cancel this scrape?" '
            'class="text-[11px] px-2 py-1 rounded text-cyan-400/70 hover:text-red-400 hover:bg-red-950/40 transition">Cancel</button>'
        )
        # Phase X/4 — best-effort guess based on phase name
        phase_num = (
            1 if phase.startswith(("starting", "navigating", "scrolling", "collecting", "batch_scraping", "storing")) else
            2 if phase == "metric_patrol" else
            3 if phase == "reply_mining" else
            4 if phase == "enriching" else 1
        )
        return HTMLResponse(
            f'<div id="scrape-progress" class="fixed top-14 right-4 z-50 w-80 pointer-events-auto rounded-xl border {card_border} {card_bg} p-4 space-y-2 shadow-xl" '
            'hx-get="/scrape-progress" hx-trigger="every 2s" hx-swap="outerHTML transition:true">'
            f'{stall_banner}'
            '<div class="flex items-center gap-3 text-sm">'
            '<span class="w-2.5 h-2.5 rounded-full bg-cyan-400 animate-pulse"></span>'
            f'<span class="text-cyan-200 font-medium">{html.escape(_phase_label(phase))}'
            + (f' · @{html.escape(target)}' if target else '') + '</span>'
            f'<span class="text-cyan-500/80 text-xs">phase {phase_num}/4</span>'
            f'<span class="ml-auto text-xs text-cyan-500/80">~{eta}s</span>'
            '</div>'
            '<div class="h-1 bg-cyan-950 rounded-full overflow-hidden">'
            f'<div class="h-full bg-cyan-400 transition-all duration-700" style="width:{pct}%"></div>'
            '</div>'
            '<div class="grid grid-cols-3 gap-2 text-[11px] text-cyan-300/70">'
            f'<div><span class="text-cyan-100 font-mono">{rate}</span> tweets/min</div>'
            f'<div><span class="text-cyan-100 font-mono">{tweets}</span> collected</div>'
            f'<div title="seconds since last progress update"><span class="{stall_color} font-mono">{hb_age if hb_age is not None else "—"}{("s" if hb_age is not None else "")}</span> since update</div>'
            '</div>'
            f'<div class="flex justify-end pt-1">{cancel_btn}</div>'
            '</div>'
        )

    if status == "completed":
        # Fire scrape-complete event on the client to drive auto-refresh + toast.
        # No `hx-trigger` on the result: once a session is terminal we stop
        # polling so the toast doesn't re-fire on every page load. A new scrape
        # replaces this div via the /trigger-scrape response. Client-side dedup
        # in header_html ensures even cross-page renders don't double-fire.
        started_at = ""
        try:
            sa = row.get("started_at")
            if sa:
                started_at = sa.isoformat() if hasattr(sa, "isoformat") else str(sa)
        except Exception:
            pass
        trigger_detail = json.dumps({
            "session_id": row.get("id"),
            "handle": target or "",
            "new_tweets": tweets,
            "started_at": started_at,
        })
        resp = HTMLResponse(
            '<div id="scrape-progress" '
            'class="fixed top-14 right-4 z-50 w-80 pointer-events-auto '
            'flex items-center gap-2 text-xs bg-emerald-900/80 border border-emerald-700 '
            'rounded-xl px-3 py-2.5 shadow-xl">'
            '<div class="inline-block w-2 h-2 rounded-full bg-emerald-400 flex-shrink-0"></div>'
            f'<span class="text-emerald-300 font-medium">Done · {tweets} tweets</span>'
            f'<span class="text-gray-500 flex-1 truncate">{html.escape(target)}</span>'
            f'<button onclick="this.closest(\'[id=scrape-progress]\').remove()" '
            f'class="text-gray-400 hover:text-gray-300 leading-none text-base px-1 flex-shrink-0">×</button>'
            '</div>'
        )
        resp.headers["HX-Trigger"] = json.dumps({"scrape-complete": json.loads(trigger_detail)})
        return resp
    if status == "failed":
        err = (row.get("error_log") or "scrape failed")[:200]
        trigger_detail = json.dumps({
            "session_id": row.get("id"),
            "error": err,
        })
        sid = row.get("id", "")
        resp = HTMLResponse(
            f'<div id="scrape-progress" '
            f'class="fixed top-14 right-4 z-50 w-80 pointer-events-auto '
            f'flex items-center gap-2 text-xs bg-red-900/80 border border-red-700 '
            f'rounded-xl px-3 py-2.5 shadow-xl" '
            f'x-data x-init="setTimeout(()=>$el.remove(),8000)">'
            f'<div class="inline-block w-2 h-2 rounded-full bg-red-400 flex-shrink-0"></div>'
            f'<span class="text-red-300 font-medium flex-shrink-0">Failed</span>'
            f'<span class="text-gray-400 flex-1 truncate text-[10px]">{html.escape(err)}</span>'
            f'<a href="/scrapes" class="text-gray-500 hover:text-gray-300 text-[10px] mr-1 flex-shrink-0">log</a>'
            f'<button onclick="this.closest(\'[id=scrape-progress]\').remove()" '
            f'class="text-gray-400 hover:text-gray-300 leading-none text-base px-1 flex-shrink-0">×</button>'
            f'</div>'
        )
        resp.headers["HX-Trigger"] = json.dumps({"scrape-failed": json.loads(trigger_detail)})
        return resp
    return HTMLResponse(
        '<div id="scrape-progress" '
        'hx-get="/scrape-progress" hx-trigger="every 5s" hx-swap="outerHTML transition:true"></div>'
    )


def _kill_pid_softly(pid):
    """SIGTERM if the pid is alive and looks like ours. Returns True if signalled."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 15)  # SIGTERM
        return True
    except (OSError, ValueError, TypeError):
        return False


@router.post("/cancel-scrape/{sid}", response_class=HTMLResponse)
def cancel_scrape(sid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    row = q("SELECT * FROM scrape_sessions WHERE id=%s AND user_id=%s", (sid, user["id"]))
    if not row:
        return PlainTextResponse('<p class="text-xs text-red-400">Not found.</p>',
                                  media_type="text/html")
    row = row[0]
    if row.get("status") not in ("starting", "running", "scrolling", "batch_scraping",
                                  "patrol"):
        return PlainTextResponse('<p class="text-xs text-gray-500">Already finished.</p>',
                                  media_type="text/html")
    _kill_pid_softly(row.get("pid"))
    q(
        "UPDATE scrape_sessions SET status='cancelled', ended_at=NOW(), "
        "error_log=COALESCE(error_log,'') || 'cancelled by user' WHERE id=%s",
        (sid,),
    )
    return (
        '<div id="scrape-progress" '
        'class="fixed top-14 right-4 z-50 w-80 pointer-events-auto '
        'flex items-center gap-2 text-xs bg-gray-800/90 border border-gray-700 '
        'rounded-xl px-3 py-2.5 shadow-xl" '
        'hx-get="/scrape-progress" hx-trigger="every 5s" hx-swap="outerHTML transition:true">'
        '<span class="text-gray-400">Cancelled.</span></div>'
    )


@router.post("/cancel-queue/{qid}", response_class=HTMLResponse)
def cancel_queue(qid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    cancel_queue_row(qid, user["id"])
    return HTMLResponse(
        '<div id="scrape-progress" '
        'hx-get="/scrape-progress" hx-trigger="every 5s" hx-swap="outerHTML transition:true">'
        '</div>'
    )


@router.get("/cookies", response_class=HTMLResponse)
def cookies_page(r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    health = _cookie_health()
    ok_count = sum(1 for h in health if h["ok"])
    rows = ""
    for h in health:
        dot = "bg-emerald-400" if h["ok"] else "bg-red-400"
        age = f'{h["age_days"]:.1f}d ago' if h["age_days"] is not None else "—"
        rows += (
            '<tr class="border-b border-gray-800 text-sm">'
            f'<td class="py-2 px-3"><span class="inline-block w-2 h-2 rounded-full {dot} mr-2"></span>'
            f'<code class="text-gray-300">{html.escape(h["file"])}</code></td>'
            f'<td class="py-2 px-3 text-gray-400">{age}</td>'
            f'<td class="py-2 px-3 text-gray-400">{h["size"]} bytes</td>'
            f'<td class="py-2 px-3 text-gray-500">{html.escape(h["reason"] or "ok")}</td>'
            '</tr>'
        )
    return header_html(0, active_profile_name(r), is_admin=user.get("is_admin", False)) + (
        '<h1 class="text-xl font-semibold mb-2">Cookie health</h1>'
        f'<p class="text-sm text-gray-500 mb-4">{ok_count}/3 cookie files usable. '
        'If 0/3 are usable, scrapes will fail with auth errors.</p>'
        '<div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden mb-6">'
        '<table class="w-full"><thead><tr class="text-xs text-gray-500 uppercase bg-gray-800/50">'
        '<th class="text-left p-3">File</th><th class="text-left p-3">Updated</th>'
        '<th class="text-left p-3">Size</th><th class="text-left p-3">Status</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
        '<div class="bg-gray-900 rounded-xl border border-gray-800 p-5 text-sm">'
        '<h2 class="font-semibold mb-2">How to refresh</h2>'
        '<p class="text-gray-500 mb-3">From the shell:</p>'
        '<pre class="bg-gray-950 border border-gray-800 rounded p-3 text-xs text-gray-300 overflow-x-auto">'
        'cd ~/services/vibechecx\n./vibechecx-setup-cookies.sh\n'
        '</pre>'
        '<p class="text-gray-500 mt-3 text-xs">This opens a Playwright browser, lets you log into X manually, '
        'and saves a fresh cookie file. Run once per cookie slot you want to use.</p>'
        '</div>'
    ) + HF
