"""web/routes/cohorts.py — /cohorts, all /cohort/{cid}/... routes"""
import os
import sys
import json
import html
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

from web.core import (
    q, get_user, require_login, DB,
)
from web.ui import (
    header_html, tip, fmt, fmt_compact, HF, _vibe, _sparkline_svg,
    scrape_depth_picker_html,
)
from web.render_insights import _render_insights, _insight_export_response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vibechecx_config import COLLECTOR_DIR, SCRAPER_HEADFUL  # noqa: E402
from vibechecx_scrape_status import start_session as ss_start  # noqa: E402
import vibechecx_insights as vi  # noqa: E402

router = APIRouter()

_PERIOD_INTERVALS = {
    "24h": "1 day",
    "7d": "7 days",
    "14d": "14 days",
    "30d": "30 days",
    "all": "100 years",
}

_LEADERBOARD_SORT_COLS = {
    "composite": "composite DESC",
    "engagement_rate": "engagement_rate DESC NULLS LAST",
    "voice_share": "voice_share DESC NULLS LAST",
    "likes": "likes DESC",
    "views": "views DESC",
    "likes_per_post": "likes_per_post DESC NULLS LAST",
    "views_per_post": "views_per_post DESC NULLS LAST",
    "reply_ratio": "reply_ratio DESC NULLS LAST",
    "posts": "posts DESC",
    "followers": "followers_count DESC",
}

_PERIOD_DAYS = {
    "24h": 1,
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "all": 365,
}


def _slugify(name):
    """Lower, strip @, collapse non-alphanumeric to single underscore."""
    import re
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lstrip("@").lower()).strip("_")
    return s or "cohort"


def _parse_handles_blob(text):
    """Split a free-text handle list (newlines, commas, spaces, tabs) into a
    deduped, lowercased list of bare usernames. Drops empties, strips `@`,
    keeps order of first appearance."""
    import re
    seen, out = set(), []
    for raw in re.split(r"[\s,;]+", text or ""):
        h = raw.strip().lstrip("@").lower()
        # X handles are 1–15 chars: letters, digits, underscore.
        if not h or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h):
            continue
        if h in seen:
            continue
        seen.add(h)
        out.append(h)
    return out


def _create_cohort_with_members(user_id, handle, all_members, selected_usernames,
                                 cohort_name=None, seed_avatar="", slug=None):
    """Single source of truth for cohort+members+profile insertion."""
    name = cohort_name or f"@{handle}"
    slug = slug or f"{handle}_{user_id}"
    rows = q(
        "INSERT INTO cohorts(name, slug, brand_keywords, user_id, pfp_url) "
        "VALUES(%s, %s, %s, %s, %s) "
        "ON CONFLICT(slug) DO UPDATE SET name=EXCLUDED.name RETURNING id",
        (name, slug, json.dumps([handle]), user_id, seed_avatar),
    )
    cid = rows[0]["id"]
    # Lowercase the lookup key too — selected_usernames may arrive mixed-case
    # from the form (form checkboxes preserve the casing shown to the user).
    by_name = {m["username"].lower(): m for m in all_members}
    for uname in selected_usernames:
        ulow = uname.lower().lstrip("@")
        m = by_name.get(ulow, {"username": ulow})
        # Migration 004 enforces a case-insensitive UNIQUE on LOWER(username).
        # ON CONFLICT(username) only matches exact-case conflicts, so we
        # MUST insert lowercase to keep the canonical row a hit-target.
        # Without this lowering, inserting `@SolGab` while a `solgab` row
        # exists trips `accounts_username_lower_unique` and 500s the whole
        # cohort-create request.
        rows = q(
            "INSERT INTO accounts(username, display_name, avatar_url, bio, followers_count) "
            "VALUES(%s, %s, %s, %s, %s) "
            "ON CONFLICT(username) DO UPDATE SET display_name=EXCLUDED.display_name "
            "RETURNING id",
            (
                ulow,
                m.get("display_name", ""),
                m.get("avatar", ""),
                (m.get("bio") or "")[:120],
                m.get("followers", 0),
            ),
        )
        aid = rows[0]["id"]
        q(
            "INSERT INTO cohort_members(cohort_id, account_id) VALUES(%s, %s) "
            "ON CONFLICT DO NOTHING",
            (cid, aid),
        )
    rows = q(
        "INSERT INTO profiles(user_id, name, type, cohort_id) "
        "VALUES(%s, %s, 'cohort', %s) RETURNING id",
        (user_id, name, cid),
    )
    return cid, rows[0]["id"]


def _create_single_profile(user_id, handle, name=None):
    rows = q(
        "INSERT INTO profiles(user_id, name, type, target_handle) "
        "VALUES(%s, %s, 'single', %s) RETURNING id",
        (user_id, name or f"@{handle}", handle),
    )
    return rows[0]["id"]


def _member_rows(cid):
    d = q(
        """
        SELECT a.id, a.username, a.display_name, a.avatar_url, a.followers_count,
               cm.note,
               count(t.tweet_id) AS tweets,
               COALESCE(sum(t.likes) FILTER (WHERE NOT t.is_retweet), 0)::int AS likes,
               COALESCE(sum(t.views) FILTER (WHERE NOT t.is_retweet), 0)::int AS views
        FROM cohort_members cm JOIN accounts a ON a.id = cm.account_id
        LEFT JOIN tweets t ON t.author_account_id = a.id
        WHERE cm.cohort_id = %s
        GROUP BY a.id, a.username, a.display_name, a.avatar_url, a.followers_count, cm.note
        ORDER BY likes DESC
        """,
        (cid,),
    )
    return "".join(
        (
            '<div class="bg-gray-900 rounded-xl p-3 border border-gray-800">'
            '<div class="flex items-start justify-between">'
            '<div class="flex-1">'
            f'<div class="text-emerald-400 font-semibold text-sm">'
            f'<a href="/account/{html.escape(x["username"])}" class="hover:underline">@{html.escape(x["username"])}</a></div>'
            f'<div class="text-xs text-gray-400">{html.escape(x.get("display_name") or "")}</div></div>'
            f'<button class="text-gray-400 hover:text-red-400 text-xs transition" '
            f'hx-post="/cohort/{cid}/remove/{html.escape(x["username"])}" '
            'hx-target="#cohort-settings-members" hx-swap="innerHTML" '
            f'hx-confirm="Remove @{html.escape(x["username"])}?">✕</button></div>'
            '<div class="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-gray-400">'
            f'<span class="whitespace-nowrap">{fmt(x["tweets"])} tw</span>'
            f'<span class="text-pink-400 whitespace-nowrap">❤ Likes {fmt(x["likes"])}</span>'
            f'<span class="text-blue-400 whitespace-nowrap">👁 Views {fmt(x["views"])}</span></div>'
            '<div class="mt-1 text-xs">'
            f'<span class="text-gray-400 italic" id="note-{html.escape(x["username"])}">{html.escape(x.get("note") or "")}</span> '
            f'<span class="text-gray-400 cursor-pointer hover:text-gray-400" '
            f'hx-get="/cohort/{cid}/note-edit/{html.escape(x["username"])}" '
            f'hx-target="#note-{html.escape(x["username"])}" hx-swap="outerHTML transition:true">✏️</span></div>'
            '</div>'
        )
        for x in d
    )


def _leaderboard_query(cid, period):
    """Run the leaderboard CTE for a cohort+period. Returns list[dict]."""
    interval = _PERIOD_INTERVALS.get(period, "7 days")
    days = _PERIOD_DAYS.get(period, 7)
    return q(
        f"""
        WITH per_account AS (
          SELECT
            a.id, a.username, a.display_name, a.avatar_url, a.followers_count,
            COUNT(t.tweet_id) FILTER (WHERE NOT t.is_retweet)                       AS posts,
            COUNT(t.tweet_id) FILTER (WHERE t.is_reply AND NOT t.is_retweet)        AS replies,
            COUNT(t.tweet_id) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet)    AS originals,
            COALESCE(SUM(t.likes)    FILTER (WHERE NOT t.is_retweet), 0)::bigint    AS likes,
            COALESCE(SUM(t.views)    FILTER (WHERE NOT t.is_retweet), 0)::bigint    AS views,
            COALESCE(SUM(t.retweets) FILTER (WHERE NOT t.is_retweet), 0)::bigint    AS retweets_count,
            COALESCE(SUM(t.replies)  FILTER (WHERE NOT t.is_retweet), 0)::bigint    AS replies_received
          FROM cohort_members cm
          JOIN accounts a ON a.id = cm.account_id
          LEFT JOIN tweets t ON t.author_account_id = a.id
                            AND t.created_at >= NOW() - INTERVAL '{interval}'
          WHERE cm.cohort_id = %s
          GROUP BY a.id
        ),
        cohort_totals AS (
          SELECT NULLIF(SUM(likes), 0)::float AS total_likes FROM per_account
        ),
        days AS (
          SELECT generate_series(0, 6) AS d
        ),
        daily AS (
          SELECT
            p.id AS account_id, days.d,
            COALESCE(SUM(t.likes), 0)::int AS day_likes
          FROM per_account p
          CROSS JOIN days
          LEFT JOIN tweets t ON t.author_account_id = p.id
                             AND NOT t.is_retweet
                             AND date_trunc('day', t.created_at)
                                 = date_trunc('day', NOW() - days.d * INTERVAL '1 day')
          GROUP BY p.id, days.d
        ),
        spark AS (
          SELECT account_id,
                 array_agg(day_likes ORDER BY d DESC)::int[] AS daily_likes
          FROM daily GROUP BY account_id
        )
        SELECT
          p.id, p.username, p.display_name, p.avatar_url,
          p.followers_count, p.posts, p.replies, p.originals,
          p.likes, p.views, p.retweets_count, p.replies_received,
          CASE WHEN p.views > 0 THEN p.likes::float / p.views ELSE NULL END   AS engagement_rate,
          CASE WHEN (p.replies + p.originals) > 0
               THEN p.replies::float / (p.replies + p.originals)
               ELSE NULL END                                                  AS reply_ratio,
          CASE WHEN ct.total_likes IS NOT NULL AND ct.total_likes > 0
               THEN p.likes::float / ct.total_likes * 100.0
               ELSE NULL END                                                  AS voice_share,
          CASE WHEN p.posts > 0
               THEN p.likes::float / p.posts
               ELSE NULL END                                                  AS likes_per_post,
          CASE WHEN p.posts > 0
               THEN p.views::float / p.posts
               ELSE NULL END                                                  AS views_per_post,
          -- Composite = WER (55 pct) + Voice share (30 pct) + Activity cadence (15 pct).
          -- WER: quality per impression; falls back to per-follower if views=0.
          -- No follower-count penalty — big accounts aren't punished for having an audience.
          LEAST(
            CASE WHEN p.views > 0 THEN
              (p.likes * 1.0 + p.retweets_count * 2.0 + p.replies_received * 4.0)
              / p.views * 100.0
            ELSE
              (p.likes * 1.0 + p.retweets_count * 2.0 + p.replies_received * 4.0)
              / GREATEST(p.followers_count, 1) * 100.0
            END, 25.0
          ) / 25.0 * 0.55
          + COALESCE(p.likes::float / NULLIF(ct.total_likes, 0) * 100.0, 0.0) * 0.30
          + LEAST(p.posts::float / GREATEST({days}.0, 1.0) / 3.0, 1.0) * 0.15
                                                                              AS composite,
          COALESCE(spark.daily_likes, ARRAY[0,0,0,0,0,0,0]::int[])            AS daily_likes
        FROM per_account p
        CROSS JOIN cohort_totals ct
        LEFT JOIN spark ON spark.account_id = p.id
        ORDER BY composite DESC
        """,
        (cid,),
    )


@router.get("/cohort/{cid}/export")
def cohort_export(cid: int, r: Request, format: str = "csv"):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return PlainTextResponse("Not found", status_code=404)
    d = q(
        """
        SELECT a.username, a.display_name, a.followers_count,
               count(t.tweet_id) AS tweets,
               COALESCE(sum(t.likes) FILTER (WHERE NOT t.is_retweet), 0)::int AS likes,
               COALESCE(sum(t.views) FILTER (WHERE NOT t.is_retweet), 0)::int AS views
        FROM cohort_members cm JOIN accounts a ON a.id = cm.account_id
        LEFT JOIN tweets t ON t.author_account_id = a.id
        WHERE cm.cohort_id = %s
        GROUP BY a.id, a.username ORDER BY likes DESC
        """,
        (cid,),
    )
    if format == "json":
        return PlainTextResponse(
            json.dumps(
                [
                    {
                        "username": x["username"],
                        "display_name": x["display_name"],
                        "followers": x["followers_count"],
                        "tweets": x["tweets"],
                        "likes": x["likes"],
                        "views": x["views"],
                    }
                    for x in d
                ],
                indent=2,
            ),
            media_type="application/json",
        )
    import csv as _csv
    import io
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["username", "display_name", "followers", "tweets", "likes", "views"])
    for x in d:
        w.writerow(
            [x["username"], x["display_name"], x["followers_count"], x["tweets"], x["likes"], x["views"]]
        )
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")


@router.get("/cohorts", response_class=HTMLResponse)
def cohorts(r: Request, days: int = 0):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    d = q(
        """
        SELECT c.id, c.name, c.slug, c.created_at, count(cm.account_id) AS mc
        FROM cohorts c LEFT JOIN cohort_members cm ON cm.cohort_id = c.id
        WHERE c.user_id = %s
        GROUP BY c.id ORDER BY c.created_at DESC
        """,
        (user["id"],),
    )
    cards = "".join(
        (
            f'<a href="/cohort/{x["id"]}" class="block bg-gray-900 rounded-xl p-5 border border-gray-800 hover:border-emerald-500/30 transition">'
            f'<div class="text-emerald-400 font-semibold">{html.escape(x["name"])}</div>'
            f'<div class="text-xs text-gray-500 mt-1">{html.escape(x["slug"])}</div>'
            f'<div class="mt-2 text-sm text-gray-400">{x["mc"]} members</div></a>'
        )
        for x in d
    ) or (
        '<div class="col-span-full text-center py-12">'
        '<p class="text-gray-500">No cohorts yet.</p>'
        '<a href="/discover" class="text-emerald-400 hover:text-emerald-300 mt-2 inline-block">Discover one →</a></div>'
    )
    return header_html(days, user["username"], is_admin=user.get("is_admin", False)) + (
        "<h1 class='text-xl font-semibold mb-6'>Your Cohorts</h1>"
        f"<div class='grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4'>{cards}</div>"
    ) + HF


@router.get("/cohort/{cid}", response_class=HTMLResponse)
def cohort_det(cid: int, r: Request, days: int = 0):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    c = q("SELECT * FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"]))
    if not c:
        return header_html(0) + "<p class='text-gray-500'>Not found</p>" + HF
    c = c[0]
    d = q(
        """
        SELECT a.id, a.username, a.display_name, a.avatar_url, a.followers_count,
               count(t.tweet_id) AS tweets,
               COALESCE(sum(t.likes) FILTER (WHERE NOT t.is_retweet), 0)::int AS likes,
               COALESCE(sum(t.views) FILTER (WHERE NOT t.is_retweet), 0)::int AS views
        FROM cohort_members cm JOIN accounts a ON a.id = cm.account_id
        LEFT JOIN tweets t ON t.author_account_id = a.id
        WHERE cm.cohort_id = %s
        GROUP BY a.id, a.username, a.display_name, a.avatar_url, a.followers_count
        ORDER BY likes DESC
        """,
        (cid,),
    )
    cards = "".join(
        (
            '<div class="bg-gray-900 rounded-xl p-4 border border-gray-800">'
            '<div class="flex items-start justify-between"><div class="flex-1">'
            f'<div class="text-emerald-400 font-semibold">'
            f'<a href="/account/{html.escape(x["username"])}" class="hover:underline">@{html.escape(x["username"])}</a></div>'
            f'<div class="text-sm text-gray-400">{html.escape(x.get("display_name") or "")}</div></div>'
            f'<button class="text-gray-400 hover:text-red-400 text-xs transition" '
            f'hx-post="/cohort/{cid}/remove/{html.escape(x["username"])}" '
            'hx-target="#cohort-settings-members" hx-swap="innerHTML" '
            f'hx-confirm="Remove @{html.escape(x["username"])}?">✕</button></div>'
            '<div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs bg-gray-800/50 rounded-lg p-2">'
            f'<span class="text-gray-300 whitespace-nowrap">{fmt(x["tweets"])} tweets</span>'
            f'<span class="text-pink-400 whitespace-nowrap">❤ Likes {fmt(x["likes"])}</span>'
            f'<span class="text-blue-400 whitespace-nowrap">👁 Views {fmt(x["views"])}</span>'
            f'<span class="text-gray-400 whitespace-nowrap">{fmt(x["followers_count"])} followers</span>'
            '</div></div>'
        )
        for x in d
    )
    name_safe = html.escape(c["name"])
    pfp_safe = html.escape(c.get("pfp_url") or "")
    # ecosystem_handles is a JSONB list; render as comma-separated for editing.
    eco_list = c.get("ecosystem_handles") or []
    if isinstance(eco_list, str):
        try:
            import json as _json
            eco_list = _json.loads(eco_list) or []
        except Exception:
            eco_list = []
    eco_display = ", ".join(eco_list)
    eco_safe = html.escape(eco_display)
    ecosystem_block = (
        '<div class="mt-4 pt-4 border-t border-gray-800">'
        '<div class="flex items-center justify-between mb-2">'
        '<span class="text-xs text-gray-500">🌐 Ecosystem handles '
        '<span class="text-gray-400 normal-case">— allowlist real entities the '
        'insights can reference but aren\'t tracked here</span></span></div>'
        f'<form hx-post="/cohort/{cid}/ecosystem-handles" hx-target="#eco-status" '
        'hx-swap="innerHTML" class="flex gap-2">'
        '<input type="text" name="handles" '
        f'value="{eco_safe}" placeholder="solana, toly, solflare" '
        'class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-xs flex-1 font-mono">'
        '<button type="submit" class="text-xs bg-emerald-600 hover:bg-emerald-500 '
        'text-white rounded-lg px-3 py-1.5">Save</button>'
        '</form><div id="eco-status" class="mt-1 text-[11px] text-emerald-400"></div>'
        '</div>'
    )
    share_block = (
        f'<div class="flex items-center gap-2"><input type="text" value="/share/{html.escape(c["share_token"])}" '
        'class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-300 flex-1" readonly>'
        f'<button class="text-xs bg-gray-700 hover:bg-gray-600 text-white rounded-lg px-2 py-1.5" '
        f'onclick="navigator.clipboard.writeText(location.origin+\'/share/{html.escape(c["share_token"])}\')">Copy</button>'
        f'<button class="text-xs text-red-400 hover:text-red-300 px-2 py-1.5" '
        f'hx-post="/cohort/{cid}/revoke-token" hx-target="#share-section" hx-swap="innerHTML">Revoke</button></div>'
        if c.get("share_token") else
        f'<button class="text-xs bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-3 py-1.5 transition" '
        f'hx-post="/cohort/{cid}/share-token" hx-target="#share-section" hx-swap="innerHTML">🔗 Generate share link</button>'
    )
    member_html = f'<div id="cohort-settings-members" class="space-y-2">{cards}</div>'
    settings = (
        '<div id="cohort-settings" class="bg-gray-900 rounded-xl border border-gray-800 p-5 mb-6">'
        '<div class="flex items-center justify-between mb-4">'
        f'<h2 class="text-lg font-semibold" id="cohort-name-display">'
        f'<span hx-get="/cohort/{cid}/name-edit" hx-trigger="click" hx-swap="outerHTML transition:true" '
        f'class="cursor-pointer hover:text-emerald-400 transition">{name_safe} ✏️</span></h2>'
        f'<span class="text-xs text-gray-500">{len(d)} members '
        f'<a href="/cohort/{cid}/export" class="text-emerald-400 hover:underline ml-2">⬇ CSV</a> '
        f'<a href="/cohort/{cid}/export?format=json" class="text-emerald-400 hover:underline">JSON</a></span>'
        '</div>'
        '<div class="flex items-center gap-3 mb-4 p-3 bg-gray-800/50 rounded-lg">'
        f'<img src="{pfp_safe}" class="w-10 h-10 rounded-full bg-gray-700 object-cover" '
        'onerror="this.style.display=\'none\'" loading="lazy">'
        f'<form hx-post="/cohort/{cid}/pfp" hx-target="#pfp-status" hx-swap="innerHTML" class="flex-1 flex gap-2">'
        f'<input type="text" name="pfp_url" value="{pfp_safe}" placeholder="PFP image URL" '
        'class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm flex-1">'
        '<button type="submit" class="bg-gray-700 hover:bg-gray-600 text-gray-300 rounded-lg px-3 py-1.5 text-xs">Set</button>'
        '</form></div><div id="pfp-status"></div>'
        f'{member_html}'
        f'<form hx-post="/cohort/{cid}/add" hx-target="#cohort-settings-members" hx-swap="innerHTML" class="flex gap-2 mt-4">'
        '<input type="text" name="handle" placeholder="+ add handle" '
        'class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm flex-1">'
        '<button type="submit" class="bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-4 py-1.5 text-xs font-medium">Add</button>'
        '</form>'
        # Merge from existing cohorts / tracked accounts. Lazy-loaded only
        # when the user expands the panel — keeps initial page load cheap.
        '<details class="mt-4 pt-4 border-t border-gray-800 group">'
        '<summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-300 flex items-center justify-between">'
        '<span>🔀 ' + tip(
            'Merge from another cohort or tracked accounts',
            'Pull entire cohorts into this one with one click, or add individual '
            'accounts you\'ve already tracked elsewhere. Duplicates are skipped '
            'automatically. Optional: delete the source cohort after merging — '
            'any profiles pointing to it will rebind to this cohort.'
        ) + '</span>'
        '<span class="text-gray-400 group-open:rotate-90 transition-transform">▶</span>'
        '</summary>'
        f'<div class="mt-3" hx-get="/cohort/{cid}/merge-candidates" '
        'hx-trigger="toggle from:closest details, load delay:0s once" '
        'hx-swap="innerHTML">'
        '<div class="text-xs text-gray-400 italic">Loading…</div>'
        '</div>'
        '</details>'
        f'{ecosystem_block}'
        '<div class="mt-4 pt-4 border-t border-gray-800">'
        '<div class="flex items-center justify-between mb-2"><span class="text-xs text-gray-500">🔗 Share</span></div>'
        f'<div id="share-section">{share_block}</div></div>'
        '<div class="mt-4 pt-4 border-t border-gray-800">'
        f'<button class="text-xs text-red-400 hover:text-red-300 hover:bg-red-900/20 rounded-lg px-3 py-1.5 transition" '
        f'hx-post="/cohort/{cid}/delete" hx-target="#cohort-settings" hx-swap="outerHTML transition:true" '
        f'hx-confirm="Delete this cohort and all its profiles? This cannot be undone.">🗑 Delete Cohort</button>'
        '</div></div>'
    )
    # Cohort vibe bar
    cvibe_val, cvibe_desc = _vibe("cohort", c["id"], f"{days}d")
    cvibe_color = "bg-emerald-500" if cvibe_val >= 65 else "bg-yellow-500" if cvibe_val >= 40 else "bg-red-500"
    cvibe_html = (
        '<div class="flex items-center gap-3 mb-4 bg-gray-900 rounded-xl border border-gray-800 p-3">'
        '<div class="flex items-center gap-2 min-w-0">'
        f'<span class="text-lg">🔮</span>'
        f'<span class="text-xs text-gray-500">Vibe</span>'
        f'<span class="text-sm font-bold whitespace-nowrap {"text-emerald-400" if cvibe_val >= 65 else "text-yellow-400" if cvibe_val >= 40 else "text-red-400"}">{cvibe_val}/100</span>'
        '</div>'
        f'<div class="flex-1 bg-gray-800 rounded-full h-2 overflow-hidden">'
        f'<div class="{cvibe_color} h-full rounded-full transition-all duration-500" style="width:{cvibe_val}%"></div>'
        '</div>'
        f'<p class="text-sm text-gray-300 leading-relaxed max-w-prose">{html.escape(cvibe_desc)}</p>'
        '</div>'
    )
    ip = f"{days}d" if days else "7d"
    cid_str = str(c["id"])
    _period_links = "".join(
        f'<a href="/cohort/{cid_str}?days={d}" class="px-2 py-1 rounded text-xs transition '
        + ("bg-purple-700 text-white" if d == days else "bg-gray-800 text-gray-400 hover:text-white")
        + f'">{l}</a>'
        for d, l in [(1, "24h"), (7, "7d"), (14, "14d"), (30, "30d")]
    )
    return header_html(days, c["name"], is_admin=user.get("is_admin", False)) + (
        f'{cvibe_html}'
        f'<div class="flex items-center gap-2 mb-4 bg-gray-900 rounded-xl border border-gray-800 p-3">'
        f'<span class="text-xs font-semibold text-gray-400">✨</span>'
        f'{_period_links}'
        f'<div class="ml-auto">'
        f'<style>.ld-htmx.htmx-request {{ display: inline-flex !important; }}</style>'
        f'<button hx-post="/cohort/{c["id"]}/generate-insights?period={ip}" '
        f'hx-target="#tab-insights" hx-swap="innerHTML" '
        'class="text-xs px-3 py-1.5 rounded bg-purple-700 hover:bg-purple-600 text-white transition" '
        '_="on click toggle .hidden on #chrt-ld then wait for htmx:afterOnLoad then add .hidden to #chrt-ld">'
        '✨ Generate</button>'
        f'<span id="chrt-ld" class="hidden text-xs text-purple-400 ml-2">⟳ ~10-15s</span>'
        '</div></div>'
        f'<div x-data="{{ tab: \'members\' }}">'
        '<div class="flex gap-1 mb-4 border-b border-gray-800 pb-2">'
        f'<button @click="tab=\'members\'" :class="tab===\'members\'?\'bg-emerald-700 text-white\':\'text-gray-400 hover:text-white\'" '
        'class="px-3 py-1.5 text-xs rounded-t transition font-medium">👥 Members</button>'
        f'<button @click="tab=\'leaderboard\'" :class="tab===\'leaderboard\'?\'bg-emerald-700 text-white\':\'text-gray-400 hover:text-white\'" '
        'class="px-3 py-1.5 text-xs rounded-t transition">📊 Leaderboard</button>'
        f'<button @click="tab=\'insights\'" :class="tab===\'insights\'?\'bg-emerald-700 text-white\':\'text-gray-400 hover:text-white\'" '
        'class="px-3 py-1.5 text-xs rounded-t transition">✨ Insights <span id="cohort-insights-age" class="text-gray-400 font-normal normal-case"></span></button>'
        '</div>'
        f'<div x-show="tab===\'members\'">'
        f'<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">{cards}</div>'
        '</div>'
        f'<template x-if="tab===\'leaderboard\'">'
        f'<div hx-get="/cohort/{c["id"]}/leaderboard?period={ip}&sort=composite&dir=desc" '
        'hx-trigger="load" hx-swap="outerHTML transition:true"></div>'
        '</template>'
        f'<div x-show="tab===\'insights\'">'
        f'<div id="tab-insights" hx-get="/cohort/{c["id"]}/insights?period={ip}" hx-swap="innerHTML" class="text-center py-8 text-gray-500 text-sm">'
        '<p class="text-gray-500 text-xs mb-4">✨ Click the Insights button above to generate.</p>'
        f'</div></div></div>'
    ) + HF


@router.post("/scrape-cohort/{cid}", response_class=HTMLResponse)
def scrape_cohort(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<p class='text-red-400 text-sm'>Not found</p>"
    cn = q("SELECT name FROM cohorts WHERE id=%s", (cid,))[0]["name"]
    ac = q("SELECT count(*)::int AS c FROM cohort_members WHERE cohort_id=%s", (cid,))[0]["c"]
    batch_script = os.path.join(COLLECTOR_DIR, "batch.py")
    session_id = ss_start(
        user_id=user["id"],
        session_type="batch",
        cohort_id=cid,
        target_handle=cn,
        progress_total=ac,
    )
    env = {**os.environ, "VIBECHECX_SCRAPE_SESSION_ID": str(session_id)}
    subprocess.Popen(
        ["python3", batch_script, str(cid)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    return (
        '<div class="flex items-center gap-2 text-xs bg-cyan-900/30 border border-cyan-800 rounded-lg px-3 py-2 mt-4" '
        'hx-get="/scrape-progress" hx-trigger="every 2s" hx-swap="outerHTML transition:true">'
        '<div class="animate-pulse inline-block w-2 h-2 rounded-full bg-cyan-400"></div>'
        f'<span class="text-cyan-300">Starting batch scrape of {html.escape(cn)}…</span></div>'
    )


@router.get("/cohort/{cid}/leaderboard", response_class=HTMLResponse)
def cohort_leaderboard(cid: int, r: Request, period: str = "7d", sort: str = "composite", dir: str = "desc"):
    """Pure-SQL leaderboard. No cache, no compute step, no spinner."""
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<p class='text-red-400'>Not found</p>"
    if period not in _PERIOD_INTERVALS:
        period = "7d"
    if dir not in ("asc", "desc"):
        dir = "desc"

    lb = _leaderboard_query(cid, period)
    # Client-side ordering by chosen sort key.
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

    if not lb:
        return (
            '<div class="text-center py-12">'
            '<div class="text-4xl mb-2 opacity-40">📊</div>'
            '<p class="text-gray-400 text-sm mb-3">No members or no tweets yet for this period.</p>'
            '<div class="flex justify-center">'
            + scrape_depth_picker_html(hx_target="#trigger-status-coh",
                                       submit_label="↻ Scrape this cohort")
            + '</div>'
            '<span id="trigger-status-coh"></span></div>'
        )

    # Top-cell accent: column-leader gets a left-border on that cell.
    top_by = {
        "composite": max((row.get("composite") or -1) for row in lb),
        "engagement_rate": max((row.get("engagement_rate") or -1) for row in lb),
        "voice_share": max((row.get("voice_share") or -1) for row in lb),
        "likes": max((row.get("likes") or -1) for row in lb),
        "views": max((row.get("views") or -1) for row in lb),
        "likes_per_post": max((row.get("likes_per_post") or -1) for row in lb),
        "views_per_post": max((row.get("views_per_post") or -1) for row in lb),
        "reply_ratio": max((row.get("reply_ratio") or -1) for row in lb),
        "posts": max((row.get("posts") or -1) for row in lb),
        "followers_count": max((row.get("followers_count") or -1) for row in lb),
    }

    def _cell(value, key, fmt_str="{:.2f}", suffix=""):
        if value is None:
            return '<td class="py-2 px-2 text-right text-gray-500 text-xs">—</td>'
        is_top = (value == top_by.get(key)) and (value is not None) and (value != -1)
        accent = ('<span class="absolute left-0 top-1 bottom-1 w-0.5 bg-emerald-400 rounded"></span>'
                  if is_top else '')
        try:
            formatted = fmt_str.format(value) + suffix
        except (ValueError, TypeError):
            formatted = str(value) + suffix
        return f'<td class="py-2 px-2 text-right relative text-sm text-gray-200">{accent}{formatted}</td>'

    rows_html = ""
    for i, row in enumerate(lb, 1):
        avatar = row.get("avatar_url") or ""
        avatar_html = (
            f'<img src="{html.escape(avatar)}" class="w-7 h-7 rounded-full bg-gray-700 object-cover flex-shrink-0" '
            'onerror="this.style.display=\'none\'" loading="lazy">'
        )
        spark = row.get("daily_likes") or []
        spark_html = _sparkline_svg(spark)
        sentiment_color = "text-emerald-400" if (row.get("engagement_rate") or 0) > 0.02 else "text-gray-400"
        rows_html += (
            '<tr class="border-b border-gray-800 hover:bg-gray-800/40 cursor-pointer transition" '
            f"onclick=\"window.location='/account/{html.escape(row['username'])}'\">"
            f'<td class="py-2 px-2 text-center text-xs text-gray-500">#{i}</td>'
            f'<td class="py-2 px-2"><div class="flex items-center gap-2">{avatar_html}'
            f'<a href="/account/{html.escape(row["username"])}" class="text-emerald-400 hover:underline text-sm font-medium">'
            f'@{html.escape(row["username"])}</a></div></td>'
            + _cell(row.get("composite"), "composite", "{:.3f}")
            + _cell(
                (row.get("engagement_rate") or 0) * 100 if row.get("engagement_rate") is not None else None,
                "engagement_rate", "{:.2f}", suffix="%",
              )
            + _cell(row.get("voice_share"), "voice_share", "{:.1f}", suffix="%")
            + _cell(row.get("likes"), "likes", "{:,}")
            + _cell(row.get("views"), "views", "{:,}")
            + _cell(row.get("likes_per_post"), "likes_per_post", "{:.0f}")
            + _cell(row.get("views_per_post"), "views_per_post", "{:,.0f}")
            + _cell(
                (row.get("reply_ratio") or 0) * 100 if row.get("reply_ratio") is not None else None,
                "reply_ratio", "{:.0f}", suffix="%",
              )
            + f'<td class="py-2 px-2 text-center {sentiment_color}">{spark_html}</td>'
            + _cell(row.get("posts"), "posts", "{:d}")
            + _cell(row.get("followers_count"), "followers_count", "{:d}")
            + '</tr>'
        )

    def _sort_link(key, label, tooltip_text=""):
        is_active = sort == key
        if is_active:
            next_dir = "asc" if dir == "desc" else "desc"
            glyph = " ▲" if dir == "asc" else " ▼"
        else:
            next_dir = "desc"
            glyph = ""
        cls = "text-emerald-400" if is_active else "text-gray-500 hover:text-emerald-400"
        label_html = tip(label, tooltip_text) if tooltip_text else label
        return (
            f'<a hx-get="/cohort/{cid}/leaderboard?period={period}&sort={key}&dir={next_dir}" '
            f'hx-target="#tab-leaderboard" hx-swap="outerHTML transition:true" '
            f'class="cursor-pointer select-none {cls}">{label_html}{glyph}</a>'
        )

    def _period_button(p):
        cls = "bg-emerald-700 text-white" if period == p else "text-gray-400 hover:text-white"
        return (
            f'<a hx-get="/cohort/{cid}/leaderboard?period={p}&sort={sort}&dir={dir}" '
            f'hx-target="#tab-leaderboard" hx-swap="outerHTML transition:true" '
            f'class="px-3 py-1 text-xs rounded {cls}">{p}</a>'
        )

    period_seg = ''.join(_period_button(p) for p in ("24h", "7d", "14d", "30d", "all"))

    composite_tip = (
        "Weighted blend: 55% engagement quality (likes+retweets×2+replies×4 per view), "
        "30% share of cohort's total likes, 15% posting cadence (3 posts/day = full marks). "
        "No follower-count penalty."
    )
    eng_tip = (
        "Likes per view (or per follower when views unavailable). "
        "How much of the audience that saw the post actually engaged."
    )
    voice_tip = (
        "This account's likes as % of all likes in the cohort during this period. "
        "Measures absolute presence, not efficiency."
    )
    reply_tip = (
        "Replies ÷ (replies + originals). "
        "High = mostly conversational; low = mostly broadcasting original content."
    )
    posts_tip = "Total non-retweet posts published during the period."
    followers_tip = "Follower count at the time of last scrape."
    likes_tip = "Total likes earned during the period. Pure absolute — who moved the most engagement."
    views_tip = "Total impressions during the period. Who reached the most eyeballs in absolute terms."
    lpp_tip = (
        "Likes ÷ posts. Proportional — audience-size neutral. "
        "One great post with 500 likes beats 10 posts with 50 likes each."
    )
    vpp_tip = (
        "Views ÷ posts. Proportional reach per piece of content. "
        "Rewards quality over quantity; not affected by follower count."
    )

    # Column group labels: #, Account, Composite, Eng rate, Voice% = 5 lead cols
    # then Likes, Views = 2 absolute; Likes/post, Views/post = 2 proportional
    # then Reply%, 7d spark, Posts, Followers = 4 trail cols
    grp_lead = '<th class="py-1 px-2 border-b border-gray-800/50" colspan="5"></th>'
    grp_abs = '<th class="py-1 px-2 text-center text-[9px] text-gray-600 uppercase tracking-widest border-b border-gray-800/50 border-l border-l-gray-700" colspan="2">— absolute —</th>'
    grp_prop = '<th class="py-1 px-2 text-center text-[9px] text-gray-600 uppercase tracking-widest border-b border-gray-800/50 border-l border-l-gray-700" colspan="2">— per post —</th>'
    grp_trail = '<th class="py-1 px-2 border-b border-gray-800/50" colspan="4"></th>'

    return (
        f'<div id="tab-leaderboard">'
        '<div class="mb-4 flex items-center justify-between flex-wrap gap-2">'
        '<div class="text-xs text-gray-500">'
        f'{len(lb)} members · period: <span class="text-gray-300">{period}</span>'
        '</div>'
        '<div class="inline-flex rounded-lg bg-gray-900 border border-gray-800 p-0.5">'
        f'{period_seg}'
        '</div>'
        '</div>'
        '<div class="overflow-x-auto rounded-lg border border-gray-800">'
        '<table class="w-full text-sm table-auto">'
        '<thead class="bg-gray-900/80">'
        f'<tr>{grp_lead}{grp_abs}{grp_prop}{grp_trail}</tr>'
        '<tr class="text-[11px] text-gray-500 uppercase tracking-wider border-b border-gray-800">'
        '<th class="py-2 px-2 text-center">#</th>'
        '<th class="py-2 px-2 text-left">Account</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("composite", "Composite", composite_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("engagement_rate", "Eng rate", eng_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("voice_share", "Voice %", voice_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("likes", "Likes", likes_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("views", "Views", views_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("likes_per_post", "Likes/post", lpp_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("views_per_post", "Views/post", vpp_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("reply_ratio", "Reply %", reply_tip)}</th>'
        '<th class="py-2 px-2 text-center text-gray-500">7d</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("posts", "Posts", posts_tip)}</th>'
        f'<th class="py-2 px-2 text-right">{_sort_link("followers", "Followers", followers_tip)}</th>'
        '</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div></div>'
    )


@router.post("/cohort/{cid}/generate-insights", response_class=HTMLResponse)
def cohort_generate_insights(cid: int, r: Request, period: str = "7d"):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<span class='text-red-400'>Not found</span>"
    from web.render_insights import _ai_error_card  # noqa: E402
    insight, *_ = vi.cached_insights("cohort", cid, period, force=True)
    if not insight:
        return _ai_error_card(
            f"/cohort/{cid}/generate-insights?period={period}",
            "tab-insights",
        )
    return cohort_insights(cid, r, period)


@router.get("/cohort/{cid}/insights", response_class=HTMLResponse)
def cohort_insights(cid: int, r: Request, period: str = "7d"):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<p class='text-red-400'>Not found</p>"
    cohort = q("SELECT name FROM cohorts WHERE id=%s", (cid,))
    scope_display = cohort[0]["name"] if cohort else f"cohort#{cid}"
    result, provider, _from_cache, age_min = vi.cached_insights("cohort", cid, period, generate_if_missing=False)
    return _render_insights(
        result=result, scope_type="cohort", scope_key=cid, scope_display=scope_display,
        period=period, provider=provider, age_min=age_min,
        regen_endpoint=f"/cohort/{cid}/generate-insights?period={period}",
        period_get_endpoint=f"/cohort/{cid}/insights",
        target_id="tab-insights",
    )


@router.get("/cohort/{cid}/insights/export")
def cohort_insights_export(cid: int, r: Request, period: str = "7d", format: str = "json"):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        from fastapi.responses import Response
        return Response("Not found", status_code=404, media_type="text/plain")
    cohort = q("SELECT name FROM cohorts WHERE id=%s", (cid,))
    name = cohort[0]["name"] if cohort else f"cohort#{cid}"
    return _insight_export_response("cohort", cid, name, period, format.lower())


@router.post("/cohort/{cid}/pfp", response_class=HTMLResponse)
async def cohort_set_pfp(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    f = await r.form()
    url = (f.get("pfp_url") or "").strip()
    q("UPDATE cohorts SET pfp_url=%s WHERE id=%s AND user_id=%s", (url, cid, user["id"]))
    return (
        '<div class="flex items-center gap-3">'
        f'<img src="{html.escape(url)}" class="w-10 h-10 rounded-full bg-gray-800 object-cover" '
        'onerror="this.style.display=\'none\'">'
        '<span class="text-xs text-gray-500">PFP updated</span></div>'
    )


@router.get("/cohort/{cid}/merge-candidates", response_class=HTMLResponse)
def cohort_merge_candidates(cid: int, r: Request):
    """Returns HTML fragment listing things the user can merge INTO this cohort:
      1. Their OTHER cohorts (with member counts + sample handles)
      2. Accounts they've ever tracked (via any cohort or single profile) that
         aren't yet in this cohort

    Used by both the cohort settings panel and the wizard step 3.
    """
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<p class='text-red-400 text-sm'>Not found</p>"

    # Other cohorts owned by this user
    others = q(
        """
        SELECT c.id, c.name,
               COUNT(cm.account_id)::int AS members,
               COALESCE(
                 (SELECT string_agg(a.username, ', ' ORDER BY a.followers_count DESC NULLS LAST)
                  FROM (SELECT a.username, a.followers_count
                        FROM cohort_members cm2
                        JOIN accounts a ON a.id = cm2.account_id
                        WHERE cm2.cohort_id = c.id
                        ORDER BY a.followers_count DESC NULLS LAST LIMIT 3) a),
                 ''
               ) AS sample
        FROM cohorts c
        LEFT JOIN cohort_members cm ON cm.cohort_id = c.id
        WHERE c.user_id = %s AND c.id != %s
        GROUP BY c.id, c.name
        ORDER BY members DESC, c.name
        """,
        (user["id"], cid),
    )

    # Accounts the user has touched, that aren't yet in THIS cohort. Touched =
    # in any of their cohorts, OR referenced by a single profile they own.
    tracked = q(
        """
        SELECT DISTINCT a.id, a.username, a.display_name, a.followers_count
        FROM accounts a
        WHERE (
            a.id IN (
              SELECT DISTINCT cm.account_id FROM cohort_members cm
              JOIN cohorts c ON c.id = cm.cohort_id
              WHERE c.user_id = %s
            )
            OR LOWER(a.username) IN (
              SELECT LOWER(p.target_handle) FROM profiles p
              WHERE p.user_id = %s AND p.target_handle IS NOT NULL AND p.target_handle != ''
            )
        )
        AND a.id NOT IN (
          SELECT account_id FROM cohort_members WHERE cohort_id = %s
        )
        ORDER BY a.followers_count DESC NULLS LAST
        LIMIT 50
        """,
        (user["id"], user["id"], cid),
    )

    if not others and not tracked:
        return (
            '<div class="text-sm text-gray-500 italic py-3 text-center">'
            "Nothing to merge yet — when you have other cohorts or "
            "tracked accounts, they'll appear here.</div>"
        )

    cohort_options = "".join(
        f'<label class="flex items-start gap-2 p-2 rounded hover:bg-gray-800/40 cursor-pointer">'
        f'<input type="checkbox" name="source_cohort_ids" value="{c["id"]}" '
        'class="accent-emerald-500 mt-1">'
        f'<div class="flex-1 min-w-0">'
        f'<div class="text-sm text-gray-200">{html.escape(c["name"])} '
        f'<span class="text-xs text-gray-500">({c["members"]} members)</span></div>'
        + (f'<div class="text-[11px] text-gray-400 truncate">{html.escape(c.get("sample") or "")}</div>'
           if c.get("sample") else "")
        + '</div></label>'
        for c in others
    )

    account_options = "".join(
        f'<label class="flex items-center gap-2 p-2 rounded hover:bg-gray-800/40 cursor-pointer">'
        f'<input type="checkbox" name="extra_account_ids" value="{a["id"]}" '
        'class="accent-emerald-500">'
        f'<div class="flex-1 min-w-0 flex items-center gap-2">'
        f'<span class="text-sm text-emerald-400">@{html.escape(a["username"])}</span>'
        + (f'<span class="text-xs text-gray-400 truncate">{html.escape(a.get("display_name") or "")}</span>'
           if a.get("display_name") else "")
        + f'<span class="ml-auto text-[11px] text-gray-400">{fmt_compact(a.get("followers_count") or 0)} followers</span>'
        + '</div></label>'
        for a in tracked
    )

    parts = []
    if others:
        parts.append(
            '<div class="mb-3">'
            f'<div class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-1">'
            f'Pull from your other cohorts ({len(others)})</div>'
            f'<div class="space-y-0.5 max-h-48 overflow-y-auto border border-gray-800 rounded-lg p-1">'
            f'{cohort_options}</div></div>'
        )
    if tracked:
        parts.append(
            '<div class="mb-3">'
            f'<div class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-1">'
            f'Or pick individual accounts you already track ({len(tracked)})</div>'
            f'<div class="space-y-0.5 max-h-48 overflow-y-auto border border-gray-800 rounded-lg p-1">'
            f'{account_options}</div></div>'
        )

    delete_toggle = (
        '<label class="flex items-center gap-2 text-xs text-gray-400 mt-2 cursor-pointer">'
        '<input type="checkbox" name="delete_sources" value="1" class="accent-red-500">'
        '<span>Also delete source cohort(s) after merge '
        '<span class="text-[10px] text-gray-400">— profiles pointing to them will rebind to this cohort</span>'
        '</span></label>'
        if others else ""
    )

    submit_btn = (
        '<button type="submit" '
        'class="w-full mt-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg '
        'px-4 py-2 text-sm font-medium">'
        '➕ Add selected to this cohort</button>'
    )

    return (
        f'<form hx-post="/cohort/{cid}/merge-from" '
        f'hx-target="#cohort-settings-members" hx-swap="innerHTML" '
        'hx-on::after-request="document.getElementById(\'merge-status\').textContent = \'\'">'
        + "".join(parts)
        + delete_toggle
        + submit_btn
        + '</form>'
        '<div id="merge-status" class="mt-1 text-[11px] text-emerald-400"></div>'
    )


@router.post("/cohort/{cid}/merge-from", response_class=HTMLResponse)
async def cohort_merge_from(cid: int, r: Request):
    """Merge selected source cohorts and/or extra accounts INTO this cohort."""
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<p class='text-red-400 text-sm'>Not found</p>"

    f = await r.form()
    source_ids_raw = f.getlist("source_cohort_ids")
    extra_ids_raw = f.getlist("extra_account_ids")
    delete_sources = bool(f.get("delete_sources"))

    # Validate ownership and coerce to int. Drop anything malformed silently.
    def _ints(seq):
        out = []
        for s in seq:
            try:
                out.append(int(s))
            except (TypeError, ValueError):
                pass
        return out
    source_ids = _ints(source_ids_raw)
    extra_ids = _ints(extra_ids_raw)

    # Filter source cohorts to ones the user actually owns + not self
    if source_ids:
        owned = q(
            "SELECT id FROM cohorts WHERE id = ANY(%s) AND user_id = %s AND id != %s",
            (source_ids, user["id"], cid),
        )
        source_ids = [row["id"] for row in owned]

    added_from_cohorts = 0
    added_individual = 0

    # 1. Copy members from source cohorts
    if source_ids:
        rows = q(
            """
            INSERT INTO cohort_members (cohort_id, account_id)
            SELECT %s, account_id
            FROM cohort_members
            WHERE cohort_id = ANY(%s)
            ON CONFLICT DO NOTHING
            RETURNING account_id
            """,
            (cid, source_ids),
        )
        added_from_cohorts = len(rows)

    # 2. Add individual accounts
    if extra_ids:
        rows = q(
            """
            INSERT INTO cohort_members (cohort_id, account_id)
            SELECT %s, id FROM accounts WHERE id = ANY(%s)
            ON CONFLICT DO NOTHING
            RETURNING account_id
            """,
            (cid, extra_ids),
        )
        added_individual = len(rows)

    # 3. Optional: delete source cohorts after rebinding their profiles
    if delete_sources and source_ids:
        # Rebind profiles pointing to a deleted source so they don't break
        q(
            "UPDATE profiles SET cohort_id = %s WHERE cohort_id = ANY(%s)",
            (cid, source_ids),
        )
        # Now safe to delete (cohort_members CASCADEs, cohort_interactions CASCADEs)
        q(
            "DELETE FROM cohorts WHERE id = ANY(%s) AND user_id = %s",
            (source_ids, user["id"]),
        )

    # Invalidate this cohort's insight cache — composition changed
    try:
        import psycopg2
        from lib.storage import invalidate_insights_cache
        conn = psycopg2.connect(**DB)
        with conn.cursor() as cur:
            invalidate_insights_cache(cur, scope_type="cohort", scope_id=cid)
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Re-render the members list so the user immediately sees the new state
    rebuilt = _member_rows(cid)
    msg = ""
    summary = []
    if added_from_cohorts:
        summary.append(f"+{added_from_cohorts} from {len(source_ids)} cohort(s)")
    if added_individual:
        summary.append(f"+{added_individual} individual")
    if delete_sources and source_ids:
        summary.append(f"deleted {len(source_ids)} source cohort(s)")
    if summary:
        msg = (
            '<div class="hx-flash-ok text-xs text-emerald-400 mb-2">'
            f'✓ {" · ".join(summary)}'
            '</div>'
        )
    else:
        msg = (
            '<div class="text-xs text-gray-500 mb-2">'
            'Nothing new to merge — selected items were already in this cohort.'
            '</div>'
        )
    return msg + rebuilt


@router.post("/cohort/{cid}/ecosystem-handles", response_class=HTMLResponse)
async def cohort_set_ecosystem_handles(cid: int, r: Request):
    """Save the curated ecosystem-handle allowlist for this cohort."""
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<span class='text-red-400'>Not found</span>"
    f = await r.form()
    raw = (f.get("handles") or "").strip()
    import re as _re
    import json as _json
    handles = [
        h.lower().lstrip("@")
        for h in _re.split(r"[,\s]+", raw)
        if h.strip()
    ]
    # Dedupe while preserving order
    seen, deduped = set(), []
    for h in handles:
        if h and h not in seen:
            seen.add(h)
            deduped.append(h)
    q(
        "UPDATE cohorts SET ecosystem_handles=%s::jsonb WHERE id=%s AND user_id=%s",
        (_json.dumps(deduped), cid, user["id"]),
    )
    # Drop insights cache so the next view uses the new allowlist
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "collector"))
        from lib.storage import invalidate_insights_cache
        import psycopg2
        conn = psycopg2.connect(**DB)
        with conn.cursor() as cur:
            invalidate_insights_cache(cur, scope_type="cohort", scope_id=cid)
        conn.commit()
        conn.close()
    except Exception:
        pass  # cache miss is fine; the warning chip drives the next regen
    return f"<span>Saved {len(deduped)} handle{'s' if len(deduped) != 1 else ''}</span>"


@router.post("/cohort/{cid}/delete", response_class=HTMLResponse)
def cohort_delete(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<p class='text-red-400'>Not found</p>"
    q("DELETE FROM cohort_members WHERE cohort_id=%s", (cid,))
    q("DELETE FROM profiles WHERE cohort_id=%s", (cid,))
    q("DELETE FROM cohorts WHERE id=%s", (cid,))
    return (
        '<div class="text-center py-8 bg-gray-900 rounded-xl border border-red-900/30">'
        '<div class="text-4xl mb-3">🗑️</div>'
        '<h2 class="text-xl font-semibold mb-1">Cohort Deleted</h2>'
        '<p class="text-gray-500 text-sm mb-4">Cohort and its profiles have been removed.</p>'
        '<a href="/profiles" class="inline-block bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Go to Profiles →</a>'
        '</div>'
    )


@router.post("/cohort/{cid}/share-token", response_class=HTMLResponse)
def cohort_share_token(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    import secrets
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<p class='text-red-400 text-sm'>Not found</p>"
    token = secrets.token_urlsafe(16)
    q("UPDATE cohorts SET share_token=%s WHERE id=%s", (token, cid))
    return (
        '<div class="flex items-center gap-2">'
        f'<input type="text" value="/share/{html.escape(token)}" class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-300 flex-1" readonly>'
        f'<button class="text-xs bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-3 py-1.5" '
        f'onclick="navigator.clipboard.writeText(location.origin+\'/share/{html.escape(token)}\')">Copy</button></div>'
    )


@router.post("/cohort/{cid}/revoke-token", response_class=HTMLResponse)
def cohort_revoke_token(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    q("UPDATE cohorts SET share_token=NULL WHERE id=%s AND user_id=%s", (cid, user["id"]))
    return '<p class="text-xs text-gray-500">Share link revoked.</p>'


@router.get("/cohort/{cid}/note-edit/{username}", response_class=HTMLResponse)
def cohort_note_edit(cid: int, username: str, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return "<span class='text-red-400'>!</span>"
    n = q(
        "SELECT note FROM cohort_members WHERE cohort_id=%s "
        "AND account_id=(SELECT id FROM accounts WHERE username=%s)",
        (cid, username),
    )
    cur_val = n[0]["note"] if n and n[0]["note"] else ""
    return (
        f'<input type="text" name="note" value="{html.escape(cur_val)}" placeholder="note..." '
        'class="bg-gray-800 border border-gray-700 rounded px-2 py-0.5 text-xs text-gray-300 w-32" '
        f'hx-post="/cohort/{cid}/note-save/{html.escape(username)}" '
        "hx-trigger=\"blur,keydown[key=='Enter']\" "
        f'hx-target="#note-{html.escape(username)}" hx-swap="outerHTML transition:true" autofocus '
        "_=\"on keydown[key=='Escape'] trigger blur\">"
    )


@router.post("/cohort/{cid}/note-save/{username}", response_class=HTMLResponse)
async def cohort_note_save(cid: int, username: str, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    f = await r.form()
    note = ((f.get("note") or "") or "")[:200]
    user = get_user(r)
    if q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        q(
            "UPDATE cohort_members SET note=%s WHERE cohort_id=%s "
            "AND account_id=(SELECT id FROM accounts WHERE username=%s)",
            (note, cid, username),
        )
    return (
        f'<span class="text-gray-400 italic cursor-pointer" id="note-{html.escape(username)}" '
        f'hx-get="/cohort/{cid}/note-edit/{html.escape(username)}" '
        f'hx-target="#note-{html.escape(username)}" hx-swap="outerHTML transition:true">{html.escape(note)}</span>'
    )


@router.get("/cohort/{cid}/name-edit", response_class=HTMLResponse)
def cohort_name_edit(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    c = q("SELECT name FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"]))
    if not c:
        return "<span class='text-red-400'>Error</span>"
    return (
        f'<input type="text" name="name" id="cohort-name-input" value="{html.escape(c[0]["name"])}" '
        'class="bg-gray-800 border border-emerald-500 rounded-lg px-3 py-1 text-sm font-semibold" '
        f'hx-patch="/cohort/{cid}/name" '
        "hx-trigger=\"blur, keydown[key=='Enter']\" "
        'hx-target="#cohort-name-display" hx-swap="innerHTML" hx-include="this" autofocus '
        "_=\"on keydown[key=='Escape'] set my.value to @value then trigger blur\">"
    )


@router.patch("/cohort/{cid}/name", response_class=HTMLResponse)
async def cohort_name_update(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    f = await r.form()
    name = (f.get("name") or "").strip()
    if not name:
        return "<span class='text-red-400'>Cannot be empty</span>"
    user = get_user(r)
    q("UPDATE cohorts SET name=%s WHERE id=%s AND user_id=%s", (name, cid, user["id"]))
    return (
        f'<span hx-get="/cohort/{cid}/name-edit" hx-trigger="click" hx-swap="outerHTML transition:true" '
        f'class="cursor-pointer hover:text-emerald-400 transition">{html.escape(name)} ✏️</span>'
    )


@router.post("/cohort/{cid}/remove/{username}", response_class=HTMLResponse)
def cohort_remove_member(cid: int, username: str, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return _member_rows(cid)
    q(
        "DELETE FROM cohort_members WHERE cohort_id=%s "
        "AND account_id=(SELECT id FROM accounts WHERE username=%s)",
        (cid, username),
    )
    return _member_rows(cid)


@router.post("/cohort/{cid}/add", response_class=HTMLResponse)
async def cohort_add_member(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if not q("SELECT 1 FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"])):
        return _member_rows(cid)
    f = await r.form()
    handle = (f.get("handle") or "").strip().lstrip("@")
    if not handle:
        return _member_rows(cid)
    q("INSERT INTO accounts(username) VALUES(%s) ON CONFLICT(username) DO NOTHING", (handle,))
    aid = q("SELECT id FROM accounts WHERE username=%s", (handle,))[0]["id"]
    q(
        "INSERT INTO cohort_members(cohort_id, account_id) VALUES(%s, %s) ON CONFLICT DO NOTHING",
        (cid, aid),
    )
    return _member_rows(cid)
