"""web/routes/misc.py — /tweet/{id}, /share/{token}, PATCH /profile/{pid}/name"""
import html

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.core import q, get_user, require_login  # noqa: E402
from web.ui import header_html, fmt, rel_time, type_pill, HF  # noqa: E402

router = APIRouter()


@router.get("/tweet/{tweet_id}", response_class=HTMLResponse)
def tweet_detail(tweet_id: str, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    t = q(
        """
        SELECT t.*, a.username, a.display_name, a.avatar_url
        FROM tweets t JOIN accounts a ON a.id = t.author_account_id
        WHERE t.tweet_id = %s
        """,
        (tweet_id,),
    )
    if not t:
        return header_html(0) + "<p class='text-gray-500 text-sm'>Tweet not found.</p>" + HF
    t = t[0]
    # reply threading: top-level replies, then their direct children.
    top_replies = q(
        """
        SELECT r.*, a.username, a.display_name, a.avatar_url
        FROM replies r JOIN accounts a ON a.id = r.author_account_id
        WHERE r.tweet_id = %s AND r.parent_reply_id IS NULL
        ORDER BY r.likes DESC NULLS LAST, r.created_at ASC
        LIMIT 200
        """,
        (tweet_id,),
    )
    # If no parent_reply_id is ever populated (legacy data), top_replies will
    # be empty but flat replies exist — fall back to "everything is top-level".
    if not top_replies:
        top_replies = q(
            """
            SELECT r.*, a.username, a.display_name, a.avatar_url
            FROM replies r JOIN accounts a ON a.id = r.author_account_id
            WHERE r.tweet_id = %s
            ORDER BY r.likes DESC NULLS LAST, r.created_at ASC
            LIMIT 200
            """,
            (tweet_id,),
        )
        child_replies = []
    else:
        parent_ids = [rp["reply_id"] for rp in top_replies]
        child_replies = q(
            """
            SELECT r.*, a.username, a.display_name, a.avatar_url
            FROM replies r JOIN accounts a ON a.id = r.author_account_id
            WHERE r.tweet_id = %s AND r.parent_reply_id = ANY(%s)
            ORDER BY r.likes DESC NULLS LAST, r.created_at ASC
            """,
            (tweet_id, parent_ids),
        )

    children_by_parent = {}
    for c in child_replies:
        children_by_parent.setdefault(c["parent_reply_id"], []).append(c)

    total_replies = len(top_replies) + len(child_replies)

    def _reply_row(rep, indent=0):
        sent = rep.get("sentiment")
        sent_html = ""
        if sent is not None:
            tone = ("text-emerald-400" if sent > 0.3 else
                    ("text-red-400" if sent < -0.3 else "text-gray-400"))
            sent_html = f' <span class="text-[10px] {tone}">{sent:+.2f}</span>'
        author_pill = (
            '<span class="text-[10px] bg-emerald-900/40 text-emerald-300 px-1.5 py-0.5 rounded ml-1">author</span>'
            if rep.get("is_author_reply") else ""
        )
        wrapper_cls = "ml-6 mt-1" if indent else ""
        kids = children_by_parent.get(rep.get("reply_id"), [])
        kids_html = (
            f'<div class="ml-6 mt-2 space-y-1 border-l border-gray-800 pl-2">'
            + "".join(_reply_row(c, indent=1) for c in kids)
            + "</div>"
        ) if kids else ""
        return (
            f'<div class="bg-gray-900 rounded-lg p-3 border border-gray-800 {wrapper_cls}">'
            '<div class="flex items-start gap-3">'
            f'<img src="{html.escape(rep.get("avatar_url") or "")}" '
            'class="w-8 h-8 rounded-full bg-gray-800 object-cover" '
            'onerror="this.style.display=\'none\'" loading="lazy">'
            '<div class="flex-1 min-w-0">'
            '<div class="text-xs flex items-center gap-1 flex-wrap">'
            f'<a href="/account/{html.escape(rep["username"])}" class="text-emerald-400 hover:underline font-medium">@{html.escape(rep["username"])}</a>'
            f'{author_pill}'
            f'<span class="text-gray-400">·</span>'
            f'<span class="text-gray-500">{rel_time(rep.get("created_at"))}</span>'
            f'{sent_html}'
            '</div>'
            f'<div class="text-sm text-gray-300 mt-1 break-words">{html.escape((rep.get("content") or "")[:500])}</div>'
            '<div class="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-gray-500">'
            f'<span class="text-pink-400 whitespace-nowrap">❤ {fmt(rep.get("likes") or 0)}</span>'
            f'<a href="https://x.com/{html.escape(rep["username"])}/status/{html.escape(rep["reply_id"])}" target="_blank" class="text-gray-400 hover:text-gray-400 whitespace-nowrap">view on X ↗</a>'
            '</div></div></div>'
            f'{kids_html}'
            '</div>'
        )

    replies_html = "".join(_reply_row(rp) for rp in top_replies) or (
        '<div class="text-center py-12 bg-gray-900 rounded-xl border border-gray-800">'
        '<div class="text-3xl mb-2">💬</div>'
        '<p class="text-gray-500 text-sm">No replies mined yet for this tweet.</p>'
        '<p class="text-gray-400 text-xs mt-2">Trigger a scrape on this profile — '
        'reply-mining picks up tweets with replies&gt;0 not yet in the index.</p></div>'
    )
    kind = ("reply" if t["is_reply"] else
            ("quote" if t["is_quote"] else
             ("retweet" if t["is_retweet"] else "original")))
    return header_html(0) + (
        '<div class="max-w-3xl mx-auto">'
        f'<a href="javascript:history.back()" class="text-xs text-gray-500 hover:text-gray-300">&larr; back</a>'
        '<div class="bg-gray-900 rounded-xl p-5 border border-gray-800 mt-3 mb-6">'
        '<div class="flex items-start gap-3 mb-3">'
        f'<img src="{html.escape(t.get("avatar_url") or "")}" class="w-10 h-10 rounded-full bg-gray-800 object-cover" '
        'onerror="this.style.display=\'none\'" loading="lazy">'
        '<div class="flex-1">'
        f'<a href="/account/{html.escape(t["username"])}" class="text-emerald-400 font-semibold hover:underline">@{html.escape(t["username"])}</a>'
        f' <span class="text-gray-500 text-sm">{html.escape(t.get("display_name") or "")}</span>'
        f'<div class="text-xs text-gray-500">{rel_time(t.get("created_at"))} · {type_pill(kind)}</div>'
        '</div></div>'
        f'<div class="text-base text-gray-200 break-words">{html.escape((t.get("content") or "")[:1000])}</div>'
        '<div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-400">'
        f'<span class="text-pink-400 whitespace-nowrap">❤ Likes {fmt(t.get("likes") or 0)}</span>'
        f'<span class="text-blue-400 whitespace-nowrap">👁 Views {fmt(t.get("views") or 0)}</span>'
        f'<span class="text-green-400 whitespace-nowrap">↻ Retweets {fmt(t.get("retweets") or 0)}</span>'
        f'<span class="text-cyan-400 whitespace-nowrap">💬 Replies {fmt(t.get("replies") or 0)}</span>'
        f'<a href="https://x.com/{html.escape(t["username"])}/status/{html.escape(t["tweet_id"])}" target="_blank" class="text-gray-500 hover:text-gray-300 ml-auto text-xs whitespace-nowrap">open on X ↗</a>'
        '</div></div>'
        f'<h2 class="text-sm text-gray-500 uppercase mb-3 flex items-center gap-2">'
        f'<span>Mined replies</span><span class="text-gray-400">{fmt(total_replies)} stored · {fmt(t.get("replies") or 0)} on X</span></h2>'
        f'<div class="space-y-2">{replies_html}</div>'
        '</div>'
    ) + HF


@router.get("/share/{token}", response_class=HTMLResponse)
def share_page(token: str, r: Request, days: int = 30):
    c = q("SELECT * FROM cohorts WHERE share_token=%s", (token,))
    if not c:
        return (
            '<!DOCTYPE html><html lang="en" class="dark"><head><title>VibeChecx — Not Found</title></head>'
            '<body class="bg-gray-950 text-gray-100 min-h-screen flex items-center justify-center">'
            '<div class="text-center"><div class="text-4xl mb-3">🔗</div>'
            '<h1 class="text-xl font-semibold mb-2">Link Not Found</h1>'
            '<p class="text-gray-500 text-sm">This share link doesn\'t exist or has been revoked.</p>'
            '</div></body></html>'
        )
    c = c[0]
    d = q(
        """
        SELECT a.username, a.display_name, a.followers_count,
               count(t.tweet_id) AS tweets,
               COALESCE(sum(t.likes) FILTER (WHERE NOT t.is_retweet), 0)::int AS likes,
               COALESCE(sum(t.views) FILTER (WHERE NOT t.is_retweet), 0)::int AS views
        FROM cohort_members cm JOIN accounts a ON a.id=cm.account_id
        LEFT JOIN tweets t ON t.author_account_id=a.id
        WHERE cm.cohort_id=%s
        GROUP BY a.id, a.username ORDER BY likes DESC LIMIT 50
        """,
        (c["id"],),
    )
    pfp = c.get("pfp_url") or ""
    cards = "".join(
        (
            '<div class="bg-gray-900 rounded-xl p-4 border border-gray-800">'
            f'<div class="text-emerald-400 font-semibold">@{html.escape(x["username"])}</div>'
            f'<div class="text-sm text-gray-500">{html.escape(x.get("display_name") or "")}</div>'
            f'<div class="mt-2 flex gap-3 text-xs bg-gray-800/50 rounded-lg p-2">'
            f'<span class="text-gray-400 whitespace-nowrap">{fmt(x["tweets"])} tweets</span>'
            f'<span class="text-pink-400 whitespace-nowrap">❤ Likes {fmt(x["likes"])}</span>'
            f'<span class="text-blue-400 whitespace-nowrap">👁 Views {fmt(x["views"])}</span>'
            f'<span class="text-gray-400 whitespace-nowrap">{fmt(x["followers_count"])} followers</span></div></div>'
        )
        for x in d
    )
    og = (
        f'<meta property="og:image" content="{html.escape(pfp)}">'
        f'<meta property="og:title" content="{html.escape(c["name"])} — VibeChecx">'
        f'<meta property="og:description" content="{len(d)} members tracked in {html.escape(c["name"])}">'
        '<meta name="twitter:card" content="summary">'
    )
    return (
        '<!DOCTYPE html><html lang="en" class="dark"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        f'<title>{html.escape(c["name"])} — VibeChecx</title>{og}'
        '<script src="https://cdn.tailwindcss.com"></script>'
        '<style>body{background:#030712;color:#f3f4f6}</style></head>'
        '<body class="min-h-screen"><div class="max-w-3xl mx-auto px-6 py-10">'
        '<div class="flex items-start gap-4 mb-8">'
        f'<img src="{html.escape(pfp)}" class="w-16 h-16 rounded-full bg-gray-800 object-cover" '
        'onerror="this.style.display=\'none\'" loading="lazy">'
        f'<div><h1 class="text-2xl font-bold">{html.escape(c["name"])}</h1>'
        f'<p class="text-gray-500 text-sm">{len(d)} tracked members</p>'
        '<p class="text-xs text-gray-400 mt-2">Shared from VibeChecx</p></div></div>'
        f'<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">{cards}</div></div></body></html>'
    )


@router.patch("/profile/{pid}/name", response_class=HTMLResponse)
async def profile_rename(pid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    f = await r.form()
    name = (f.get("name") or "").strip()
    if not name:
        return "<span class='text-red-400'>Cannot be empty</span>"
    q("UPDATE profiles SET name=%s WHERE id=%s AND user_id=%s", (name, pid, user["id"]))
    return f'<span class="text-emerald-400 font-semibold">{html.escape(name)}</span>'
