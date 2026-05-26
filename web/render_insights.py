"""web/render_insights.py — Insights-specific rendering."""
import os
import sys
import re
import json
import html

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vibechecx_insights as vi  # noqa: E402

from web.core import q  # noqa: E402
from web.ui import tip, fmt, rel_time, header_html, HF  # noqa: E402


def _ai_error_card(regen_endpoint: str, target_id: str) -> str:
    """Generic error card shown when all AI backends fail (quota, outage, etc).
    Never mentions provider names."""
    return (
        f'<div id="{target_id}" class="col-span-12 rounded-xl border border-amber-900/40 bg-amber-950/20 p-6 text-center">'
        '<p class="text-amber-300 font-medium mb-2">VibeChecx AI is temporarily unavailable.</p>'
        '<p class="text-gray-400 text-sm mb-4">The service may be at capacity. Try again in a few minutes.</p>'
        f'<button hx-post="{regen_endpoint}" hx-target="#{target_id}" hx-swap="outerHTML" '
        'class="text-sm px-4 py-2 rounded bg-purple-700 hover:bg-purple-600 text-white transition">'
        '↻ Try again</button>'
        '</div>'
    )


def _provider_label(provider: str | None) -> str:
    """Always show 'VibeChecx AI' to the user regardless of which backend ran."""
    return "VibeChecx AI"


def _render_timely_angles(angles: list | None, exists_in_cache: bool, poll_url: str) -> str:
    """Return the Timely Angles card HTML.
    - Not yet in cache → loading spinner with HTMX poll every 4s.
    - In cache but empty/None → empty string (search found nothing, card hidden).
    - In cache with results → rendered card.
    """
    if not exists_in_cache:
        return (
            '<div id="timely-angles-card" '
            f'hx-get="{poll_url}" hx-trigger="every 4s" hx-swap="outerHTML">'
            '<section class="bg-gray-900 rounded-xl p-5 border border-gray-800 animate-pulse">'
            '<h3 class="flex items-center gap-2 text-[11px] font-semibold tracking-widest text-indigo-400 uppercase mb-3">'
            '<span class="text-base leading-none">📡</span>Timely Angles</h3>'
            '<p class="text-xs text-gray-500">Searching current events…</p>'
            '</section></div>'
        )
    if not angles:
        return ""
    items = "".join(
        '<div class="bg-gray-800/40 rounded-lg p-3 space-y-1">'
        f'<div class="text-sm font-medium text-gray-100">{html.escape(a.get("headline") or "")}</div>'
        f'<div class="text-xs text-gray-400 leading-relaxed">{html.escape(a.get("context") or "")}</div>'
        f'<div class="text-xs text-indigo-300 leading-relaxed italic">{html.escape(a.get("why_it_fits") or "")}</div>'
        '</div>'
        for a in angles if a.get("headline")
    )
    if not items:
        return ""
    body = f'<div class="space-y-2">{items}</div>'
    return (
        '<div id="timely-angles-card">'
        '<section class="bg-gray-900 rounded-xl p-5 border border-indigo-900/40">'
        '<h3 class="flex items-center gap-2 text-[11px] font-semibold tracking-widest text-indigo-400 uppercase mb-3">'
        '<span class="text-base leading-none">📡</span>Timely Angles</h3>'
        f'{body}</section></div>'
    )


def _insight_card(icon, title, body_html, accent="emerald"):
    """Card primitive for insights — uppercase microlabel + body. Suppress if no body."""
    if not body_html:
        return ""
    return (
        '<section class="bg-gray-900 rounded-xl p-5 border border-gray-800">'
        f'<h3 class="flex items-center gap-2 text-[11px] font-semibold tracking-widest text-{accent}-400 uppercase mb-3">'
        f'<span class="text-base leading-none">{icon}</span>{html.escape(title)}</h3>'
        f'{body_html}</section>'
    )


def _render_insights(*, result, scope_type, scope_key, scope_display, period, provider,
                     age_min, regen_endpoint, period_get_endpoint, target_id,
                     timely_angles=None, timely_angles_exists=False,
                     timely_angles_poll_url=None):
    """Shared renderer for /cohort/{cid}/insights and /account/{handle}/insights.

    Layout: 12-col grid → hero (col-span-12) → left data (8) + right action (4).
    Suppresses cards with no data. Hallucination warning banner sits above hero.
    """
    # Period segmented control — defined early so the empty-state response
    # can also include it. Otherwise clicking 14d/7d/24h on an uncached
    # period would strip the tabs and trap the user in the empty state.
    def _period_btn(p):
        cls = "bg-purple-700 text-white" if p == period else "text-gray-400 hover:text-white"
        return (
            f'<a hx-get="{period_get_endpoint}?period={p}" hx-target="#{target_id}" hx-swap="innerHTML" '
            f'class="px-3 py-1 text-xs rounded {cls}">{p}</a>'
        )

    period_seg = (
        '<div class="inline-flex rounded-lg bg-gray-900 border border-gray-800 p-0.5 mb-4">'
        + "".join(_period_btn(p) for p in ("24h", "7d", "14d", "30d"))
        + '</div>'
    )

    if not result:
        return (
            f'{period_seg}'
            '<div class="text-center py-8">'
            '<p class="text-gray-500 text-sm mb-4">No insights yet for this period.</p>'
            f'<button hx-post="{regen_endpoint}" hx-target="#{target_id}" hx-swap="innerHTML" '
            'class="text-sm px-4 py-2 rounded bg-purple-700 hover:bg-purple-600 text-white transition">'
            '✨ Generate Insights</button></div>'
        )

    warnings = result.get("_warnings") or {}
    # Handle classification sets for deeplink routing (Follow-up C).
    #   internal_handles → /account/<h>     (emerald, internal)
    #   external_handles → x.com/<h>        (cyan, ↗ glyph, new tab)
    #   anything else    → left as plain text
    _hs = result.get("_handle_sets") or {}
    internal_handles_set = {h.lower() for h in (_hs.get("internal") or [])}
    external_handles_set = {h.lower() for h in (_hs.get("external") or [])}

    _HANDLE_RE_LOCAL = re.compile(r"@([A-Za-z0-9_]{1,15})\b")

    def linkify(text: str) -> str:
        """Escape + linkify @handles. Internal → /account/<h>, external →
        x.com/<h>, unknown → plain text. Safe for inline use anywhere we
        currently call html.escape() on prose."""
        if not text:
            return ""
        escaped = html.escape(text)
        def _sub(m):
            h = m.group(1)
            hl = h.lower()
            if hl in internal_handles_set:
                return (f'<a href="/account/{h}" '
                        f'class="text-emerald-400 hover:underline">@{h}</a>')
            if hl in external_handles_set:
                return (f'<a href="https://x.com/{h}" target="_blank" rel="noopener" '
                        f'class="text-cyan-400 hover:underline">@{h}'
                        f'<span class="text-[10px] ml-px">↗</span></a>')
            return f"@{h}"
        return _HANDLE_RE_LOCAL.sub(_sub, escaped)

    behavioral_headline = result.get("behavioral_headline") or ""
    strategic_thesis = result.get("strategic_thesis") or ""
    account_classification = result.get("account_classification") or {}
    summary = result.get("period_summary") or ""
    topics = result.get("top_topics") or []
    top_performers = result.get("top_performers") or []
    hidden_patterns = result.get("hidden_patterns") or []
    whats_working = result.get("whats_working") or []
    weaknesses = result.get("weaknesses") or []
    to_improve = result.get("to_improve") or []
    operator_actions = result.get("operator_actions") or []
    content_series = result.get("content_series") or []
    posting_insight = result.get("posting_insight") or ""
    kudos = result.get("kudos") or []
    content_formula = result.get("content_formula") or ""
    content_ideas = result.get("content_ideas") or []

    age_str = f"{age_min}m ago" if age_min is not None else "just now"
    classification_type = html.escape(account_classification.get("type") or "")
    classification_label = classification_type.replace("_", " ").title() if classification_type else ""

    # Hero footer link
    leaderboard_link = ""
    if scope_type == "cohort":
        leaderboard_link = (
            '<div class="mt-3 text-xs text-gray-500">'
            f'<button @click="tab=\'leaderboard\'" class="text-emerald-400 hover:underline">See full leaderboard →</button>'
            '</div>'
        )
    elif scope_type == "account":
        leaderboard_link = (
            '<div class="mt-3 text-xs text-gray-500">'
            f'<a href="/account/{html.escape(scope_display.lstrip("@"))}/posts?period={html.escape(period)}" class="text-emerald-400 hover:underline">See all posts in this period →</a>'
            '</div>'
        )

    # Classification + posting insight chip (shown below headline)
    meta_chips = ""
    if classification_label:
        ac_explain = (account_classification.get("explanation") or "").strip()
        chip_inner = (
            tip(
                classification_label,
                f'Account archetype this strategist assigns:\n\n'
                f'• <strong>community_mascot</strong>: a personal voice that '
                f'represents a project to its community.\n'
                f'• <strong>brand</strong>: official voice of a product or org.\n'
                f'• <strong>creator</strong>: independent voice with personal '
                f'authority in a niche.\n'
                f'• <strong>media</strong>: publication-style coverage.\n'
                f'• <strong>community_hub</strong>: connector account that '
                f'amplifies others.\n\n'
                + (f'<strong>For this account:</strong> {ac_explain}' if ac_explain else ''),
            )
            if ac_explain else classification_label
        )
        meta_chips += (
            f'<span class="inline-block px-2 py-0.5 rounded bg-purple-900/50 border border-purple-700/50 text-purple-300 text-[11px] mr-2">'
            f'{chip_inner}</span>'
        )
    if posting_insight:
        meta_chips += (
            f'<span class="text-[11px] text-cyan-400/80">📅 {linkify(posting_insight)}</span>'
        )

    # Hero — headline primary, strategic thesis as the strategic story
    # paragraph, period_summary as a smaller data tension line below.
    hero_headline = behavioral_headline or strategic_thesis or summary
    thesis_html = ""
    if strategic_thesis and strategic_thesis != hero_headline:
        thesis_html = (
            '<p class="mt-3 text-[15px] text-gray-200 max-w-2xl leading-relaxed">'
            f'{linkify(strategic_thesis)}</p>'
        )
    summary_html = ""
    if summary and summary != hero_headline and summary != strategic_thesis:
        summary_html = (
            '<p class="mt-2 text-xs text-gray-500 max-w-2xl leading-relaxed italic">'
            f'{linkify(summary)}</p>'
        )
    hero = (
        '<section class="col-span-12 bg-gradient-to-br from-gray-900 to-gray-900/40 rounded-2xl p-6 lg:p-8 border border-gray-800">'
        '<div class="flex items-start justify-between gap-4 flex-wrap">'
        '<div class="flex-1 min-w-[280px]">'
        f'<div class="text-[11px] uppercase tracking-widest text-emerald-400 mb-2">Insight · {html.escape(scope_display)} · {html.escape(period)}</div>'
        f'<h2 class="text-xl lg:text-2xl font-semibold text-gray-100 leading-snug max-w-prose">{linkify(hero_headline)}</h2>'
        + thesis_html
        + summary_html
        + (f'<div class="mt-3 flex items-center flex-wrap gap-2">{meta_chips}</div>' if meta_chips else "")
        + leaderboard_link
        + '</div>'
        '<div class="text-right text-xs text-gray-500 shrink-0">'
        f'<div class="font-medium text-gray-400">{html.escape(_provider_label(provider))}</div>'
        f'<div>{age_str}</div>'
        '<div class="mt-2 flex gap-1 justify-end flex-wrap">'
        + (
            # Download buttons — JSON for AI re-feeding, MD for human reading.
            # Derived from regen_endpoint so this stays scope-agnostic.
            f'<a href="{regen_endpoint.replace("/generate-insights", "/insights/export")}&format=json" '
            f'download class="text-[11px] px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 transition" '
            f'title="Download as JSON — paste into another LLM as context">⬇ JSON</a>'
            f'<a href="{regen_endpoint.replace("/generate-insights", "/insights/export")}&format=md" '
            f'download class="text-[11px] px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 transition" '
            f'title="Download as Markdown — readable on its own">⬇ MD</a>'
            if regen_endpoint and "/generate-insights" in regen_endpoint else ""
        )
        + f'<button hx-post="{regen_endpoint}" hx-target="#{target_id}" hx-swap="innerHTML" '
        'class="text-[11px] px-2.5 py-1 rounded bg-purple-700/80 hover:bg-purple-600 text-white transition" '
        'title="Burn tokens, regenerate fresh">↻ Regenerate</button>'
        '</div></div></div></section>'
    )

    # Warning UI — two distinct surfaces:
    #   1. Amber banner ONLY when the LLM fabricated tweet_ids (factual claims
    #      that couldn't be grounded — these were stripped).
    #   2. Small cyan info chip for "external references" — handles the LLM
    #      mentioned that aren't in our DB. These may be real ecosystem
    #      entities (@solana, @toly, etc.) so we surface them without
    #      censoring the prose. The user can curate them into
    #      cohorts.ecosystem_handles to make them stop appearing here.
    warning_html = ""
    external_chip_html = ""
    if warnings:
        removed_t = warnings.get("removed_tweet_ids") or []
        external_h = warnings.get("external_handles_in_prose") or []
        # Back-compat with the pre-Tier-2 cache shape (removed_handles existed)
        legacy_removed_h = warnings.get("removed_handles") or []

        if removed_t:
            n = len(removed_t)
            warning_html = (
                '<div class="col-span-12 flex items-start gap-3 rounded-lg bg-amber-900/30 border border-amber-700/60 px-4 py-3">'
                '<div class="text-amber-400 text-lg leading-none">⚠</div>'
                '<div class="text-sm text-amber-200 flex-1">'
                f'This insight fabricated <strong>{n}</strong> tweet ID{"s" if n != 1 else ""} not in your data — those references were dropped. '
                f'<button class="ml-2 underline text-amber-300 hover:text-amber-100" hx-post="{regen_endpoint}" hx-target="#{target_id}" hx-swap="innerHTML">Re-run</button>'
                '<details class="mt-1 text-xs text-amber-300/80"><summary class="cursor-pointer">Show fabricated IDs</summary>'
                f'<code class="block mt-1 text-amber-200/80">{html.escape(", ".join(removed_t))}</code>'
                '</details></div></div>'
            )

        chip_handles = [h for h in (external_h or legacy_removed_h) if len(h) >= 3]
        if chip_handles:
            handle_links = " ".join(
                f'<a href="https://x.com/{html.escape(h)}" target="_blank" rel="noopener" '
                f'class="text-cyan-300 hover:text-cyan-200 hover:underline">@{html.escape(h)}</a>'
                for h in chip_handles[:8]
            )
            extra = f" + {len(chip_handles) - 8} more" if len(chip_handles) > 8 else ""
            external_chip_html = (
                '<div class="col-span-12 flex items-center gap-2 text-[12px] text-cyan-400/80 px-2">'
                '<span class="text-cyan-500">ℹ︎</span>'
                f'<span>External references the analysis mentions but aren\'t in your tracked data:</span>'
                f'<span class="space-x-1">{handle_links}{html.escape(extra)}</span>'
                '</div>'
            )

    # Operator actions — "Strategic Moves" box, shown after hero
    operator_html = ""
    if operator_actions:
        items = "".join(
            f'<li class="flex items-start gap-2 text-sm text-gray-200 leading-relaxed">'
            f'<span class="text-cyan-400 font-bold shrink-0">{i+1}.</span>'
            f'<span>{linkify(a)}</span></li>'
            for i, a in enumerate(operator_actions[:3])
        )
        operator_html = (
            '<div class="col-span-12 rounded-xl border border-cyan-800/50 bg-cyan-950/20 p-5">'
            '<h3 class="text-xs font-semibold tracking-widest text-cyan-400 uppercase mb-3">⚡ Strategic Moves</h3>'
            f'<ol class="space-y-2">{items}</ol>'
            '</div>'
        )

    # Topics chips
    topics_html = "".join(
        '<span class="inline-block px-2.5 py-1 rounded-full text-xs bg-gray-800 text-emerald-400 mr-1.5 mb-1.5 border border-gray-700">'
        f'#{html.escape(t.get("topic") or "")} '
        f'<span class="text-gray-500">{t.get("mentions") or t.get("total_mentions") or 0}</span></span>'
        for t in topics[:12] if t.get("topic")
    )
    topics_card = _insight_card("📊", "Topics", topics_html, accent="emerald")

    # Top performers — inline metrics + dual link (x.com permalink + internal detail)
    def _tweet_links(tid):
        if not tid:
            return ""
        tid_safe = html.escape(str(tid))
        # x.com supports `/i/web/status/<id>` which resolves without needing
        # the author handle — works for both account and cohort scope.
        # For account scope we know the handle; use it for a prettier URL.
        if scope_type == "account":
            author = html.escape(scope_display.lstrip("@"))
            x_url = f"https://x.com/{author}/status/{tid_safe}"
        else:
            x_url = f"https://x.com/i/web/status/{tid_safe}"
        return (
            f'<div class="flex gap-3 text-[10px] text-gray-500">'
            f'<a href="{x_url}" target="_blank" rel="noopener" '
            f'class="hover:text-cyan-400">view on x.com ↗</a>'
            f'<a href="/tweet/{tid_safe}" class="hover:text-emerald-400">vibechecx detail →</a>'
            f'</div>'
        )

    performers_html = "".join(
        '<div class="bg-gray-800/40 rounded-lg p-3 text-xs space-y-1.5">'
        + (
            f'<div class="text-[10px] text-emerald-400 mb-1">'
            + (
                f'<a href="/account/{html.escape((p.get("authored_by") or "").lstrip("@"))}" '
                f'class="hover:underline">@{html.escape((p.get("authored_by") or "").lstrip("@"))}</a>'
                if p.get("authored_by") else ""
            )
            + '</div>'
            if p.get("authored_by") else ""
        )
        + f'<div class="text-gray-300 leading-relaxed">{linkify((p.get("content_preview") or "")[:160])}</div>'
        '<div class="flex flex-wrap gap-x-4 gap-y-1 text-gray-400 text-[11px]">'
        f'<span>♥ Likes <span class="text-gray-200 font-medium">{p.get("likes", 0):,}</span></span>'
        f'<span>👁 Views <span class="text-gray-200 font-medium">{p.get("views", 0):,}</span></span>'
        f'<span>💬 Replies <span class="text-gray-200 font-medium">{p.get("replies_count", p.get("replies", 0)):,}</span></span>'
        f'<span>🔁 Retweets <span class="text-gray-200 font-medium">{p.get("retweets", 0):,}</span></span>'
        + (f'<span class="ml-auto text-gray-400">' + tip(
            f'Q{round(p.get("quality_score") or 0)}',
            'Quality score (0–100): composite engagement depth, normalised to the '
            'top performer in this period.\n\n'
            '<strong>Formula:</strong> <code>reply_count×30 + likes×20 + '
            'retweets×25 + log(views+1)×5</code>, normalised to 100.\n\n'
            'Higher = the post triggered deeper engagement relative to its peers '
            'this period. Conversation (replies) is weighted highest because the '
            'X feed rewards it most.',
            with_icon=False,
        ) + '</span>' if p.get("quality_score") else "")
        + '</div>'
        f'<div class="text-emerald-400 italic leading-snug">{linkify(p.get("why") or "")}</div>'
        + (
            _tweet_links(p.get("tweet_id"))
            if p.get("tweet_id") else ""
        )
        + '</div>'
        for p in top_performers[:5]
    )
    if performers_html:
        performers_html = f'<div class="space-y-2">{performers_html}</div>'
    performers_card = _insight_card("🏅", "Top performers", performers_html, accent="emerald")

    # Hidden patterns
    patterns_html = "".join(
        f'<li class="flex items-start gap-2 text-sm text-gray-300 leading-relaxed">'
        f'<span class="text-amber-400 leading-none mt-1 shrink-0">🔍</span><span>{linkify(p)}</span></li>'
        for p in hidden_patterns[:4]
    )
    if patterns_html:
        patterns_html = f'<ul class="space-y-2">{patterns_html}</ul>'
    patterns_card = _insight_card("🔍", "Hidden patterns", patterns_html, accent="amber") if patterns_html else ""

    # Kudos — the handle chip routes to /account/<h> when internal,
    # https://x.com/<h> when external/unknown.
    def _kudos_handle_link(handle: str) -> str:
        h = (handle or "").lstrip("@").strip()
        if not h:
            return html.escape(handle or "")
        hl = h.lower()
        h_safe = html.escape(h)
        if hl in internal_handles_set:
            return (f'<a href="/account/{h_safe}?period={html.escape(period)}" '
                    f'class="text-emerald-400 hover:underline">@{h_safe}</a>')
        # Even unknown handles are now allowed in kudos prose; link them
        # externally so the user can verify on x.com.
        return (f'<a href="https://x.com/{h_safe}" target="_blank" rel="noopener" '
                f'class="text-cyan-400 hover:underline">@{h_safe}'
                f'<span class="text-[10px] ml-px">↗</span></a>')

    kudos_html = "".join(
        '<div class="flex items-start gap-2 text-sm text-gray-300 leading-relaxed">'
        '<span class="text-purple-400 leading-none mt-0.5 shrink-0">🏆</span>'
        f'<div>{_kudos_handle_link(k.get("handle"))} '
        f'<span class="text-gray-400">{linkify(k.get("reason") or "")}</span></div>'
        '</div>'
        for k in kudos[:6] if k.get("handle")
    )
    if kudos_html:
        kudos_html = f'<div class="space-y-2">{kudos_html}</div>'
    kudos_card = _insight_card("🏆", "Kudos", kudos_html, accent="purple")

    # What's working
    working_html = "".join(
        f'<li class="flex items-start gap-2 text-sm text-gray-300 leading-relaxed">'
        f'<span class="text-emerald-400 leading-none mt-1 shrink-0">✓</span><span>{linkify(w)}</span></li>'
        for w in whats_working[:6]
    )
    if working_html:
        working_html = f'<ul class="space-y-1.5">{working_html}</ul>'
    working_card = _insight_card("✓", "What's working", working_html, accent="emerald")

    # Weaknesses — red-tinted, explicit
    weak_html = "".join(
        f'<li class="flex items-start gap-2 text-sm text-gray-300 leading-relaxed">'
        f'<span class="text-red-400 leading-none mt-1 shrink-0">⚠</span><span>{linkify(w)}</span></li>'
        for w in weaknesses[:5]
    )
    if weak_html:
        weak_html = f'<ul class="space-y-1.5">{weak_html}</ul>'
    weak_card = (
        '<section class="bg-gray-900 rounded-xl p-5 border border-red-900/40">'
        '<h3 class="flex items-center gap-2 text-[11px] font-semibold tracking-widest text-red-400 uppercase mb-3">⚠ Blind Spots</h3>'
        f'{weak_html}</section>'
    ) if weak_html else ""

    # To improve
    improve_html = "".join(
        f'<li class="flex items-start gap-2 text-sm text-gray-300 leading-relaxed">'
        f'<span class="text-amber-400 leading-none mt-1 shrink-0">💡</span><span>{linkify(w)}</span></li>'
        for w in to_improve[:6]
    )
    if improve_html:
        improve_html = f'<ul class="space-y-1.5">{improve_html}</ul>'
    improve_card = _insight_card("💡", "To improve", improve_html, accent="amber")

    # Content series — named recurring formats
    series_html = "".join(
        '<div class="mb-4 last:mb-0">'
        f'<div class="font-medium text-gray-200 text-sm">{linkify(s.get("name") or "")}</div>'
        + (f'<div class="text-xs text-gray-500 mt-0.5">{linkify(s.get("format") or "")}</div>' if s.get("format") else "")
        + (
            f'<div class="mt-1.5 bg-gray-800 rounded p-2 text-xs text-gray-400 font-mono italic leading-relaxed">'
            f'"{linkify(s.get("example") or "")}"</div>'
            if s.get("example") else ""
        )
        + '</div>'
        for s in content_series[:3]
    )
    series_card = (
        '<section class="bg-gray-900 rounded-xl p-5 border border-purple-800/40">'
        '<h3 class="flex items-center gap-2 text-[11px] font-semibold tracking-widest text-purple-400 uppercase mb-3">📺 Recurring Series</h3>'
        f'{series_html}</section>'
    ) if series_html else ""

    # Content ideas — formula block above the ideas list
    ideas_body = ""
    if content_formula:
        ideas_body += (
            '<div class="mb-3 p-3 rounded-lg bg-cyan-950/30 border border-cyan-800/30 text-xs text-cyan-200 leading-relaxed">'
            f'<span class="font-semibold text-cyan-400 uppercase tracking-wide text-[10px]">Formula · </span>'
            f'{linkify(content_formula)}</div>'
        )
    ideas_list = "".join(
        f'<li class="flex items-start gap-2 text-sm text-gray-300 leading-relaxed">'
        f'<span class="text-cyan-400 leading-none mt-1 shrink-0">✎</span><span>{linkify(i)}</span></li>'
        for i in content_ideas[:6]
    )
    if ideas_list:
        ideas_body += f'<ul class="space-y-1.5">{ideas_list}</ul>'
    ideas_card = _insight_card("✎", "Content ideas", ideas_body, accent="cyan")

    # Layout: hero + operator actions full-width, then auto-balanced
    # multi-column for the rest. CSS Multi-Column packs the cards into 2 (or 3
    # on very wide screens) columns by computed height — no more dead space
    # on the left when right column has more cards. Each card gets
    # break-inside-avoid + mb-4 so it stays atomic and rhythmic.
    #
    # Card order is the reading priority: proof (performers) → insight
    # (patterns) → strategic gaps (blind spots) → positives (working) →
    # tactical (improve/series/ideas) → context (topics, kudos).
    timely_card = (
        _render_timely_angles(timely_angles, timely_angles_exists, timely_angles_poll_url)
        if timely_angles_poll_url else ""
    )

    cards_in_order = [
        performers_card, patterns_card, weak_card, working_card,
        improve_card, kudos_card, series_card, ideas_card, topics_card,
        timely_card,
    ]
    masonry_html = "".join(
        f'<div class="break-inside-avoid mb-4">{c}</div>'
        for c in cards_in_order if c
    )
    cards_section = (
        '<section class="col-span-12 columns-1 xl:columns-2 2xl:columns-3 gap-4">'
        f'{masonry_html}'
        '</section>'
    ) if masonry_html else ""

    return (
        period_seg
        + '<div class="grid grid-cols-12 gap-4 w-full">'
        + warning_html
        + external_chip_html
        + hero
        + operator_html  # full-width "Strategic Moves" after hero, before cards
        + cards_section
        + '</div>'
    )


def _insight_to_markdown(insight: dict, scope_display: str, period: str,
                         provider: str | None) -> str:
    """Render the insight JSON as a clean Markdown brief — readable on its own
    or as input to another LLM. Order matches the visual panel."""
    if not insight:
        return f"# {scope_display} — {period}\n\n_No insight generated._\n"
    lines: list[str] = []
    lines.append(f"# {scope_display} — VibeChecx Insight ({period})")
    if provider:
        lines.append(f"_Provider: {_provider_label(provider)}_  ")
    bh = insight.get("behavioral_headline")
    if bh:
        lines.append(f"\n## Headline\n> {bh}")
    st = insight.get("strategic_thesis")
    if st:
        lines.append(f"\n## Strategic thesis\n{st}")
    ps = insight.get("period_summary")
    if ps:
        lines.append(f"\n## Period summary\n{ps}")
    ac = insight.get("account_classification") or {}
    if ac.get("type"):
        lines.append(f"\n**Account type:** {ac.get('type')}: {ac.get('explanation','')}")
    pi = insight.get("posting_insight")
    if pi:
        lines.append(f"\n**Posting insight:** {pi}")
    ops = insight.get("operator_actions") or []
    if ops:
        lines.append("\n## ⚡ Strategic Moves")
        for i, a in enumerate(ops, 1):
            lines.append(f"{i}. {a}")
    tps = insight.get("top_performers") or []
    if tps:
        lines.append("\n## 🏅 Top performers")
        for tp in tps:
            tid = tp.get("tweet_id", "")
            author = tp.get("authored_by") or ""
            author_str = f" · {author}" if author else ""
            lines.append(f"\n- **{(tp.get('content_preview') or '')[:100]}**{author_str}")
            lines.append(
                f"  - ♥ {tp.get('likes',0)} · 👁 {tp.get('views',0)} · "
                f"💬 {tp.get('replies_count', tp.get('replies',0))} · "
                f"🔁 {tp.get('retweets',0)} · Q{round(tp.get('quality_score') or 0)}"
            )
            why = tp.get("why")
            if why:
                lines.append(f"  - _Why:_ {why}")
            if tid:
                lines.append(f"  - tweet_id: `{tid}`")
    hp = insight.get("hidden_patterns") or []
    if hp:
        lines.append("\n## 🔍 Hidden patterns")
        for p in hp:
            lines.append(f"- {p}")
    ww = insight.get("whats_working") or []
    if ww:
        lines.append("\n## ✓ What's working")
        for w in ww:
            lines.append(f"- {w}")
    wk = insight.get("weaknesses") or []
    if wk:
        lines.append("\n## ⚠ Blind spots")
        for w in wk:
            lines.append(f"- {w}")
    ti = insight.get("to_improve") or []
    if ti:
        lines.append("\n## 💡 To improve")
        for w in ti:
            lines.append(f"- {w}")
    kudos = insight.get("kudos") or []
    if kudos:
        lines.append("\n## 🏆 Kudos")
        for k in kudos:
            lines.append(f"- **{k.get('handle','?')}**: {k.get('reason','')}")
    cs = insight.get("content_series") or []
    if cs:
        lines.append("\n## 📺 Recurring series")
        for s in cs:
            lines.append(f"\n**{s.get('name','?')}**")
            if s.get("format"):
                lines.append(f"- Format: {s.get('format')}")
            if s.get("example"):
                lines.append(f"- Example: _{s.get('example')}_")
    cf = insight.get("content_formula")
    ci = insight.get("content_ideas") or []
    if cf or ci:
        lines.append("\n## ✎ Content ideas")
        if cf:
            lines.append(f"\n**Formula:** {cf}\n")
        for i in ci:
            lines.append(f"- {i}")
    topics = insight.get("top_topics") or []
    if topics:
        lines.append("\n## 📊 Topics")
        topic_chips = ", ".join(
            f"#{t.get('topic','?')} ({t.get('mentions', t.get('post_mentions', 0))})"
            for t in topics[:12]
        )
        lines.append(topic_chips)
    warnings = insight.get("_warnings") or {}
    if warnings.get("removed_tweet_ids"):
        lines.append("\n---")
        lines.append(f"\n_⚠ Warnings: fabricated tweet_ids removed: "
                     f"{', '.join(warnings['removed_tweet_ids'])}_")
    return "\n".join(lines) + "\n"


def insight_filename_base(display: str, period: str, generated_at=None) -> str:
    """Build a self-describing filename:
        vibechecx_<scope-slug>_<period>_<YYYY-MM-DD>

    Strips a trailing ' · 7d' / ' · 30d' from the display label so the
    period isn't duplicated, lowercases + hyphenates for filesystem-safe
    output, and stamps the generation date so users can tell snapshots
    apart at a glance.

    Examples
        'Solflare Affiliates · 30d', '30d', 2026-05-26
            → 'vibechecx_solflare-affiliates_30d_2026-05-26'
        '@solgab · 7d',              '7d',  2026-05-26
            → 'vibechecx_solgab_7d_2026-05-26'
    """
    from datetime import datetime as _dt, timezone as _tz
    cleaned = re.sub(r"\s*[·•|]\s*\d+[dhw]\s*$", "", display or "").strip().lstrip("@")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned).strip("-").lower()[:40] or "insight"
    when = generated_at or _dt.now(_tz.utc)
    date = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)[:10]
    return f"vibechecx_{slug}_{period}_{date}"


def _insight_export_response(scope_type: str, scope_id: int,
                             scope_display: str, period: str, fmt: str):
    """Shared handler for both account + cohort insight export."""
    from fastapi.responses import Response
    insight, provider, _from_cache, _age = vi.cached_insights(scope_type, scope_id, period, generate_if_missing=False)
    if not insight:
        return Response(
            content=f"No insight cached for {scope_display} ({period}). "
                    f"Generate one first.",
            status_code=404, media_type="text/plain",
        )
    filename_base = insight_filename_base(scope_display, period)
    if fmt == "md":
        body = _insight_to_markdown(insight, scope_display, period, provider)
        return Response(
            content=body, media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.md"'},
        )
    # default: json
    payload = {
        "scope_type": scope_type, "scope_display": scope_display,
        "period": period, "provider": _provider_label(provider), "insight": insight,
    }
    body = json.dumps(payload, indent=2, default=str)
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.json"'},
    )
