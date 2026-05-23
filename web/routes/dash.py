"""web/routes/dash.py — /, /posts, /tags, /profile, /profiles, /set-profile/{pid}"""
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.core import (
    q, get_user, require_login, get_active_profile, NoActiveProfile,
    profile_account_ids, profile_display_handle, _require_profile,
)
from web.ui import (
    header_html, tip, tweet_link, tag_chip, type_pill, period_clause,
    period_buttons, fmt, fmt_compact, rel_time, error_page_html, AH, AF, HF, _vibe,
)

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vibechecx_precision import precision_badge_html  # noqa: E402

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dash(r: Request, tag: str = "", days: int = 0, sort: str = "likes"):
    user, prof_or_resp = _require_profile(r)
    if not user:
        return prof_or_resp
    prof = prof_or_resp
    if not isinstance(prof, dict):
        return prof
    # For a single-account profile, the dashboard IS the rich account view —
    # no point rendering a generic cohort-flavored summary plus a separate
    # account page. Delegate directly to account_page so /account/{h} and /
    # show the same canonical view, including AI insights inline.
    if prof.get("target_handle") and not prof.get("cohort_id"):
        from web.routes.accounts import account_page
        return account_page(prof["target_handle"], r, days=(days or 365))
    try:
        account_ids = profile_account_ids(prof)
    except NoActiveProfile:
        return RedirectResponse("/profiles", 302)
    order = {
        "likes": "likes DESC",
        "views": "views DESC",
        "date": "created_at DESC",
        "type": "is_reply ASC",
    }.get(sort, "likes DESC")
    period = period_clause(days, table_alias="")
    tag_clause = " AND %s = ANY(t.tags)" if tag else ""
    tag_params = (tag,) if tag else ()

    stats = q(
        f"""
        SELECT
          (SELECT count(*) FROM tweets WHERE author_account_id = ANY(%s){period}) AS t,
          (SELECT count(*) FROM tweets WHERE author_account_id = ANY(%s) AND is_retweet{period}) AS rt,
          (SELECT count(*) FROM tweets WHERE author_account_id = ANY(%s) AND is_reply{period}) AS re,
          (SELECT count(*) FROM tweets WHERE author_account_id = ANY(%s) AND NOT is_retweet AND NOT is_reply{period}) AS orig,
          (SELECT COALESCE(sum(likes),0) FROM tweets WHERE author_account_id = ANY(%s) AND NOT is_retweet{period}) AS tl,
          (SELECT COALESCE(sum(views),0) FROM tweets WHERE author_account_id = ANY(%s) AND NOT is_retweet{period}) AS tv,
          (SELECT count(*) FROM media WHERE tweet_id IN
             (SELECT tweet_id FROM tweets WHERE author_account_id = ANY(%s){period})) AS mi,
          (SELECT COALESCE(sum(likes),0) FROM tweets WHERE author_account_id = ANY(%s) AND NOT is_retweet AND NOT is_reply{period}) AS pl,
          (SELECT COALESCE(sum(likes),0) FROM tweets WHERE author_account_id = ANY(%s) AND is_reply{period}) AS rl,
          (SELECT COALESCE(sum(views),0) FROM tweets WHERE author_account_id = ANY(%s) AND NOT is_retweet AND NOT is_reply{period}) AS pv,
          (SELECT COALESCE(sum(views),0) FROM tweets WHERE author_account_id = ANY(%s) AND is_reply{period}) AS rv
        """,
        (account_ids,) * 11,
    )[0]

    recent = q(
        f"""
        SELECT a.username, a.avatar_url, t.tweet_id, t.content, t.likes, t.views,
               t.retweets, t.is_reply, t.is_retweet, t.is_quote, t.sentiment,
               t.content_type, t.tags, t.category, t.created_at
        FROM tweets t JOIN accounts a ON a.id = t.author_account_id
        WHERE t.author_account_id = ANY(%s) AND NOT t.is_retweet{tag_clause}{period_clause(days, 't')}
        ORDER BY t.{order} LIMIT 20
        """,
        (account_ids, *tag_params),
    )

    def _row(x):
        kind = "reply" if x["is_reply"] else ("quote" if x["is_quote"] else "original")
        tags_html = "".join(
            tag_chip(t, current_path="/", current_qs={"days": days, "sort": sort}, active=(t == tag))
            for t in (x.get("tags") or [])[:4]
        )
        return (
            "<tr class='border-b border-gray-800 text-sm hover:bg-gray-800/30'>"
            f"<td class='py-2 px-2 text-xs text-emerald-400 whitespace-nowrap'>"
            f"<a href='/account/{html.escape(x['username'])}' class='hover:underline'>@{html.escape(x['username'])}</a></td>"
            f"<td class='py-2 px-2 text-gray-300 max-w-md'>{tweet_link(x['tweet_id'], x['content'], 80, x['username'])}</td>"
            f"<td class='py-2 px-2 text-center tooltip whitespace-nowrap' data-tip='{x['likes']} likes'>{fmt(x['likes'])}</td>"
            f"<td class='py-2 px-2 text-center whitespace-nowrap'>{fmt(x['views'])}</td>"
            f"<td class='py-2 px-2 text-center'>{type_pill(kind)}</td>"
            f"<td class='py-2 px-2 flex flex-wrap gap-1'>{tags_html}</td>"
            f"<td class='py-2 px-2 text-xs text-gray-500 whitespace-nowrap'>{rel_time(x.get('created_at'))}</td>"
            "</tr>"
        )

    rows = "".join(_row(x) for x in recent)
    tag_banner = (
        f"<p class='text-xs text-emerald-500 mb-2'>Filtered by: #{html.escape(tag)} "
        f"<a href='/' class='underline text-gray-500 hover:text-white'>clear</a></p>"
        if tag
        else ""
    )
    sort_buttons = "".join(
        (
            f'<a href="/?sort={s}&days={days}{"&tag="+html.escape(tag) if tag else ""}" '
            f'class="px-2.5 py-1 rounded text-xs transition '
            f'{"bg-emerald-700 text-white" if sort == s else "bg-gray-800 text-gray-400 hover:text-white"}">'
            f'{label}</a>'
        )
        for s, label in [("likes", "❤ Likes"), ("views", "👁 Views"), ("date", "📅 Date")]
    )

    period_label = (
        f"Past {('24h' if days == 1 else f'{days}d')}" if days else "All time"
    )

    # Cohort overview cards for dashboard
    cohort_cards = ""
    ddays = days if days else 7
    cohorts_dash = q(
        "SELECT c.id, c.name, (SELECT count(*) FROM cohort_members WHERE cohort_id=c.id) AS members "
        "FROM cohorts c WHERE c.user_id=%s ORDER BY c.name", (user["id"],)
    )
    for c in cohorts_dash:
        co_id, cname = c["id"], html.escape(c.get("name", "?"))
        cv, _ = _vibe("cohort", co_id, f"{ddays}d")
        cv_color = "bg-emerald-500" if cv >= 65 else "bg-yellow-500" if cv >= 40 else "bg-red-500"
        top3 = q(
            f"SELECT a.username, COALESCE(SUM(t.likes),0) AS l "
            f"FROM cohort_members cm JOIN accounts a ON a.id=cm.account_id "
            f"LEFT JOIN tweets t ON t.author_account_id=a.id AND NOT t.is_retweet "
            f"AND t.created_at > NOW() - INTERVAL '{ddays} days' "
            f"WHERE cm.cohort_id=%s GROUP BY a.username ORDER BY l DESC LIMIT 3", (co_id,)
        )
        t3 = " · ".join(f"@{r['username']}" for r in top3) if top3 else "—"
        cohort_cards += (
            f'<div class="bg-gray-900 rounded-xl p-4 border border-gray-800">'
            f'<div class="flex items-center justify-between mb-2">'
            f'<a href="/cohort/{co_id}" class="text-sm font-medium text-emerald-400 hover:underline">{cname}</a>'
            f'<span class="text-xs font-bold {"text-emerald-400" if cv >= 65 else "text-yellow-400" if cv >= 40 else "text-red-400"}">{cv}/100</span></div>'
            f'<div class="w-full bg-gray-800 rounded-full h-1.5 mb-2 overflow-hidden">'
            f'<div class="{cv_color} h-full rounded-full" style="width:{cv}%"></div></div>'
            f'<div class="text-[10px] text-gray-400">{c["members"]} members</div>'
            f'<div class="text-[10px] text-gray-400 truncate">{t3}</div>'
            # Target the full-width panel below the cohort grid so the insight
            # uses the dashboard's full width instead of being crammed into
            # the small grid-cell card.
            f'<button hx-post="/cohort/{co_id}/generate-insights?period={ddays}d" '
            f'hx-target="#dash-insight-panel" hx-swap="innerHTML" '
            'hx-on:htmx:after-on-load="document.getElementById(\'dash-insight-panel\').scrollIntoView({behavior:\'smooth\',block:\'start\'})" '
            'class="text-xs mt-2 px-2 py-1 rounded bg-purple-700 hover:bg-purple-600 text-white transition">'
            f'✨ Insights</button>'
            '</div>'
        )
    cohort_html = (
        '<div class="mb-6"><h2 class="text-xs font-semibold text-gray-500 uppercase mb-3">📋 Cohorts</h2>'
        f'<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">{cohort_cards}</div>'
        # Full-width insight panel; lives outside the cohort grid so the
        # injected HTML inherits the parent <main> width (max-w-7xl / 1600px),
        # not a grid-cell width.
        '<div id="dash-insight-panel" class="mt-6"></div>'
        '</div>'
    ) if cohort_cards else ""

    return header_html(days, prof.get("name", ""), is_admin=user.get("is_admin", False)) + (
        '<div class="flex items-center justify-between mb-4">'
        f'<div class="flex gap-1">{period_buttons(days, "/", {"sort": sort, "tag": tag})}</div>'
        f'<div class="text-xs text-gray-500">{period_label}</div>'
        '</div>'
        f'{precision_badge_html(prof)}'

        f'{cohort_html}'
        '<div class="flex justify-between items-center mb-4 mt-4">'
        '<h1 class="text-lg font-semibold">Top Posts</h1>'
        f'<div class="flex gap-1">{sort_buttons}</div>'
        '</div>'
        '<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">'
        f'<div class="bg-gray-900 rounded-xl p-5 border border-gray-800 min-w-0">'
        f'<div class="text-3xl font-bold text-emerald-400" title="{fmt(stats["t"])}">{fmt_compact(stats["t"])}</div>'
        f'<div class="text-sm text-gray-400 whitespace-normal">Tweets</div>'
        f'<div class="text-xs text-gray-400 mt-1 break-words">{fmt(stats["orig"])} originals · {fmt(stats["re"])} replies · {fmt(stats["rt"])} retweets</div></div>'
        f'<div class="bg-gray-900 rounded-xl p-5 border border-gray-800 min-w-0">'
        f'<div class="text-3xl font-bold text-pink-400" title="{fmt(stats["tl"])}">{fmt_compact(stats["tl"])}</div>'
        f'<div class="text-sm text-gray-400 whitespace-normal">Earned Likes</div>'
        f'<div class="text-xs text-gray-400 mt-1 break-words">posts ❤ {fmt(stats["pl"])} · replies ❤ {fmt(stats["rl"])}</div></div>'
        f'<div class="bg-gray-900 rounded-xl p-5 border border-gray-800 min-w-0">'
        f'<div class="text-3xl font-bold text-blue-400" title="{fmt(stats["tv"])}">{fmt_compact(stats["tv"])}</div>'
        f'<div class="text-sm text-gray-400 whitespace-normal">Total Views</div>'
        f'<div class="text-xs text-gray-400 mt-1 break-words">posts 👁 {fmt(stats["pv"])} · replies 👁 {fmt(stats["rv"])}</div></div>'
        f'<div class="bg-gray-900 rounded-xl p-5 border border-gray-800 min-w-0">'
        f'<div class="text-3xl font-bold text-cyan-400" title="{fmt(stats["mi"])}">{fmt_compact(stats["mi"])}</div>'
        f'<div class="text-sm text-gray-400 whitespace-normal">Media Items</div></div>'
        '</div>'
        f'{tag_banner}'
        '<div class="bg-gray-900 rounded-xl border border-gray-800 p-5">'
        '<table class="w-full"><thead><tr class="text-xs text-gray-500 uppercase">'
        '<th class="text-left pb-2">Author</th><th class="text-left pb-2">Tweet</th>'
        '<th class="pb-2">❤ Likes</th><th class="pb-2">👁 Views</th><th class="pb-2">Type</th>'
        '<th class="pb-2">Tags</th><th class="pb-2">When</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    ) + HF


@router.get("/posts", response_class=HTMLResponse)
def posts(r: Request, sort: str = "likes", tag: str = "", days: int = 0,
          type: str = "all"):
    user, prof_or_resp = _require_profile(r)
    if not user:
        return prof_or_resp
    prof = prof_or_resp
    if not isinstance(prof, dict):
        return prof
    try:
        account_ids = profile_account_ids(prof)
    except NoActiveProfile:
        return RedirectResponse("/profiles", 302)
    order = {"likes": "likes DESC", "views": "views DESC", "date": "created_at DESC"}.get(
        sort, "likes DESC"
    )
    tag_clause = " AND %s = ANY(t.tags)" if tag else ""
    tag_params = (tag,) if tag else ()
    type_clause = {
        "originals": " AND NOT t.is_reply AND NOT t.is_retweet AND NOT t.is_quote",
        "replies": " AND t.is_reply",
        "quotes": " AND t.is_quote",
        "retweets": " AND t.is_retweet",
        "all": " AND NOT t.is_retweet",
        "everything": "",
    }.get(type, " AND NOT t.is_retweet")
    d = q(
        f"""
        SELECT a.username, a.avatar_url, t.tweet_id, t.content, t.likes, t.views,
               t.replies AS rc, t.is_reply, t.is_retweet, t.is_quote, t.sentiment,
               t.content_type, t.tags, t.category, t.created_at,
               (SELECT count(*)::int FROM replies WHERE tweet_id=t.tweet_id) AS mined_reply_count
        FROM tweets t JOIN accounts a ON a.id = t.author_account_id
        WHERE t.author_account_id = ANY(%s){type_clause}{tag_clause}{period_clause(days, 't')}
        ORDER BY t.{order} LIMIT 100
        """,
        (account_ids, *tag_params),
    )

    def _row(x):
        kind = (
            "reply" if x["is_reply"] else (
                "quote" if x["is_quote"] else (
                    "retweet" if x["is_retweet"] else "original"
                )
            )
        )
        tags_html = "".join(
            tag_chip(t, current_path="/posts",
                     current_qs={"days": days, "sort": sort, "type": type}, active=(t == tag))
            for t in (x.get("tags") or [])[:4]
        )
        on_x = x.get("rc") or 0
        mined = x.get("mined_reply_count") or 0
        reply_label = (
            f"💬 {on_x}" if not mined
            else f"💬 {mined}/{on_x}" if on_x else f"💬 {mined}"
        )
        replies_link = (
            f'<a href="/tweet/{html.escape(x["tweet_id"])}" '
            f'class="text-cyan-400 hover:underline" title="{mined} mined / {on_x} on X">'
            f'{reply_label}</a>'
        )
        return (
            "<tr class='border-b border-gray-800 text-sm hover:bg-gray-800/30'>"
            f"<td class='py-2 px-3 text-xs text-emerald-400 whitespace-nowrap'>"
            f"<a href='/account/{html.escape(x['username'])}' class='hover:underline'>@{html.escape(x['username'])}</a></td>"
            f"<td class='py-2 px-3 text-gray-300 max-w-lg'>{tweet_link(x['tweet_id'], x['content'], 100, x['username'])}</td>"
            f"<td class='py-2 px-3'>{type_pill(kind)}</td>"
            f"<td class='py-2 px-3 text-center whitespace-nowrap'>{fmt(x['likes'])}</td>"
            f"<td class='py-2 px-3 text-center whitespace-nowrap'>{fmt(x['views'])}</td>"
            f"<td class='py-2 px-3 text-center text-xs'>{replies_link}</td>"
            f"<td class='py-2 px-3 flex flex-wrap gap-1'>{tags_html}</td>"
            f"<td class='py-2 px-3 text-xs text-gray-500'>{rel_time(x.get('created_at'))}</td>"
            "</tr>"
        )

    rws = "".join(_row(x) for x in d)
    sort_buttons = "".join(
        (
            f'<a href="/posts?sort={s}&days={days}&type={html.escape(type)}{"&tag="+html.escape(tag) if tag else ""}" '
            f'class="px-3 py-1 rounded text-xs transition '
            f'{"bg-emerald-800 text-emerald-200" if sort == s else "bg-gray-800 text-gray-400 hover:text-white"}">'
            f'{label}</a>'
        )
        for s, label in [("likes", "❤ Likes"), ("views", "👁 Views"), ("date", "📅 Date")]
    )
    type_tabs = "".join(
        (
            f'<a href="/posts?type={tval}&sort={sort}&days={days}{"&tag="+html.escape(tag) if tag else ""}" '
            f'class="px-3 py-1 rounded-full text-xs transition '
            f'{"bg-cyan-700 text-white" if type == tval else "bg-gray-800 text-gray-400 hover:text-white"}">'
            f'{label}</a>'
        )
        for tval, label in [
            ("all", "All (no RT)"),
            ("originals", "Originals"),
            ("replies", "Replies"),
            ("quotes", "Quotes"),
            ("retweets", "Retweets"),
            ("everything", "Everything"),
        ]
    )
    tag_banner = (
        f"<p class='text-xs text-emerald-500 mb-2'>Filtered by: #{html.escape(tag)} "
        f"<a href='/posts?type={html.escape(type)}&days={days}&sort={sort}' class='underline text-gray-500 hover:text-white'>clear</a></p>"
        if tag
        else ""
    )
    period_label = f"Past {('24h' if days == 1 else f'{days}d')}" if days else "All time"
    return header_html(days, prof.get("name", ""), is_admin=user.get("is_admin", False)) + (
        '<div class="flex items-center justify-between mb-4">'
        f'<div class="flex gap-1">{period_buttons(days, "/posts", {"sort": sort, "tag": tag, "type": type})}</div>'
        f'<div class="text-xs text-gray-500">{period_label}</div>'
        '</div>'
        '<div class="flex justify-between items-center mb-4 flex-wrap gap-2">'
        '<h1 class="text-xl font-semibold">Posts</h1>'
        f'<div class="flex gap-1 flex-wrap">{type_tabs}</div>'
        f'<div class="flex gap-1">{sort_buttons}</div>'
        '</div>'
        f'{tag_banner}'
        '<div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">'
        '<table class="w-full"><thead><tr class="text-xs text-gray-500 uppercase bg-gray-800/50">'
        '<th class="text-left p-3">Author</th><th class="text-left p-3">Tweet</th>'
        '<th class="p-3">Type</th><th class="p-3">❤ Likes</th><th class="p-3">👁 Views</th>'
        '<th class="p-3">💬 Replies</th>'
        '<th class="p-3">Tags</th><th class="p-3">When</th></tr></thead>'
        f'<tbody>{rws}</tbody></table></div>'
    ) + HF


@router.get("/tags", response_class=HTMLResponse)
def tags(r: Request, days: int = 0):
    user, prof_or_resp = _require_profile(r)
    if not user:
        return prof_or_resp
    prof = prof_or_resp
    if not isinstance(prof, dict):
        return prof
    try:
        account_ids = profile_account_ids(prof)
    except NoActiveProfile:
        return RedirectResponse("/profiles", 302)
    d = q(
        f"""
        SELECT unnest(tags) AS tag, count(*) AS c,
               avg(likes)::int AS al, avg(views)::int AS av
        FROM tweets
        WHERE author_account_id = ANY(%s)
          AND tags IS NOT NULL
          AND NOT is_retweet{period_clause(days, '')}
        GROUP BY tag ORDER BY c DESC LIMIT 50
        """,
        (account_ids,),
    )
    cards = "".join(
        (
            f'<a href="/posts?tag={html.escape(x["tag"])}" '
            f'class="block bg-gray-900 rounded-xl p-4 border border-gray-800 '
            f'hover:border-emerald-500/30 transition">'
            f'<div class="text-emerald-400 font-semibold">#{html.escape(x["tag"])}</div>'
            f'<div class="mt-2 grid grid-cols-2 gap-2 text-center text-xs">'
            f'<div class="bg-gray-800/50 rounded p-1.5">'
            f'<div class="text-gray-200 font-medium">{x["c"]}</div>'
            f'<div class="text-gray-400">posts</div></div>'
            f'<div class="bg-gray-800/50 rounded p-1.5">'
            f'<div class="text-pink-400 font-medium">{x["al"]}</div>'
            f'<div class="text-gray-400">avg ❤</div></div></div></a>'
        )
        for x in d
    )
    return header_html(days, prof.get("name", ""), is_admin=user.get("is_admin", False)) + (
        f"<h1 class='text-xl font-semibold mb-6'>Tags</h1>"
        f"<div class='grid grid-cols-2 md:grid-cols-3 gap-3'>"
        f"{cards or '<p class=\"text-gray-500 text-sm col-span-full\">No tags yet — run enrichment.</p>'}"
        f"</div>"
    ) + HF


@router.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(r: Request, days: int = 0, sort: str = "composite", dir: str = "desc"):
    """Standalone leaderboard page for the active cohort profile."""
    from web.routes.cohorts import _leaderboard_query as lbq, _LEADERBOARD_SORT_COLS

    user, prof_or_resp = _require_profile(r)
    if not user:
        return prof_or_resp
    prof = prof_or_resp
    if not isinstance(prof, dict):
        return prof

    cohort_id = prof.get("cohort_id")
    if not cohort_id:
        return header_html(days, prof.get("name", ""), is_admin=user.get("is_admin", False)) + (
            '<div class="text-center py-16">'
            '<div class="text-5xl mb-4 opacity-40">📊</div>'
            '<h2 class="text-xl font-semibold text-gray-300 mb-2">Leaderboard requires a Cohort workspace</h2>'
            '<p class="text-sm text-gray-500 mb-4">Single-account profiles don\'t have a leaderboard. '
            'Switch to a cohort workspace to see member rankings.</p>'
            '<a href="/profiles" class="inline-block bg-emerald-600 hover:bg-emerald-500 '
            'text-white rounded-lg px-6 py-2.5 text-sm font-medium transition">'
            'Switch Workspace →</a>'
            '</div>'
        ) + HF

    ip = f"{days}d" if days else "7d"
    period_str = f"Past {('24h' if days == 1 else f'{days}d')}" if days else "All time"
    if dir not in ("asc", "desc"):
        dir = "desc"

    lb = lbq(cohort_id, ip)
    asc = dir == "asc"
    if sort in _LEADERBOARD_SORT_COLS:
        def _k(row):
            if sort == "followers":
                v = row.get("followers_count")
            elif sort == "engagement_rate":
                v = row.get("engagement_rate")
            else:
                v = row.get(sort)
            return (v if v is not None else -1)
        lb = sorted(lb, key=_k, reverse=not asc)

    def _pb(p):
        cls = "bg-emerald-700 text-white" if p == ip else "bg-gray-800 text-gray-400 hover:text-white"
        d = {"24h": 1, "7d": 7, "14d": 14, "30d": 30}.get(p, 0)
        return f'<a href="/leaderboard?days={d}&sort={sort}&dir={dir}" class="px-2.5 py-1 rounded text-xs transition {cls}">{p}</a>'

    period_seg = "".join(_pb(p) for p in ("24h", "7d", "14d", "30d"))

    def _cell(value, fmt_str="{:.2f}", suffix=""):
        if value is None:
            return '<td class="py-2 px-2 text-right text-gray-500 text-xs">—</td>'
        try:
            formatted = fmt_str.format(value) + suffix
        except (ValueError, TypeError):
            formatted = str(value) + suffix
        return f'<td class="py-2 px-2 text-right text-sm text-gray-200">{formatted}</td>'

    def _sort_link(key, label):
        is_active = sort == key
        if is_active:
            next_dir = "asc" if dir == "desc" else "desc"
            glyph = " ▲" if dir == "asc" else " ▼"
        else:
            next_dir = "desc"
            glyph = ""
        cls = "text-emerald-400" if is_active else "text-gray-500 hover:text-emerald-400"
        return (
            f'<a href="/leaderboard?days={days}&sort={key}&dir={next_dir}" '
            f'class="cursor-pointer select-none {cls}">{label}{glyph}</a>'
        )

    rows_html = ""
    for i, row in enumerate(lb, 1):
        avatar = row.get("avatar_url") or ""
        avatar_html = (
            f'<img src="{html.escape(avatar)}" class="w-7 h-7 rounded-full bg-gray-700 object-cover flex-shrink-0" '
            'onerror="this.style.display=\'none\'" loading="lazy">'
        )
        rows_html += (
            '<tr class="border-b border-gray-800 hover:bg-gray-800/40 cursor-pointer transition" '
            f"onclick=\"window.location='/account/{html.escape(row['username'])}'\">"
            f'<td class="py-2 px-2 text-center text-xs text-gray-500">#{i}</td>'
            f'<td class="py-2 px-2"><div class="flex items-center gap-2">{avatar_html}'
            f'<a href="/account/{html.escape(row["username"])}" class="text-emerald-400 hover:underline text-sm font-medium">'
            f'@{html.escape(row["username"])}</a></div></td>'
            + _cell(row.get("composite"), "{:.3f}")
            + _cell(
                (row.get("engagement_rate") or 0) * 100 if row.get("engagement_rate") is not None else None,
                "{:.2f}", suffix="%",
              )
            + _cell(row.get("voice_share"), "{:.1f}", suffix="%")
            + _cell(row.get("likes"), "{:,}")
            + _cell(row.get("views"), "{:,}")
            + _cell(row.get("likes_per_post"), "{:.0f}")
            + _cell(row.get("views_per_post"), "{:,.0f}")
            + _cell(
                (row.get("reply_ratio") or 0) * 100 if row.get("reply_ratio") is not None else None,
                "{:.0f}", suffix="%",
              )
            + _cell(row.get("posts"), "{:d}")
            + _cell(row.get("followers_count"), "{:d}")
            + '</tr>'
        )

    no_data = not lb
    return header_html(days, prof.get("name", ""), is_admin=user.get("is_admin", False)) + (
        '<div class="flex items-center justify-between mb-4">'
        '<h1 class="text-lg font-semibold">Leaderboard</h1>'
        f'<div class="text-xs text-gray-500">{period_str} · {len(lb)} members</div>'
        '</div>'
        f'<div class="inline-flex rounded-lg bg-gray-900 border border-gray-800 p-0.5 mb-4">{period_seg}</div>'
        + (
            '<div class="text-center py-12 bg-gray-900 rounded-xl border border-gray-800">'
            '<div class="text-4xl mb-2 opacity-40">📊</div>'
            '<p class="text-gray-400 text-sm">No data for this period.</p></div>'
            if no_data else
            '<div class="overflow-x-auto rounded-lg border border-gray-800">'
            '<table class="w-full text-sm table-auto">'
            '<thead class="bg-gray-900/80">'
            '<tr class="text-[11px] text-gray-500 uppercase tracking-wider border-b border-gray-800">'
            '<th class="py-2 px-2 text-center">#</th>'
            '<th class="py-2 px-2 text-left">Account</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("composite", "Composite")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("engagement_rate", "Eng rate")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("voice_share", "Voice %")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("likes", "Likes")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("views", "Views")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("likes_per_post", "Likes/post")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("views_per_post", "Views/post")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("reply_ratio", "Reply %")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("posts", "Posts")}</th>'
            f'<th class="py-2 px-2 text-right">{_sort_link("followers_count", "Followers")}</th>'
            '</tr></thead>'
            f'<tbody>{rows_html}</tbody></table></div>'
        )
    ) + HF


@router.get("/profile", response_class=HTMLResponse)
def profile_view(r: Request, days: int = 0):
    """Per-profile overview. Renders the **active profile's** identity,
    not a hardcoded handle."""
    user, prof_or_resp = _require_profile(r)
    if not user:
        return prof_or_resp
    prof = prof_or_resp
    if not isinstance(prof, dict):
        return prof
    # Single-account profile → render the rich account view directly. No
    # duplicate "Profile" page that shows watered-down stats; the account
    # page is canonical and includes AI insights inline.
    if prof.get("target_handle") and not prof.get("cohort_id"):
        from web.routes.accounts import account_page
        return account_page(prof["target_handle"], r, days=(days or 365))
    try:
        account_ids = profile_account_ids(prof)
    except NoActiveProfile:
        return RedirectResponse("/profiles", 302)

    is_cohort = bool(prof.get("cohort_id"))
    if is_cohort:
        agg = q(
            "SELECT COALESCE(SUM(followers_count),0)::int AS followers, "
            "COALESCE(SUM(following_count),0)::int AS following, "
            "COUNT(*) AS members "
            "FROM accounts WHERE id = ANY(%s)",
            (account_ids,),
        )[0]
        display_name = prof.get("name") or prof.get("cname") or "Cohort"
        handle_text = f"{agg['members']} members"
        followers = agg["followers"]
        following = agg["following"]
    else:
        a = q(
            "SELECT username, display_name, followers_count, following_count, bio "
            "FROM accounts WHERE id = ANY(%s) LIMIT 1",
            (account_ids,),
        )
        if not a:
            return RedirectResponse("/profiles", 302)
        a = a[0]
        display_name = a.get("display_name") or a["username"]
        handle_text = f"@{a['username']}"
        followers = a.get("followers_count") or 0
        following = a.get("following_count") or 0

    s = q(
        f"""
        SELECT
          count(*) AS t,
          count(*) FILTER (WHERE NOT is_retweet AND NOT is_reply) AS orig,
          count(*) FILTER (WHERE is_reply) AS r,
          count(*) FILTER (WHERE is_retweet) AS rt,
          COALESCE(sum(likes) FILTER (WHERE NOT is_retweet AND NOT is_reply),0)::int AS pl,
          COALESCE(sum(likes) FILTER (WHERE is_reply AND NOT is_retweet),0)::int AS rl,
          COALESCE(sum(likes) FILTER (WHERE NOT is_retweet),0)::int AS tl,
          COALESCE(sum(views) FILTER (WHERE NOT is_retweet AND NOT is_reply),0)::int AS pv,
          COALESCE(sum(views) FILTER (WHERE is_reply AND NOT is_retweet),0)::int AS rv,
          COALESCE(sum(views) FILTER (WHERE NOT is_retweet),0)::int AS tv
        FROM tweets WHERE author_account_id = ANY(%s){period_clause(days, '')}
        """,
        (account_ids,),
    )[0]

    pl_pct = (s["pl"] / (s["tl"] or 1)) * 100
    rl_pct = (s["rl"] / (s["tl"] or 1)) * 100
    pv_pct = (s["pv"] / (s["tv"] or 1)) * 100
    rv_pct = (s["rv"] / (s["tv"] or 1)) * 100

    return header_html(days, prof.get("name", ""), is_admin=user.get("is_admin", False)) + (
        "<h1 class='text-xl font-semibold mb-6'>Profile</h1>"
        "<div class='grid grid-cols-1 lg:grid-cols-2 gap-6'>"
        "<div class='bg-gray-900 rounded-xl p-6 border border-gray-800'>"
        f"<div class='text-emerald-400 font-semibold text-2xl'>{html.escape(handle_text)}</div>"
        f"<div class='text-gray-400 text-sm mt-1'>{html.escape(display_name)}</div>"
        "<div class='mt-4 flex gap-4 text-sm'>"
        "<div class='bg-gray-800/50 rounded-xl p-3 text-center flex-1 min-w-0'>"
        f"<div class='text-lg font-bold text-gray-200' title='{fmt(followers)}'>{fmt_compact(followers)}</div>"
        f"<div class='text-xs text-gray-400'>{'cohort followers' if is_cohort else 'followers'}</div></div>"
        "<div class='bg-gray-800/50 rounded-xl p-3 text-center flex-1 min-w-0'>"
        f"<div class='text-lg font-bold text-gray-200' title='{fmt(following)}'>{fmt_compact(following)}</div>"
        f"<div class='text-xs text-gray-400'>{'cohort following' if is_cohort else 'following'}</div></div>"
        "</div>"
        "<div class='mt-4 grid grid-cols-2 gap-3 text-center text-sm'>"
        f"<div class='bg-gray-800/50 rounded-xl p-3 min-w-0'><div class='text-2xl font-bold text-emerald-400' title='{fmt(s['t'])}'>{fmt_compact(s['t'])}</div><div class='text-xs text-gray-400'>total posts</div></div>"
        f"<div class='bg-gray-800/50 rounded-xl p-3 min-w-0'><div class='text-2xl font-bold text-cyan-400' title='{fmt(s['orig'])}'>{fmt_compact(s['orig'])}</div><div class='text-xs text-gray-400'>originals</div></div>"
        f"<div class='bg-gray-800/50 rounded-xl p-3 min-w-0'><div class='text-2xl font-bold text-cyan-400' title='{fmt(s['r'])}'>{fmt_compact(s['r'])}</div><div class='text-xs text-gray-400'>replies</div></div>"
        f"<div class='bg-gray-800/50 rounded-xl p-3 min-w-0'><div class='text-2xl font-bold text-gray-300' title='{fmt(s['rt'])}'>{fmt_compact(s['rt'])}</div><div class='text-xs text-gray-400'>retweets</div></div>"
        "</div></div>"
        "<div class='bg-gray-900 rounded-xl p-6 border border-gray-800'>"
        "<h2 class='text-lg font-semibold mb-4'>Likes</h2>"
        f"<div class='space-y-3'>"
        f"<div><div class='flex justify-between text-sm'><span>On Posts</span><span class='text-pink-400 whitespace-nowrap'>❤ {fmt(s['pl'])}</span></div>"
        f"<div class='w-full bg-gray-800 rounded-full h-2 mt-1'><div class='bg-pink-500 h-2 rounded-full' style='width:{pl_pct:.1f}%'></div></div></div>"
        f"<div><div class='flex justify-between text-sm'><span>On Replies</span><span class='text-pink-400 whitespace-nowrap'>❤ {fmt(s['rl'])}</span></div>"
        f"<div class='w-full bg-gray-800 rounded-full h-2 mt-1'><div class='bg-pink-500 h-2 rounded-full' style='width:{rl_pct:.1f}%'></div></div></div>"
        f"<div class='text-center text-2xl font-bold text-pink-400 mt-4 whitespace-nowrap'>❤ {fmt(s['tl'])}</div></div></div>"
        "<div class='bg-gray-900 rounded-xl p-6 border border-gray-800'>"
        "<h2 class='text-lg font-semibold mb-4'>Views</h2>"
        f"<div class='space-y-3'>"
        f"<div><div class='flex justify-between text-sm'><span>On Posts</span><span class='text-blue-400 whitespace-nowrap'>👁 {fmt(s['pv'])}</span></div>"
        f"<div class='w-full bg-gray-800 rounded-full h-2 mt-1'><div class='bg-blue-500 h-2 rounded-full' style='width:{pv_pct:.1f}%'></div></div></div>"
        f"<div><div class='flex justify-between text-sm'><span>On Replies</span><span class='text-blue-400 whitespace-nowrap'>👁 {fmt(s['rv'])}</span></div>"
        f"<div class='w-full bg-gray-800 rounded-full h-2 mt-1'><div class='bg-blue-500 h-2 rounded-full' style='width:{rv_pct:.1f}%'></div></div></div>"
        f"<div class='text-center text-2xl font-bold text-blue-400 mt-4 whitespace-nowrap'>👁 {fmt(s['tv'])}</div></div></div>"
        "</div>"
        "<div class='mt-6 bg-gray-900 rounded-xl border border-gray-800 p-5'>"
        "<div class='flex items-center justify-between mb-3'>"
        "<h2 class='text-sm font-semibold text-gray-300'>Past Insights</h2>"
        "</div>"
        "<div hx-get='/insights/library' hx-trigger='load' hx-swap='innerHTML'>"
        "<p class='text-gray-500 text-xs text-center py-4'>Loading…</p>"
        "</div></div>"
    ) + HF


@router.get("/profiles", response_class=HTMLResponse)
def profiles_page(r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    data = q(
        "SELECT p.*, c.name AS cohort_name, c.slug FROM profiles p "
        "LEFT JOIN cohorts c ON c.id = p.cohort_id "
        "WHERE p.user_id = %s ORDER BY p.created_at",
        (user["id"],),
    )
    if not data:
        return RedirectResponse("/wizard/1", status_code=302)
    cards = ""
    for p in data:
        is_cohort = p["type"] == "cohort"
        emoji = "👥" if is_cohort else "📊"
        sub = p.get("cohort_name") or p.get("target_handle") or ""
        if is_cohort and p.get("cohort_id"):
            deep_link = (
                f'<a href="/cohort/{p["cohort_id"]}" '
                'class="text-[10px] text-gray-500 hover:text-emerald-400 transition mt-1 inline-block">manage ↗</a>'
            )
        elif p.get("target_handle"):
            deep_link = (
                f'<a href="/account/{html.escape(p["target_handle"])}" '
                'class="text-[10px] text-gray-500 hover:text-emerald-400 transition mt-1 inline-block">view ↗</a>'
            )
        else:
            deep_link = ""
        cards += (
            f'<div class="bg-gray-900 rounded-xl p-5 border border-gray-800 hover:border-emerald-500/30 transition">'
            f'<a href="/set-profile/{p["id"]}" class="block">'
            f'<div class="text-2xl mb-2">{emoji}</div>'
            f'<div class="text-emerald-400 font-semibold">{html.escape(p["name"])}</div>'
            f'<div class="text-xs text-gray-500 mt-1">{html.escape(p["type"])} · {html.escape(sub)}</div>'
            f'</a>{deep_link}</div>'
        )
    create_btns = (
        '<div class="mt-8">'
        '<h2 class="text-xs font-semibold text-gray-500 uppercase mb-3">+ New Workspace</h2>'
        '<div class="grid grid-cols-3 gap-3">'
        '<a href="/wizard/2?type=single" class="block bg-gray-900/50 rounded-xl p-4 '
        'border border-dashed border-gray-700 hover:border-emerald-500/30 transition text-center group">'
        '<div class="text-2xl mb-1 text-gray-500 group-hover:text-emerald-400">📊</div>'
        '<div class="text-xs text-gray-500 group-hover:text-emerald-400">Single Account</div></a>'
        '<a href="/wizard/2?type=cohort" class="block bg-gray-900/50 rounded-xl p-4 '
        'border border-dashed border-gray-700 hover:border-emerald-500/30 transition text-center group">'
        '<div class="text-2xl mb-1 text-gray-500 group-hover:text-emerald-400">👥</div>'
        '<div class="text-xs text-gray-500 group-hover:text-emerald-400">Cohort</div></a>'
        '<a href="/discover?from=profiles" class="block bg-gray-900/50 rounded-xl p-4 '
        'border border-dashed border-gray-700 hover:border-emerald-500/30 transition text-center group">'
        '<div class="text-2xl mb-1 text-gray-500 group-hover:text-emerald-400">🔍</div>'
        '<div class="text-xs text-gray-500 group-hover:text-emerald-400">Discover</div></a>'
        '</div></div>'
    )
    return header_html(0, is_admin=user.get("is_admin", False)) + (
        '<div class="max-w-lg mx-auto">'
        '<h1 class="text-xl font-semibold mb-6">Workspaces</h1>'
        f'<div class="grid grid-cols-2 gap-4">{cards}</div>'
        f'{create_btns}</div>'
    ) + HF


@router.get("/set-profile/{pid}")
def set_profile(pid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    p = q("SELECT * FROM profiles WHERE id=%s AND user_id=%s", (pid, user["id"]))
    if not p:
        return RedirectResponse("/profiles", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("vibechecx_profile", str(pid), max_age=30 * 86400, httponly=True, samesite="lax")
    return resp
