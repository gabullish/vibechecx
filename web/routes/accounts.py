"""web/routes/accounts.py — /account/{handle} and related routes"""
import os
import sys
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.core import q, get_user, require_login, active_profile_name
from web.ui import header_html, tip, fmt, fmt_compact, rel_time, type_pill, HF, _vibe
from web.render_insights import _render_insights

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web.queue_worker import enqueue  # noqa: E402
import vibechecx_insights as vi  # noqa: E402


def _depth_chip(value, label, checked=False):
    ck = "checked" if checked else ""
    return (
        f'<label class="cursor-pointer">'
        f'<input type="radio" name="days" value="{value}" class="sr-only peer" {ck}>'
        f'<span class="px-1.5 py-0.5 text-[10px] rounded border border-gray-700 text-gray-400 '
        f'peer-checked:border-cyan-500 peer-checked:text-cyan-300 peer-checked:bg-cyan-950/40 '
        f'hover:border-gray-500 transition select-none inline-block">{label}</span>'
        f'</label>'
    )

router = APIRouter()


@router.get("/account/{handle}", response_class=HTMLResponse)
def account_page(handle: str, r: Request, days: int = 365):
    handle = handle.lower().lstrip("@")
    # Case-insensitive lookup — prefer account with most tweets (handles duplicate casing)
    ac = q(
        "SELECT a.* FROM accounts a "
        "LEFT JOIN tweets t ON t.author_account_id = a.id "
        "WHERE LOWER(a.username) = %s "
        "GROUP BY a.id ORDER BY COUNT(t.tweet_id) DESC LIMIT 1",
        (handle,),
    )
    if not ac:
        # Check if a scrape is already running for this handle (auto-started by wizard).
        active = q(
            """SELECT id FROM scrape_sessions
               WHERE target_handle=%s AND status='running'
               ORDER BY created_at DESC LIMIT 1""",
            (handle,),
        )
        if active:
            scraping_card = (
                '<div class="flex items-center gap-3 bg-cyan-900/30 border border-cyan-700 rounded-xl px-5 py-4 mb-4">'
                '<span class="inline-block w-3 h-3 rounded-full bg-cyan-400 animate-pulse flex-shrink-0"></span>'
                f'<div><p class="text-cyan-300 font-medium text-sm">Scraping @{html.escape(handle)}…</p>'
                '<p class="text-cyan-500 text-xs mt-0.5">Pulling tweets and profile data. This takes 2–5 minutes — progress shows in the top-right corner.</p></div>'
                '</div>'
            )
        else:
            scraping_card = (
                f'<form hx-post="/account/refresh/{html.escape(handle)}" '
                f'hx-target="#no-data-action" hx-swap="innerHTML transition:true" '
                f'class="flex items-center justify-center flex-wrap gap-2">'
                + '<span class="text-xs text-gray-500 w-full text-center mb-1">How far back to scrape:</span>'
                + _depth_chip("1", "24h")
                + _depth_chip("7", "7d", True)
                + _depth_chip("14", "14d")
                + _depth_chip("30", "30d")
                + '<button type="submit" class="px-4 py-2 text-sm rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white transition">🔍 Scrape this account</button>'
                + '</form>'
                + '<div id="no-data-action" class="mt-3"></div>'
            )
        return header_html(0, active_profile_name(r), show_insights=False) + (
            '<div class="max-w-xl mx-auto text-center py-12">'
            '<div class="text-4xl mb-3">🔍</div>'
            f'<h1 class="text-xl font-semibold mb-2">@{html.escape(handle)}</h1>'
            '<p class="text-gray-400 text-sm mb-6">No data yet — a scrape pulls tweets, engagement stats, and profile info.</p>'
            f'{scraping_card}'
            '</div>'
        ) + HF
    ac = ac[0]
    aid = ac["id"]
    # Aggregate breakdown: posts vs replies for the period
    agg = q(
        f"""
        SELECT
            count(*)::int AS total_tweets,
            count(*) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote)::int AS posts,
            count(*) FILTER (WHERE is_reply)::int AS replies,
            count(*) FILTER (WHERE is_retweet)::int AS retweets,
            count(*) FILTER (WHERE is_quote)::int AS quotes,
            COALESCE(sum(likes) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote), 0)::int AS posts_likes,
            COALESCE(sum(likes) FILTER (WHERE is_reply), 0)::int AS replies_likes,
            COALESCE(sum(views) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote), 0)::int AS posts_views,
            COALESCE(sum(views) FILTER (WHERE is_reply), 0)::int AS replies_views,
            COALESCE(sum(replies) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote), 0)::int AS posts_replies,
            COALESCE(sum(replies) FILTER (WHERE is_reply), 0)::int AS replies_on_replies,
            COALESCE(sum(retweets) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote), 0)::int AS posts_retweets,
            COALESCE(sum(quotes) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote), 0)::int AS posts_quotes,
            COALESCE(sum(bookmarks) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote), 0)::int AS posts_bookmarks,
            COALESCE(AVG(quality_score) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote AND quality_score IS NOT NULL), 0)::real AS posts_avg_quality,
            COALESCE(AVG(inorganic_score) FILTER (WHERE NOT is_reply AND NOT is_retweet AND NOT is_quote AND inorganic_score IS NOT NULL), 0)::real AS posts_avg_inorganic
        FROM tweets WHERE author_account_id=%s
          AND created_at > NOW() - INTERVAL '{int(days)} days'
        """,
        (aid,),
    )[0]
    tc = agg["total_tweets"]
    total_likes = agg["posts_likes"] + agg["replies_likes"]
    total_views = agg["posts_views"] + agg["replies_views"]

    tw = q(
        f"""
        SELECT tweet_id, content, created_at, likes, retweets, replies, views,
               is_retweet, is_reply, is_quote, sentiment, category
        FROM tweets WHERE author_account_id=%s
          AND created_at > NOW() - INTERVAL '{int(days)} days'
        ORDER BY created_at DESC LIMIT 50
        """,
        (aid,),
    )
    pfp = ac.get("avatar_url") or ""
    bio = (ac.get("bio") or "")[:200]
    cohort_pills = ""
    user = get_user(r)
    if user:
        cohorts_for = q(
            """
            SELECT c.id, c.name FROM cohorts c
            JOIN cohort_members cm ON cm.cohort_id = c.id
            WHERE c.user_id = %s AND cm.account_id = %s
            """,
            (user["id"], ac["id"]),
        )
        cohort_pills = "".join(
            f'<a href="/cohort/{c["id"]}" class="text-[10px] px-2 py-0.5 rounded-full bg-gray-800 text-gray-400 hover:text-emerald-400 hover:bg-gray-700 transition">in {html.escape(c["name"])}</a>'
            for c in cohorts_for
        )
    refresh_btn = (
        f'<form hx-post="/account/refresh/{html.escape(handle)}" '
        f'hx-target="#account-scrape-progress" hx-swap="innerHTML transition:true" '
        f'class="flex items-center flex-wrap gap-1.5">'
        + '<span class="text-[10px] text-gray-500 mr-0.5">how far back:</span>'
        + _depth_chip("1", "24h")
        + _depth_chip("7", "7d", True)
        + _depth_chip("14", "14d")
        + _depth_chip("30", "30d")
        + '<button type="submit" class="px-2 py-0.5 text-[10px] rounded bg-cyan-900/40 border border-cyan-800/60 text-cyan-300 hover:bg-cyan-800/50 transition">↻ Scrape</button>'
        + '</form>'
    )

    def _tweet_card(t):
        badge = ""
        if t.get("is_quote"):
            badge = '<span class="text-cyan-400">💬 Quote</span> '
        elif t.get("is_reply"):
            badge = '<span class="text-yellow-400">↩ Reply</span> '
        return (
            '<div class="bg-gray-900 rounded-lg p-4 border border-gray-800">'
            f'<div class="text-xs text-gray-500 mb-1">{rel_time(t.get("created_at"))}</div>'
            f'<div class="text-sm mb-2">{badge}{html.escape((t["content"] or "")[:200])}</div>'
            '<div class="flex gap-3 text-xs text-gray-500 flex-wrap">'
            f'<span class="text-pink-400 whitespace-nowrap">❤ {fmt(t["likes"] or 0)}</span>'
            f'<span class="text-blue-400 whitespace-nowrap">👁 {fmt(t["views"] or 0)}</span>'
            f'<span class="text-green-400 whitespace-nowrap">↻ {fmt(t["retweets"] or 0)}</span>'
            f'{"<span class=\"text-yellow-400 whitespace-nowrap\">⚡ "+str(t["sentiment"])[:4]+"</span>" if t.get("sentiment") is not None else ""}'
            '</div></div>'
        )

    tweets = "".join(_tweet_card(t) for t in tw[:20])

    # Stats bar (rich breakdown with period label)
    period_label = {1: "24h", 7: "7d", 14: "14d", 30: "30d", 365: "1y"}.get(days, f"{days}d")
    eng_total = agg["posts_likes"] + agg["posts_replies"] + agg["posts_retweets"] + agg["replies_likes"]
    eng_posts = agg["posts_likes"] + agg["posts_replies"] + agg["posts_retweets"]
    stats_bar = (
        '<div class="bg-gray-900 rounded-xl border border-gray-800 p-4 mb-6">'
        '<div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-center">'
        f'<div><div class="text-lg font-semibold text-white">{fmt(ac.get("followers_count", 0))}</div>'
        f'<div class="text-[10px] text-gray-400">Followers</div>'
        f'<div class="text-[10px] text-gray-400">{fmt(ac.get("following_count", 0))} following</div></div>'
        f'<div><div class="text-lg font-semibold text-white">{fmt(agg["total_tweets"])}</div>'
        f'<div class="text-[10px] text-gray-400">Tweets <span class="text-gray-500">({period_label})</span></div></div>'
        f'<div><div class="text-lg font-semibold text-pink-400">{fmt(total_likes)}</div>'
        f'<div class="text-[10px] text-gray-400">Likes</div></div>'
        f'<div><div class="text-lg font-semibold text-blue-400">{fmt(total_views)}</div>'
        f'<div class="text-[10px] text-gray-400">Views</div></div>'
        '</div>'
        '<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mt-3 pt-3 border-t border-gray-800">'
        f'<div class="bg-gray-800/30 rounded-lg p-3"><div class="text-xs text-gray-400 font-medium mb-2">' + tip(
            'Posts',
            'Standalone tweets authored by this account in the period — '
            'not replies, not quotes, not retweets. The audience-growth lever.'
        ) + f'<span class="text-gray-500 font-normal ml-1">· {fmt(agg["posts"])} tweets</span></div>'
        '<div class="space-y-1">'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">❤ Likes</span><span class="text-pink-400 font-medium">{fmt(agg["posts_likes"])}</span></div>'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">👁 Views</span><span class="text-blue-400 font-medium">{fmt(agg["posts_views"])}</span></div>'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">💬 Inbound replies</span><span class="text-gray-300 font-medium">{fmt(agg["posts_replies"])}</span></div>'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">↻ Retweets</span><span class="text-gray-300 font-medium">{fmt(agg["posts_retweets"])}</span></div>'
        '</div></div>'
        f'<div class="bg-gray-800/30 rounded-lg p-3"><div class="text-xs text-gray-400 font-medium mb-2">' + tip(
            'Outbound Replies',
            '<strong>Outbound</strong> replies — tweets this account wrote in '
            'response to someone else. High reply ratio = conversational '
            'positioning (community member / mascot). Low reply ratio = '
            'broadcaster positioning (brand / media).'
        ) + f'<span class="text-gray-500 font-normal ml-1">· {fmt(agg["replies"])} tweets</span></div>'
        '<div class="space-y-1">'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">❤ Likes</span><span class="text-pink-400 font-medium">{fmt(agg["replies_likes"])}</span></div>'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">👁 Views</span><span class="text-blue-400 font-medium">{fmt(agg["replies_views"])}</span></div>'
        '</div></div>'
        f'<div class="bg-gray-800/30 rounded-lg p-3"><div class="text-xs text-gray-400 font-medium mb-2">' + tip(
            'Quotes',
            'Quote tweets — the account wrapped their own commentary around '
            'someone else\'s tweet. A way to amplify others while adding '
            'voice. The X algo rewards substantive added commentary.'
        ) + '</div>'
        '<div class="space-y-1">'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">🔁 Quoted others</span><span class="text-gray-300 font-medium">{fmt(agg["quotes"])}</span></div>'
        '</div></div>'
        f'<div class="bg-gray-800/30 rounded-lg p-3"><div class="text-xs text-gray-400 font-medium mb-2">' + tip(
            'Retweets',
            'Bare retweets — no added commentary. Filtered OUT of every other '
            'analysis (they aren\'t this account\'s authored content), shown '
            'here just for completeness.'
        ) + '</div>'
        '<div class="space-y-1">'
        f'<div class="flex justify-between text-[11px]"><span class="text-gray-500">🔄 Shared bare</span><span class="text-gray-300 font-medium">{fmt(agg["retweets"])}</span></div>'
        '</div></div>'
        '</div>'
        '</div>'
    )

    # Performance section
    posts = max(agg["posts"], 1)
    perf_epv = round(eng_posts / max(agg["posts_views"], 1) * 1000, 2)
    perf_epf = round(eng_total / max(ac.get("followers_count", 0), 1) * 1000, 2)
    perf_quality = round(agg["posts_avg_quality"], 1)
    perf_inorganic = round(agg["posts_avg_inorganic"], 3)
    perf_html = (
        f'<div class="max-w-4xl mx-auto mb-6">'
        '<div class="bg-gray-900 rounded-xl border border-gray-800 p-4">'
        '<h3 class="text-xs font-semibold text-gray-400 uppercase mb-3">📊 Efficiency</h3>'
        '<div class="grid grid-cols-3 gap-4 text-center">'
        f'<div><div class="text-lg font-semibold text-emerald-400">{fmt(perf_epv)}</div>'
        f'<div class="text-[10px] text-gray-400">'
        + tip("EPV ×1000", "Engagement Per View — likes on posts ÷ post views, scaled ×1000. Higher = more engaged audience per impression.", with_icon=False)
        + '</div></div>'
        f'<div><div class="text-lg font-semibold text-blue-400">{fmt(perf_epf)}</div>'
        f'<div class="text-[10px] text-gray-400">'
        + tip("EPF ×1000", "Engagement Per Follower — total engagement ÷ followers, scaled ×1000. Higher = follower base is actively engaging.", with_icon=False)
        + '</div></div>'
        f'<div><div class="text-lg font-semibold text-purple-400">{fmt(perf_quality)}</div>'
        f'<div class="text-[10px] text-gray-400">'
        + tip("Quality (0-100)", "AI-scored post quality (0–100). Measures originality, depth, and clarity. Averaged across all posts in the period.", with_icon=False)
        + '</div></div>'
        '</div>'
        '<div class="flex gap-4 mt-3 pt-3 border-t border-gray-800 text-xs text-gray-400">'
        f'<span>🏆 {fmt(eng_total)} engagement (posts+replies)</span>'
        f'<span>'
        + tip(f"🔬 {fmt(perf_inorganic)} inorganic", "Inorganic score — proxy for bot-like patterns (repetitive phrasing, posting bursts). Lower is better. Near 0 = natural posting.", with_icon=False)
        + '</span>'
        '</div></div></div>'
    )
    # Media performance — does this account's videos beat their photos?
    # GROUP BY media type, count distinct tweets containing it, avg likes/views.
    # Filtered to non-retweets in the same window as the rest of the page.
    media_perf = q(
        f"""
        SELECT
            m.media_type,
            COUNT(DISTINCT t.tweet_id)::int AS tweets,
            COALESCE(AVG(t.likes), 0)::int AS avg_likes,
            COALESCE(AVG(t.views), 0)::int AS avg_views
        FROM media m
        JOIN tweets t ON t.tweet_id = m.tweet_id
        WHERE t.author_account_id = %s
          AND NOT t.is_retweet
          AND t.created_at > NOW() - INTERVAL '{int(days)} days'
          AND m.media_type IN ('photo', 'video', 'animated_gif')
        GROUP BY m.media_type
        """,
        (aid,),
    )
    media_html = ""
    if media_perf:
        _icons = {"photo": "📷", "video": "🎥", "animated_gif": "🎞"}
        _labels = {"photo": "Photos", "video": "Videos", "animated_gif": "GIFs"}
        # Rank media types by avg likes — winner gets the green highlight.
        best_type = max(media_perf, key=lambda r: r["avg_likes"])["media_type"]
        # Stable display order: photo → video → gif (even if some are missing)
        order = ("photo", "video", "animated_gif")
        rows_by_type = {r["media_type"]: r for r in media_perf}
        cells = ""
        for mt in order:
            r = rows_by_type.get(mt)
            if not r:
                cells += (
                    f'<div class="text-center text-gray-600">'
                    f'<div class="text-2xl mb-1">{_icons[mt]}</div>'
                    f'<div class="text-[10px]">{_labels[mt]}</div>'
                    f'<div class="text-[10px]">—</div></div>'
                )
                continue
            is_best = (mt == best_type and len(media_perf) > 1)
            label_cls = "text-emerald-400" if is_best else "text-gray-300"
            crown = ' 👑' if is_best else ''
            cells += (
                f'<div class="text-center">'
                f'<div class="text-2xl mb-1">{_icons[mt]}</div>'
                f'<div class="text-[10px] text-gray-400">{_labels[mt]}{crown} · {fmt(r["tweets"])} tweets</div>'
                f'<div class="text-sm font-semibold {label_cls}">❤ {fmt(r["avg_likes"])}'
                f'<span class="text-gray-500 font-normal text-[10px]"> avg</span></div>'
                f'<div class="text-[10px] text-blue-400">👁 {fmt(r["avg_views"])}'
                f'<span class="text-gray-500"> avg</span></div>'
                f'</div>'
            )
        media_html = (
            '<div class="max-w-4xl mx-auto mb-6">'
            '<div class="bg-gray-900 rounded-xl border border-gray-800 p-4">'
            '<h3 class="text-xs font-semibold text-gray-400 uppercase mb-3">'
            + tip(
                "🎞 Media format performance",
                "Average likes and views per tweet, grouped by attached media "
                "type (photos, videos, GIFs). 👑 marks the best-performing "
                "format by avg likes. Helps answer: should I post more videos?",
            ) + '</h3>'
            f'<div class="grid grid-cols-3 gap-4">{cells}</div>'
            '</div></div>'
        )
    # Vibe bar
    vibe_val, vibe_desc = _vibe("account", aid, f"{days}d")
    vibe_color = "bg-emerald-500" if vibe_val >= 65 else "bg-yellow-500" if vibe_val >= 40 else "bg-red-500"
    vibe_html = (
        f'<div class="max-w-4xl mx-auto mb-6">'
        '<div class="bg-gray-900 rounded-xl border border-gray-800 p-4">'
        '<div class="flex items-center justify-between mb-2">'
        f'<h3 class="text-xs font-semibold text-gray-400 uppercase">' + tip(
            '🔮 Vibe',
            'Composite 0–100 score blending: engagement rate (likes/views), '
            'activity volume (posts in the period), and reach (log-scaled views). '
            'Pure SQL — no LLM call. Tier: <strong>65+</strong> hot, '
            '<strong>40–65</strong> warm, <strong>under 40</strong> cool.\n\n'
            'Use it as a quick sanity check; the strategic thesis (in the AI '
            'Insights panel below) is the deeper read.'
        ) + '</h3>'
        f'<span class="text-sm font-bold {"text-emerald-400" if vibe_val >= 65 else "text-yellow-400" if vibe_val >= 40 else "text-red-400"}">{vibe_val}/100</span>'
        '</div>'
        f'<div class="w-full bg-gray-800 rounded-full h-3 mb-2 overflow-hidden">'
        f'<div class="{vibe_color} h-full rounded-full transition-all duration-500" style="width:{vibe_val}%"></div>'
        '</div>'
        f'<p class="text-sm text-gray-300 leading-relaxed">{html.escape(vibe_desc)}</p>'
        '</div></div>'
    )
    # AI Insights scope is independent of the top date scope. Top scope
    # filters the stats display; insights have their own segmented control
    # below. Default the initial render to 30d (the longest cached scope);
    # the user switches scope via the insights-period buttons which htmx-swap
    # just the insights body without re-rendering the page.
    ap = "30d"
    # Render the FULL cached insight panel inline. The cache lookup is one
    # JSONB read (~5ms); rendering takes another ~5ms; total ~10ms. There is
    # no LLM call here — that only fires when the user clicks "↻ Regenerate".
    # Previously this rendered only summary+topics+kudos, hiding 70% of the
    # report and forcing users to regen-to-see-everything (token waste).
    cr, cp, _from_cache, ca = vi.cached_insights("account", aid, ap, generate_if_missing=False)
    if cr:
        cached_ins = (
            f'<div id="account-insights-content">'
            + _render_insights(
                result=cr, scope_type="account", scope_key=aid,
                scope_display=f"@{ac['username']}",
                period=ap, provider=cp, age_min=ca,
                regen_endpoint=f"/account/{html.escape(handle)}/generate-insights?period={ap}",
                period_get_endpoint=f"/account/{html.escape(handle)}/insights",
                target_id="account-insights-content",
            )
            + '</div>'
        )
    else:
        cached_ins = (
            f'<div id="account-insights-content" class="text-center py-4 bg-gray-900 rounded-xl border border-gray-800">'
            f'<button hx-post="/account/{html.escape(handle)}/generate-insights?period={ap}" '
            f'hx-target="#account-insights-content" hx-swap="innerHTML" '
            'class="text-xs px-3 py-1.5 rounded bg-purple-700 hover:bg-purple-600 text-white transition" '
            '_="on click toggle .hidden on #insights-loading then wait for htmx:afterOnLoad then add .hidden to #insights-loading">'
            '✨ Generate Insights</button></div>'
        )
    # Date tabs use htmx-swap of #account-data so clicking 7d/30d/1y doesn't
    # trigger a full page reload (was destroying the DOM + re-running Alpine
    # init on each click — ~500ms perceived for a 57ms server response).
    # hx-push-url keeps the address bar in sync for back-button + share.
    date_tabs = "".join(
        f'<a hx-get="/account/{html.escape(handle)}?days={d}" '
        f'hx-target="#account-data" hx-swap="outerHTML transition:true" '
        f'hx-push-url="true" '
        f'class="px-2.5 py-1 rounded text-xs transition cursor-pointer '
        + ("bg-emerald-700 text-white" if days == d else "bg-gray-800 text-gray-400 hover:text-white")
        + f'">{l}</a>'
        for d, l in [(1, "24h"), (7, "7d"), (14, "14d"), (30, "30d"), (365, "1y")]
    )

    body = (
        '<div id="account-data" class="max-w-4xl mx-auto">'
        '<div class="flex items-start gap-4 mb-4">'
        f'<img src="{html.escape(pfp)}" class="w-16 h-16 rounded-full bg-gray-800 object-cover" '
        'onerror="this.style.display=\'none\'" loading="lazy">'
        '<div class="flex-1">'
        f'<h1 class="text-xl font-semibold">@{html.escape(ac["username"])}</h1>'
        f'<p class="text-gray-500 text-sm">{html.escape(bio)}</p>'
        f'<div class="flex flex-wrap gap-1 mt-2">{cohort_pills}</div>'
        '</div>'
        f'<div id="account-refresh-status">{refresh_btn}</div>'
        '</div>'
        '<div id="account-scrape-progress"></div>'
        f'<div class="flex gap-1 mb-4">{date_tabs}</div>'
        f'{stats_bar}'
        f'{perf_html}'
        f'{media_html}'
        f'{vibe_html}'
        '<div class="mb-6">'
        '<button onclick="var b=document.getElementById(\'ai-insights-body\');var h=b.classList.toggle(\'hidden\');document.getElementById(\'ai-toggle-icon\').textContent=h?\'▶\':\'▼\';" '
        'class="flex items-center gap-2 text-xs font-semibold text-gray-400 uppercase mb-2 hover:text-white transition w-full text-left">'
        '<span id="ai-toggle-icon">▼</span> ✨ AI Insights'
        f'<span id="account-insights-age" class="text-gray-400 font-normal normal-case"></span></button>'
        '<span id="insights-loading" class="hidden text-xs text-purple-400 ml-2 block">'
        '⟳ Generating… ~10-15s <span class="inline-block w-2 h-2 rounded-full bg-purple-400 animate-pulse ml-1"></span></span>'
        f'<div id="ai-insights-body">'
        f'{cached_ins}'
        f'</div></div>'
        f'<div class="space-y-3">{tweets or "<p class=\"text-gray-400 text-sm text-center py-8\">No tweets in this period.</p>"}</div>'
        '</div>'
    )

    # On an htmx swap (tab click), return only the body fragment — no header,
    # no footer. The client replaces #account-data outerHTML with this string.
    if r.headers.get("HX-Request"):
        return body
    return header_html(days, active_profile_name(r), is_admin=user.get("is_admin", False) if user else False, show_insights=False) + body + HF


@router.post("/account/refresh/{handle}", response_class=HTMLResponse)
async def account_refresh(handle: str, r: Request):
    user = get_user(r)
    if not user:
        return RedirectResponse("/login", 302)
    handle = handle.lower().lstrip("@")
    form = await r.form()
    try:
        days = int(form.get("days") or 7)
        if days not in (1, 7, 14, 30):
            days = 7
    except (ValueError, TypeError):
        days = 7
    # Find or auto-create a single-handle profile for this user+handle.
    rows = q(
        "SELECT id FROM profiles WHERE user_id=%s AND target_handle=%s AND cohort_id IS NULL LIMIT 1",
        (user["id"], handle),
    )
    if rows:
        profile_id = rows[0]["id"]
    else:
        rows = q(
            "INSERT INTO profiles (user_id, name, target_handle) VALUES (%s, %s, %s) RETURNING id",
            (user["id"], f"@{handle}", handle),
        )
        profile_id = rows[0]["id"]
    enqueue(user["id"], profile_id, days)
    return HTMLResponse(
        '<div id="account-scrape-progress" '
        'class="text-xs text-cyan-400 mt-2 flex items-center gap-2">'
        '<span class="w-2 h-2 rounded-full bg-cyan-400 animate-pulse inline-block"></span>'
        'Queued — progress shows in the bar above'
        '</div>'
    )
