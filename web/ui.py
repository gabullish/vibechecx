"""web/ui.py — HTML/CSS/JS rendering helpers."""
import os
import time
import html
from datetime import datetime, timezone

from vibechecx_config import COOKIE_DIR  # noqa: E402
from web.core import q  # noqa: E402

# ── Templates / fragments ────────────────────────────────────────────

AH = (
    '<!DOCTYPE html><html lang="en" class="dark"><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
    '<title>VibeChecx</title><script src="https://cdn.tailwindcss.com"></script>'
    '</head><body class="bg-gray-950 text-gray-100 min-h-screen flex items-center '
    'justify-center"><main class="max-w-sm w-full px-6">'
)
AF = '<p class="text-center text-xs text-gray-400 mt-6">VibeChecx</p></main></body></html>'
HF = "</main></body></html>"


def header_html(days=0, active_name="", is_admin=False, show_insights=True):
    ds = f"?days={days}" if days else ""
    ab = (
        f'<a href="/profiles" class="text-xs text-emerald-400 bg-gray-800 px-2 py-0.5 '
        f'rounded hover:bg-gray-700 transition">{html.escape(active_name)}</a>'
        if active_name
        else '<span class="text-xs text-gray-400">no active profile</span>'
    )
    cookie_pill = cookie_health_pill_html()
    return (
        '<!DOCTYPE html><html lang="en" class="dark"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '<title>VibeChecx</title>'
        '<script src="https://unpkg.com/htmx.org@2.0.4"></script>'
        '<script src="https://unpkg.com/hyperscript.org@0.9.12"></script>'
        '<script src="https://cdn.tailwindcss.com"></script>'
        '<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>'
        '<script>document.addEventListener("htmx:afterSwap",function(e){window.Alpine&&Alpine.initTree(e.detail.elt)})</script>'
        '<style>[x-cloak]{display:none!important}'
        # Legacy short-tip class (data-tip attribute, single-line). Kept for
        # backwards compat with spots that use title= or data-tip=.
        '.tooltip{position:relative;cursor:help}'
        '.tooltip:hover:after{content:attr(data-tip);position:absolute;bottom:100%;'
        'left:50%;transform:translateX(-50%);background:#1f2937;color:#d1d5db;'
        'font-size:.75rem;padding:4px 8px;border-radius:6px;white-space:nowrap;'
        'z-index:10;border:1px solid #374151}'
        # New tip() system — wraps prose with a dotted underline + (?) icon,
        # opens a wrapping tooltip on hover/focus, accessible via keyboard
        # (tabindex), works on mobile via tap (focus = tap on iOS/Android).
        # Use sparingly: only for jargon/formulas/non-obvious terms.
        '.tip{position:relative;display:inline-flex;align-items:center;gap:2px;cursor:help;'
        'border-bottom:1px dotted rgba(156,163,175,.4);outline:none}'
        '.tip:focus,.tip:focus-visible{border-bottom-color:rgba(52,211,153,.7)}'
        '.tip-icon{display:inline-flex;align-items:center;justify-content:center;'
        'width:13px;height:13px;border-radius:50%;background:rgba(75,85,99,.4);'
        'color:rgba(209,213,219,.7);font-size:9px;font-weight:600;line-height:1;margin-left:2px;flex-shrink:0}'
        '.tip:hover>.tip-body,.tip:focus>.tip-body,.tip:focus-within>.tip-body{'
        'opacity:1;visibility:visible;pointer-events:auto;transform:translateX(-50%) translateY(-2px)}'
        '.tip-body{position:absolute;bottom:calc(100% + 4px);left:50%;'
        'transform:translateX(-50%) translateY(4px);width:max-content;max-width:280px;'
        'padding:8px 10px;background:#030712;color:#e5e7eb;font-size:11px;line-height:1.5;'
        'text-align:left;border:1px solid #374151;border-radius:6px;'
        'box-shadow:0 4px 14px rgba(0,0,0,.5);z-index:60;'
        'opacity:0;visibility:hidden;pointer-events:none;'
        'transition:opacity .15s,visibility .15s,transform .15s;white-space:normal;font-weight:400}'
        '.tip-body::after{content:"";position:absolute;top:100%;left:50%;'
        'transform:translateX(-50%);border:5px solid transparent;border-top-color:#374151}'
        '.tip-body strong{color:#fff;font-weight:600}'
        '.tip-body code{background:rgba(31,41,55,.7);padding:1px 5px;border-radius:3px;'
        'font-size:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}'
        # §10.1 universal htmx loading affordance — spinner suffix on any
        # element with hx-get/post/patch/delete/put during its in-flight request.
        '[hx-get],[hx-post],[hx-patch],[hx-delete],[hx-put]{position:relative;transition:opacity .12s,background-color .12s,filter .12s}'
        '.htmx-request{opacity:.55;pointer-events:none;cursor:wait;filter:saturate(.6)}'
        '.htmx-request::after{content:"";display:inline-block;width:.7em;height:.7em;'
        'margin-left:.45em;vertical-align:-1px;border:2px solid currentColor;'
        'border-top-color:transparent;border-radius:9999px;animation:hx-spin .7s linear infinite}'
        '@keyframes hx-spin{to{transform:rotate(360deg)}}'
        'form.htmx-request button[type=submit]{opacity:.55;pointer-events:none}'
        'form.htmx-request button[type=submit]::after{content:"";display:inline-block;'
        'width:.7em;height:.7em;margin-left:.45em;border:2px solid currentColor;'
        'border-top-color:transparent;border-radius:9999px;animation:hx-spin .7s linear infinite;vertical-align:-1px}'
        '.hx-flash-error{animation:hx-err 1.6s ease-out 1}'
        '.hx-flash-ok{animation:hx-ok 1.4s ease-out 1}'
        '@keyframes hx-err{0%,30%{background-color:rgb(127 29 29 / .55);box-shadow:inset 0 0 0 1px rgb(248 113 113)}100%{background-color:transparent;box-shadow:none}}'
        '@keyframes hx-ok{0%,30%{background-color:rgb(6 78 59 / .55);box-shadow:inset 0 0 0 1px rgb(52 211 153)}100%{background-color:transparent;box-shadow:none}}'
        '[data-noaffordance].htmx-request::after{content:none}'
        '[data-noaffordance].htmx-request{opacity:1;filter:none}'
        '@keyframes hx-toast-in{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}'
        # Smooth scrape-progress swap. With hx-swap="outerHTML transition:true",
        # the browser captures old + new states and cross-fades them — no
        # destroy-then-rebuild flash. Naming the view-transition lets the
        # browser pair the old and new elements as the SAME logical thing.
        '#scrape-progress{view-transition-name:scrape-progress}'
        '::view-transition-old(scrape-progress),'
        '::view-transition-new(scrape-progress){animation-duration:160ms}'
        '</style></head>'
        '<body class="bg-gray-950 text-gray-100 min-h-screen font-sans antialiased">'
        # §10.9G toast stack — fixed bottom-right, drained by JS event listeners.
        '<div id="toast-stack" class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 pointer-events-none w-80"></div>'
        '<script>'
        # universal flash on response / response error
        'document.body.addEventListener("htmx:responseError",function(e){'
        'var t=e.detail.elt;if(!t)return;'
        't.classList.add("hx-flash-error");'
        'setTimeout(function(){t.classList.remove("hx-flash-error")},1700)});'
        'document.body.addEventListener("htmx:afterRequest",function(e){'
        'if(!e.detail.successful)return;'
        'var tgt=e.detail.target;if(!tgt||tgt===document.body)return;'
        'tgt.classList.add("hx-flash-ok");'
        'setTimeout(function(){tgt.classList.remove("hx-flash-ok")},1500)});'
        # toast() helper
        'function toast(msg,kind,action){'
        'var cls=kind==="ok"?"border-emerald-700 bg-emerald-950/80 text-emerald-200"'
        ':"border-red-700 bg-red-950/80 text-red-200";'
        'var a=action?(\' <a href="\'+action.href+\'" class="ml-2 underline hover:no-underline">\'+action.label+\'</a>\'):"";'
        'var el=document.createElement("div");'
        'el.className="pointer-events-auto rounded-lg border px-3 py-2 text-sm shadow-lg backdrop-blur "+cls;'
        'el.style.animation="hx-toast-in .2s ease-out";'
        'el.innerHTML=msg+a;'
        'document.getElementById("toast-stack").appendChild(el);'
        'setTimeout(function(){el.style.opacity="0";el.style.transition="opacity .4s";'
        'setTimeout(function(){el.remove()},400)},kind==="err"?9000:5000)}'
        # HX-Trigger event listeners: dedup by session_id so re-polled
        # terminal states don't stack toasts. Persist in sessionStorage so
        # navigating to a different page doesn't re-fire the same event.
        'function _alreadyHandled(kind,sid){'
        'if(!sid)return false;'
        'var k="vibechecx_scrape_"+kind;'
        'try{if(sessionStorage.getItem(k)==String(sid))return true;'
        'sessionStorage.setItem(k,String(sid));return false}catch(_){return false}}'
        'document.body.addEventListener("scrape-complete",function(e){'
        'var d=e.detail||{};'
        'if(_alreadyHandled("complete",d.session_id)){'
        'var _ep=document.getElementById("scrape-progress");'
        'if(_ep)_ep.remove();'
        'return;}'
        'var label="Scrape complete";'
        'if(d.handle)label+=" · @"+d.handle;'
        'if(d.new_tweets!==undefined)label+=" · "+d.new_tweets+" new";'
        'var act=null;'
        'if(d.handle){act={href:"/account/"+d.handle,label:"View"}}'
        'toast(label,"ok",act);'
        # Hard reload (not htmx swap) — swapping the full HTML response into
        # <main> would nest the response inside the current page and produce
        # a duplicate header. The 1500ms delay lets the user read the toast;
        # the sessionStorage dedup prevents the same event from re-firing
        # on the reloaded page.
        'setTimeout(function(){location.reload()},1500)});'
        'document.body.addEventListener("scrape-failed",function(e){'
        'var d=e.detail||{};'
        'if(_alreadyHandled("failed",d.session_id))return;'
        'var msg="Scrape failed";if(d.error)msg+=": "+String(d.error).slice(0,120);'
        'var act=d.session_id?{href:"/scrapes",label:"View log"}:null;'
        'toast(msg,"err",act)});'
        '</script>'
        '<nav class="border-b border-gray-800 px-6 py-4">'
        '<div class="mx-auto w-full max-w-6xl xl:max-w-7xl 2xl:max-w-[1600px] flex items-center gap-3">'
        '<a href="/" class="text-xl font-bold bg-gradient-to-r from-emerald-400 to-cyan-400 '
        'bg-clip-text text-transparent">VibeChecx</a>'
        f'{ab}{cookie_pill}'
        '<div class="ml-auto flex gap-4 text-sm">'
        f'<a href="/{ds}" class="hover:text-white transition">Dashboard</a>'
        f'<a href="/posts{ds}" class="hover:text-white transition">Posts</a>'
        f'<a href="/tags{ds}" class="hover:text-white transition">Tags</a>'
        f'<a href="/leaderboard{ds}" class="hover:text-white transition">Leaderboard</a>'
        + (f'<a href="/profile{ds}" class="hover:text-white transition">Insights</a>' if show_insights else '')
        + f'<a href="/profiles" class="hover:text-white transition">Workspaces</a>'
        + ('<a href="/admin" class="hover:text-white transition">Admin</a>' if is_admin else '')
        + '<a href="/logout" class="text-gray-400 hover:text-gray-400 transition">logout</a>'
        '<span id="queue-status-chip" '
        'hx-get="/queue-status" hx-trigger="load, every 8s" hx-swap="outerHTML transition:true">'
        '</span>'
        '</div></div></nav>'
        '<div id="scrape-progress" hx-get="/scrape-progress" '
        'hx-trigger="load delay:0.5s, every 3s" hx-swap="outerHTML transition:true"></div>'
        '<main class="mx-auto w-full px-4 sm:px-6 lg:px-8 max-w-6xl xl:max-w-7xl 2xl:max-w-[1600px] py-8">'
    )


def scrape_depth_picker_html(hx_target="#trigger-status", submit_label="↻ Scrape",
                              compact=False):
    """Inline depth-picker form replacing the old single scrape button.

    Renders 24h / 7d / 14d / 30d radio chips + a submit button. Posts to
    /trigger-scrape with a `days` field; the backend passes --days to the
    coordinator subprocess.
    """
    pad = "px-1.5 py-0.5 text-[10px]" if compact else "px-2 py-1 text-xs"
    btn_cls = (
        "px-2 py-0.5 text-[10px]" if compact else "px-3 py-1 text-xs"
    )
    gap = "gap-1" if compact else "gap-1.5"

    def _chip(value, label, checked=""):
        return (
            f'<label class="cursor-pointer">'
            f'<input type="radio" name="days" value="{value}" class="sr-only peer" {checked}>'
            f'<span class="{pad} rounded border border-gray-700 text-gray-400 '
            f'peer-checked:border-cyan-500 peer-checked:text-cyan-300 peer-checked:bg-cyan-950/40 '
            f'hover:border-gray-500 transition select-none inline-block">{label}</span>'
            f'</label>'
        )

    lbl_cls = "text-[10px] text-gray-500 mr-0.5" if compact else "text-xs text-gray-500 mr-1"
    return (
        f'<form hx-post="/trigger-scrape" hx-target="{hx_target}" '
        f'hx-swap="outerHTML transition:true" '
        f'class="flex items-center flex-wrap {gap}">'
        + f'<span class="{lbl_cls}">how far back:</span>'
        + _chip("1", "24h")
        + _chip("7", "7d", "checked")
        + _chip("14", "14d")
        + _chip("30", "30d")
        + f'<button type="submit" class="{btn_cls} rounded bg-cyan-900/40 border '
        f'border-cyan-800/60 text-cyan-300 hover:bg-cyan-800/50 transition">'
        f'{html.escape(submit_label)}</button>'
        f'</form>'
    )


def tweet_link(tweet_id, content, max_len=80, sn=""):
    snippet = content[:max_len] + ("..." if len(content) > max_len else "")
    sn = sn or "i"
    return (
        f'<a href="https://x.com/{html.escape(sn)}/status/{html.escape(str(tweet_id))}" '
        f'target="_blank" class="hover:text-emerald-400 transition group">'
        f'<span class="group-hover:underline">{html.escape(snippet)}</span>'
        f'<span class="text-gray-400 text-xs opacity-0 group-hover:opacity-100 transition">↗</span>'
        f'</a>'
    )


def tag_chip(t, current_path="/", current_qs=None, active=False):
    """Tag chip that preserves the current page and other query params."""
    current_qs = dict(current_qs or {})
    current_qs.pop("tag", None)
    if active:
        # Clicking the active tag clears it.
        qs_pairs = [(k, v) for k, v in current_qs.items() if v not in (None, "", 0)]
    else:
        qs_pairs = [(k, v) for k, v in current_qs.items() if v not in (None, "", 0)] + [("tag", t)]
    qs = "&".join(f"{k}={html.escape(str(v))}" for k, v in qs_pairs)
    href = current_path + (f"?{qs}" if qs else "")
    style = (
        "bg-emerald-700 text-white border-emerald-500"
        if active
        else "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white"
    )
    return (
        f'<a href="{href}" class="inline-block text-xs px-2 py-0.5 rounded-full '
        f'border border-gray-700 transition cursor-pointer {style}">'
        f'#{html.escape(t)}</a>'
    )


def type_pill(t):
    m = {
        "original": "bg-emerald-900/50 text-emerald-300",
        "retweet": "bg-gray-800 text-gray-400",
        "reply": "bg-cyan-900/50 text-cyan-300",
        "quote": "bg-yellow-900/50 text-yellow-300",
    }
    return (
        f'<span class="text-xs px-2 py-0.5 rounded-full border '
        f'{m.get(t, "bg-gray-800 text-gray-400")}">{html.escape(t)}</span>'
    )


def period_clause(days, table_alias="t"):
    p = f"{table_alias}." if table_alias else ""
    mapping = {
        1: f" AND {p}created_at>=NOW()-INTERVAL'24 hours'",
        7: f" AND {p}created_at>=NOW()-INTERVAL'7 days'",
        14: f" AND {p}created_at>=NOW()-INTERVAL'14 days'",
        30: f" AND {p}created_at>=NOW()-INTERVAL'30 days'",
    }
    return mapping.get(int(days or 0), "")


def period_buttons(current_days, current_path="/", current_qs=None):
    """Day-range buttons that preserve other query params."""
    current_qs = dict(current_qs or {})
    current_qs.pop("days", None)
    buttons = []
    for v, label in [(1, "24h"), (7, "7d"), (14, "14d"), (30, "30d"), (0, "All")]:
        qs_pairs = [(k, val) for k, val in current_qs.items() if val not in (None, "", 0)]
        if v:
            qs_pairs.append(("days", v))
        qs = "&".join(f"{k}={html.escape(str(val))}" for k, val in qs_pairs)
        href = current_path + (f"?{qs}" if qs else "")
        style = (
            "bg-emerald-700 text-white"
            if int(current_days or 0) == v
            else "bg-gray-800 text-gray-400 hover:text-white"
        )
        buttons.append(
            f'<a href="{href}" class="px-2.5 py-1 rounded text-xs transition {style}">{label}</a>'
        )
    return "".join(buttons)


_TIP_ALLOWED_TAGS = ("strong", "code", "em", "br")


def tip(label_html: str, tip_text: str, *, with_icon: bool = True) -> str:
    """Wrap a label with a hover/focus tooltip.

    label_html: the visible text (can already be HTML — won't be re-escaped).
    tip_text:   plain text shown in the tooltip. Supports a small inline
                vocabulary: <strong>, <code>, <em>, <br>. Newlines in
                tip_text are converted to <br>.

    Pure CSS, no JS. Accessible: tabindex=0 + ARIA role=tooltip on the body;
    visible on hover, focus, OR focus-within (so tapping on mobile works).

    Usage:
      tip("Q-score", "Composite engagement-depth score 0–100. Components: ...")
      tip("4.15%", "Engagement rate (likes / views). Web3 norm is 1–2%.")
    """
    safe = html.escape(tip_text).replace("\n", "<br>")
    # Allow a small inline vocabulary
    for t in _TIP_ALLOWED_TAGS:
        safe = safe.replace(f"&lt;{t}&gt;", f"<{t}>").replace(f"&lt;/{t}&gt;", f"</{t}>")
    icon = '<span class="tip-icon" aria-hidden="true">?</span>' if with_icon else ''
    return (
        f'<span class="tip" tabindex="0" aria-label="More info">'
        f'{label_html}{icon}'
        f'<span class="tip-body" role="tooltip">{safe}</span>'
        f'</span>'
    )


def fmt(n):
    """Format a number with commas for readability."""
    if n is None:
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n == 0 or n == int(n):
        return f"{int(n):,}"
    if abs(n) < 10:
        return f"{n:.2f}"
    return f"{n:,.1f}"


def fmt_compact(n):
    """Compact-format a big number for headline tiles: 12.4M, 1.2k, 999.

    Use for text-3xl numeric headlines where a 9-digit comma-formatted value
    would force the card to overflow. Pair with `title=` showing the full
    comma-formatted value for hover precision.
    """
    if n is None:
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 1000:
        return f"{sign}{int(n)}" if n == int(n) else f"{sign}{n:.1f}"
    for unit, threshold in [("k", 1_000), ("M", 1_000_000),
                             ("B", 1_000_000_000), ("T", 1_000_000_000_000)]:
        if n < threshold * 1000:
            v = n / threshold
            return f"{sign}{v:.0f}{unit}" if v >= 100 else f"{sign}{v:.1f}{unit}"
    return f"{sign}{n / 1e15:.1f}P"


def rel_time(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        d = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return str(value)[:10]
    s = int((datetime.now(timezone.utc) - d).total_seconds())
    d_utc = d.astimezone(timezone.utc)
    date_str = d_utc.strftime("%b %d")
    if s < 60:
        rel = "just now"
    elif s < 3600:
        rel = f"{s // 60}m ago"
    elif s < 86400:
        rel = f"{s // 3600}h ago"
    elif s < 604800:
        rel = f"{s // 86400}d ago"
    else:
        rel = d_utc.strftime("%H:%M UTC")
    return f"{date_str} ({rel})"


def error_page_html(title, body):
    return (
        header_html(0)
        + '<div class="text-center py-12">'
        + f'<div class="text-4xl mb-3">⚠️</div>'
        + f'<h2 class="text-xl font-semibold text-gray-300 mb-2">{html.escape(title)}</h2>'
        + f'<p class="text-sm text-gray-500">{body}</p>'
        + "</div>" + HF
    )


def _sparkline_svg(points, w=72, h=18):
    """Inline SVG sparkline. `points` is a list of numbers; rendered with currentColor."""
    if not points or max(points) == 0:
        return '<span class="text-gray-700 text-[10px]">—</span>'
    mx = max(points) or 1
    n = len(points)
    coords = " ".join(
        f"{(i * w / max(1, n - 1)):.1f},{(h - (p / mx) * h):.1f}" for i, p in enumerate(points)
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="inline-block align-middle">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" points="{coords}"/>'
        '</svg>'
    )


def _vibe(scope_type, scope_id, period_label="7d"):
    """Pure-SQL vibe score for cohort/account, returning (0-100 int, str).

    Replaces the deleted vibechecx_scoring module. The score is a weighted
    blend of engagement rate (likes/views), activity volume, reach
    (log-scaled views) — plus member-activation fraction for cohorts.
    The description names the tier and quotes the underlying numbers.
    """
    import logging
    import math
    logger = logging.getLogger("vibechecx.web")
    days_map = {"24h": 1, "7d": 7, "14d": 14, "30d": 30, "1y": 365, "all": 36500}
    days = days_map.get(period_label, 7)
    try:
        if scope_type == "cohort":
            rows = q(
                f"""
                SELECT
                  COUNT(DISTINCT t.author_account_id) AS active_authors,
                  COUNT(*) FILTER (WHERE NOT t.is_retweet) AS posts,
                  COALESCE(SUM(t.likes) FILTER (WHERE NOT t.is_retweet), 0)::bigint AS likes,
                  COALESCE(SUM(t.views) FILTER (WHERE NOT t.is_retweet), 0)::bigint AS views,
                  (SELECT COUNT(*) FROM cohort_members WHERE cohort_id=%s) AS total_members
                FROM cohort_members cm
                LEFT JOIN tweets t ON t.author_account_id = cm.account_id
                                   AND t.created_at >= NOW() - INTERVAL '{days} days'
                WHERE cm.cohort_id = %s
                """,
                (scope_id, scope_id),
            )
        else:
            rows = q(
                f"""
                SELECT
                  COUNT(*) FILTER (WHERE NOT is_retweet) AS posts,
                  COALESCE(SUM(likes) FILTER (WHERE NOT is_retweet), 0)::bigint AS likes,
                  COALESCE(SUM(views) FILTER (WHERE NOT is_retweet), 0)::bigint AS views,
                  0 AS active_authors,
                  1 AS total_members
                FROM tweets WHERE author_account_id = %s
                  AND created_at >= NOW() - INTERVAL '{days} days'
                """,
                (scope_id,),
            )
    except Exception:
        logger.exception("vibe query failed for %s %s", scope_type, scope_id)
        return 0, "no data"
    if not rows:
        return 0, "no data"
    r = rows[0]
    posts = int(r.get("posts") or 0)
    likes = int(r.get("likes") or 0)
    views = int(r.get("views") or 0)
    total_members = int(r.get("total_members") or 1)
    active_authors = int(r.get("active_authors") or 0)
    if posts == 0 and views == 0:
        return 0, "no tweets in period"

    eng_rate = (likes / views) if views > 0 else 0.0
    # 5% likes/views == strong (1.0); caps at 1.
    eng_norm = min(1.0, eng_rate / 0.05)
    # 50 posts/period gives full activity score
    activity_norm = min(1.0, posts / 50.0)
    # log10(views) /5 maps 100k views to 1.0
    reach_norm = min(1.0, math.log10(max(1, views)) / 5.0)
    if scope_type == "cohort":
        active_pct = (active_authors / total_members) if total_members else 0.0
        score = 0.35 * eng_norm + 0.25 * activity_norm + 0.20 * reach_norm + 0.20 * active_pct
    else:
        score = 0.40 * eng_norm + 0.30 * activity_norm + 0.30 * reach_norm
    val = int(round(score * 100))
    tier = (
        "buzzing" if val >= 80 else
        "healthy" if val >= 60 else
        "lukewarm" if val >= 40 else
        "quiet"    if val >= 20 else
        "silent"
    )
    if scope_type == "cohort":
        desc = (
            f"{tier} · {active_authors}/{total_members} active · "
            f"{fmt(posts)} posts · {eng_rate*100:.1f}% engagement"
        )
    else:
        desc = (
            f"{tier} · {fmt(posts)} posts · {eng_rate*100:.1f}% engagement · "
            f"{fmt(likes)} likes / {fmt(views)} views"
        )
    return val, desc


def _cookie_health():
    """Return list of {file, ok, age_days, size}.

    Checks local COOKIE_DIR first.  If none exist (e.g. on Render where
    the filesystem is separate), falls back to the cookie_health table in
    the configured database — populated by the local machine's setup script.
    """
    files = ["main.json", "scraper1.json", "scraper2.json"]
    slot_names = [f.replace(".json", "") for f in files]
    results = []

    # Try local files first
    any_local = any(os.path.exists(os.path.join(COOKIE_DIR, f)) for f in files)
    if any_local:
        for f in files:
            p = os.path.join(COOKIE_DIR, f)
            if not os.path.exists(p):
                results.append({"file": f, "ok": False, "age_days": None,
                                "size": 0, "reason": "missing"})
                continue
            try:
                st = os.stat(p)
                age_days = (time.time() - st.st_mtime) / 86400
                size = st.st_size
                ok = size > 200 and age_days < 30
                results.append({
                    "file": f, "ok": ok, "age_days": round(age_days, 1), "size": size,
                    "reason": "" if ok else (
                        "stale" if age_days >= 30 else "too small (login may be expired)"
                    ),
                })
            except OSError:
                results.append({"file": f, "ok": False, "age_days": None,
                                "size": 0, "reason": "unreadable"})
        return results

    # No local files — try DB fallback (Render deployment)
    try:
        from vibechecx_config import DB_CONFIG
        import psycopg2
        conn = psycopg2.connect(**DB_CONFIG, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT slot_name, size_bytes, age_seconds, last_seen_at "
            "FROM public.cookie_health ORDER BY slot_name"
        )
        rows = {r[0]: r for r in cur.fetchall()}
        conn.close()
    except Exception:
        rows = {}

    for slot, fname in zip(slot_names, files):
        row = rows.get(slot)
        if row:
            size = row[1]
            age_days = row[2] / 86400
            ok = size > 200 and age_days < 30
            results.append({
                "file": fname,
                "ok": ok,
                "age_days": round(age_days, 1),
                "size": size,
                "reason": "" if ok else (
                    "stale" if age_days >= 30 else "too small (login may be expired)"
                ),
            })
        else:
            results.append({"file": fname, "ok": False, "age_days": None,
                            "size": 0, "reason": "missing"})
    return results


def cookie_health_pill_html():
    health = _cookie_health()
    ok = sum(1 for h in health if h["ok"])
    if ok == len(health):
        return ""  # all good, don't clutter the nav
    dot = "bg-yellow-400" if ok > 0 else "bg-red-400"
    label = f"cookies {ok}/{len(health)}"
    return (
        f'<a href="/cookies" class="text-[10px] px-2 py-0.5 rounded-full bg-gray-900 border border-gray-800 hover:border-gray-700 transition" '
        f'title="Cookie health">'
        f'<span class="inline-block w-1.5 h-1.5 rounded-full {dot} mr-1"></span>{label}</a>'
    )
