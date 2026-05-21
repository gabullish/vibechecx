"""web/routes/discovery.py — /discover/..., /wizard/..."""
import html
import json
import secrets
import importlib.util
import logging

import os
import sys
import subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from vibechecx_config import COLLECTOR_DIR  # noqa: E402
from vibechecx_scrape_status import start_session as ss_start  # noqa: E402

from web.core import q, get_user, require_login  # noqa: E402
from web.ui import header_html, tip, HF  # noqa: E402

logger = logging.getLogger("vibechecx.web")

router = APIRouter()


# ── Shared helpers ────────────────────────────────────────────────────


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
    by_name = {m["username"].lower(): m for m in all_members}
    for uname in selected_usernames:
        ulow = uname.lower().lstrip("@")
        m = by_name.get(ulow, {"username": ulow})
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


def _load_discover_module():
    spec = importlib.util.spec_from_file_location(
        "vibechecx_discover", os.path.join(COLLECTOR_DIR, "discover.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Discover routes ───────────────────────────────────────────────────


@router.get("/discover", response_class=HTMLResponse)
def disc(r: Request, step: int = 1, error: str = ""):
    redir = require_login(r)
    if redir:
        return redir
    e = (
        f'<div class="bg-red-900/50 text-red-300 text-sm p-3 rounded-lg mb-4">{html.escape(error)}</div>'
        if error
        else ""
    )
    suggest = r.query_params.get("suggest", "")
    if step == 1:
        if suggest:
            h = suggest.lstrip("@")
            return header_html(0) + (
                '<h1 class="text-2xl font-semibold mb-2">Discover Your Cohort</h1>'
                '<p class="text-gray-500 text-sm mb-6">Enter a project\'s X handle — '
                'we\'ll scrape its affiliates page and find members.</p>'
                f'{e}<form id="suggest-form" hx-post="/discover/start" hx-target="#disc-result" hx-swap="innerHTML" class="flex gap-3 max-w-lg">'
                f'<input type="text" name="handle" value="@{html.escape(h)}" required '
                'class="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm">'
                '<button type="submit" class="bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Discover</button>'
                '</form><div id="disc-result"></div>'
                '<script>setTimeout(function(){document.getElementById("suggest-form").querySelector("button").click()},200)</script>'
            ) + HF
        return header_html(0) + (
            "<h1 class='text-2xl font-semibold mb-2'>Discover Your Cohort</h1>"
            "<p class='text-gray-500 text-sm mb-6'>Enter a project's X handle — we'll scrape "
            "its affiliates page and find members.</p>"
            f"{e}<form hx-post='/discover/start' hx-target='#disc-result' hx-swap='innerHTML' "
            "class='flex gap-3 max-w-lg'>"
            "<input type='text' name='handle' placeholder='@solflare' required "
            "class='flex-1 bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm'>"
            "<button type='submit' class='bg-emerald-600 hover:bg-emerald-500 text-white "
            "rounded-lg px-6 py-2.5 text-sm font-medium'>Discover</button></form>"
            "<div id='disc-result'></div>"
        ) + HF
    return header_html(0) + (
        f"<div class='text-center py-12'><p class='text-gray-500'>{html.escape(error or 'Unknown step')}</p></div>"
    ) + HF


@router.post("/discover/start", response_class=HTMLResponse)
async def disc_start(r: Request):
    redir = require_login(r)
    if redir:
        return redir
    try:
        f = await r.form()
        h = (f.get("handle") or "").strip().lstrip("@")
    except Exception:
        j = await r.json()
        h = (j.get("handle") or "").strip().lstrip("@")
    if not h:
        return "<p class='text-red-400 text-sm'>Enter a handle</p>"
    dsid = secrets.token_hex(16)
    user = get_user(r)
    q(
        "INSERT INTO discovery_sessions(id, project_handle, user_id) VALUES (%s, %s, %s)",
        (dsid, h, user["id"]),
    )
    return (
        f'<div class="mt-6 p-6 bg-gray-900 rounded-xl border border-emerald-800/30" '
        f'hx-get="/discover/check/{dsid}" hx-trigger="load delay:0.5s" hx-swap="outerHTML transition:true">'
        '<div class="flex items-center gap-4">'
        '<div class="animate-spin w-6 h-6 border-2 border-emerald-500 border-t-transparent rounded-full"></div>'
        f'<div><p class="text-emerald-400 font-medium">Scanning @{html.escape(h)}\'s affiliates...</p>'
        '<p class="text-xs text-gray-500">This takes about 10 seconds.</p></div></div></div>'
    )


@router.get("/discover/check/{dsid}", response_class=HTMLResponse)
async def disc_check(dsid: str, r: Request):
    user = get_user(r)
    if not user:
        return RedirectResponse("/login", 302)
    rows = q(
        "SELECT project_handle, members_json FROM discovery_sessions WHERE id=%s AND user_id=%s",
        (dsid, user["id"]),
    )
    if not rows:
        return "<p class='text-gray-500'>Session expired. <a href='/discover' class='text-emerald-400'>Start over</a></p>"
    handle = rows[0]["project_handle"]
    mj = rows[0]["members_json"]
    seed_avatar = ""
    if not mj:
        try:
            disc_mod = _load_discover_module()
            res = await disc_mod.discover_affiliates(handle)
        except Exception:
            logger.exception("discover_affiliates failed for %s", handle)
            res = None
        if res is None:
            return (
                '<div class="text-center py-12 bg-gray-900 rounded-xl border border-gray-800">'
                '<div class="text-5xl mb-4">⚠️</div>'
                '<h2 class="text-xl font-semibold mb-2">Could Not Check</h2>'
                f'<p class="text-gray-500 text-sm mb-4">Could not determine whether @{html.escape(handle)} has an affiliates page.<br>'
                'Network issue or rate limit? <a href="/discover" class="text-emerald-400 hover:underline">Try again</a></p></div>'
            )
        members, seed_avatar = res["members"], res.get("seed_avatar", "")
        mj = json.dumps(members)
        q("UPDATE discovery_sessions SET members_json=%s WHERE id=%s", (mj, dsid))
    else:
        members = json.loads(mj)

    if not members:
        suggestions = "".join(
            f'<a href="/discover?suggest={html.escape(t)}" '
            f'class="bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg px-3 py-1.5 text-xs transition">{html.escape(t)}</a>'
            for t in ["@solflare", "@phantom", "@magiceden", "@nytimes"]
        )
        return (
            '<div class="text-center py-12 bg-gray-900 rounded-xl border border-gray-800">'
            '<div class="text-5xl mb-4">🔍</div>'
            '<h2 class="text-xl font-semibold mb-2">No Affiliates Found</h2>'
            f'<p class="text-gray-500 text-sm mb-4 max-w-md mx-auto">'
            f'@{html.escape(handle)} doesn\'t have an X affiliates page — that\'s normal for '
            'personal accounts. You have two options:'
            '</p>'
            '<div class="max-w-md mx-auto bg-gray-800/40 rounded-xl p-5 mb-4 text-left">'
            '<div class="flex items-start gap-3">'
            '<div class="text-2xl shrink-0">✏️</div>'
            '<div class="flex-1">'
            '<div class="text-emerald-400 font-semibold text-sm">Build a custom cohort</div>'
            '<p class="text-xs text-gray-400 mb-3 mt-1">'
            f'Paste any handles you want to track alongside @{html.escape(handle)}. '
            'No discovery, just your picks.</p>'
            '<a href="/wizard/2?type=custom" '
            'class="inline-block bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-4 py-1.5 text-xs font-medium">'
            'Open custom cohort →</a></div></div></div>'
            '<div class="max-w-md mx-auto bg-gray-800/40 rounded-xl p-5 mb-4 text-left">'
            '<div class="flex items-start gap-3">'
            '<div class="text-2xl shrink-0">📊</div>'
            '<div class="flex-1">'
            '<div class="text-emerald-400 font-semibold text-sm">Track as a single account</div>'
            '<p class="text-xs text-gray-400 mb-3 mt-1">'
            f'Just @{html.escape(handle)}, on its own dashboard with insights.</p>'
            '<form action="/wizard/scan" method="POST" class="inline">'
            '<input type="hidden" name="type" value="single">'
            f'<input type="hidden" name="handle" value="{html.escape(handle)}">'
            '<button type="submit" class="bg-gray-700 hover:bg-gray-600 text-white rounded-lg px-4 py-1.5 text-xs font-medium">'
            f'Track @{html.escape(handle)} →</button></form></div></div></div>'
            '<p class="text-xs text-gray-400 mt-6 mb-2">Or try a project we know has affiliates:</p>'
            f'<div class="flex flex-wrap justify-center gap-2 mb-4">{suggestions}</div>'
            '<p class="text-xs text-gray-400"><a href="/discover" class="text-gray-500 hover:text-emerald-400">← Try another handle</a></p>'
            '</div>'
        )

    seed_hidden = (
        f'<input type="hidden" name="seed_avatar" value="{html.escape(seed_avatar)}">'
        if seed_avatar else ""
    )
    cards = "".join(
        (
            '<label class="flex items-center gap-3 p-3 rounded-lg border border-gray-800 '
            'hover:border-emerald-500/30 transition cursor-pointer bg-gray-900">'
            f'<input type="checkbox" name="members" value="{html.escape(m["username"])}" checked '
            'class="accent-emerald-500 w-4 h-4">'
            f'<img src="{html.escape(m.get("avatar", ""))}" class="w-8 h-8 rounded-full bg-gray-800" '
            'onerror="this.style.display=\'none\'" loading="lazy">'
            '<div class="flex-1">'
            f'<span class="text-emerald-400 font-medium">@{html.escape(m["username"])}</span>'
            f'<span class="text-gray-500 text-sm ml-2">{html.escape((m.get("display_name") or "")[:30])}</span>'
            f'<div class="text-xs text-gray-400">{m.get("followers", 0)} followers</div></div></label>'
        )
        for m in members
    )
    return (
        f"<h1 class='text-xl font-semibold mb-2'>@{html.escape(handle)}'s Affiliates</h1>"
        f"<p class='text-sm text-gray-500 mb-4'>Found {len(members)} members.</p>"
        f"<div class='text-xs text-emerald-500 mb-4'>✓ Scrape complete. Pick the ones you want.</div>"
        f"<form hx-post='/discover/create/{dsid}' hx-target='#disc-result2' hx-swap='innerHTML' class='space-y-2 mb-6'>"
        f"{seed_hidden}<div class='max-h-96 overflow-y-auto space-y-2'>{cards}</div>"
        f"<div class='mt-6 flex gap-3 items-center'>"
        f"<input type='text' name='custom_handle' placeholder='+ add comma separated' "
        f"class='bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm flex-1'>"
        f"<button type='submit' class='bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2 text-sm font-medium'>Create Cohort</button>"
        f"</div></form><div id='disc-result2'></div>"
    )


@router.post("/discover/create/{dsid}", response_class=HTMLResponse)
async def disc_create(dsid: str, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    f = await r.form()
    members = list(f.getlist("members"))
    custom = (f.get("custom_handle") or "").strip().lstrip("@")
    if custom:
        for ch in custom.replace(",", " ").split():
            ch = ch.strip().lstrip("@")
            if ch:
                members.append(ch)
    rows = q(
        "SELECT project_handle, members_json FROM discovery_sessions "
        "WHERE id=%s AND user_id=%s",
        (dsid, user["id"]),
    )
    if not rows:
        return "<p class='text-gray-500'>Session expired. <a href='/discover'>Start over</a></p>"
    handle = rows[0]["project_handle"]
    all_members = json.loads(rows[0]["members_json"]) if rows[0]["members_json"] else []
    seed_avatar = (f.get("seed_avatar") or "")
    _, pid = _create_cohort_with_members(
        user_id=user["id"],
        handle=handle,
        all_members=all_members,
        selected_usernames=members,
        seed_avatar=seed_avatar,
    )
    return (
        '<div class="text-center py-8 bg-gray-900 rounded-xl border border-emerald-800/30">'
        '<div class="text-4xl mb-3">🎉</div>'
        '<h2 class="text-xl font-semibold mb-1">Cohort Created!</h2>'
        f'<p class="text-gray-500 text-sm mb-4">{len(members)} members from @{html.escape(handle)}.</p>'
        f'<a href="/set-profile/{pid}" class="inline-block bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Go to Dashboard →</a>'
        '</div>'
    )


# ── Wizard routes ─────────────────────────────────────────────────────


@router.get("/wizard/{step}", response_class=HTMLResponse)
def wizard(step: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    if step == 1:
        existing = q(
            "SELECT id, name, slug FROM cohorts WHERE user_id=%s LIMIT 20", (user["id"],)
        )
        ex_cards = "".join(
            (
                f'<a href="/wizard/existing/{c["id"]}" '
                'class="block bg-gray-900 rounded-xl p-4 border border-gray-800 '
                'hover:border-emerald-500/30 transition text-left">'
                f'<div class="text-emerald-400 font-semibold text-sm">{html.escape(c["name"])}</div>'
                f'<div class="text-xs text-gray-500">{html.escape(c["slug"])}</div></a>'
            )
            for c in existing
        )
        ex_section = (
            '<div id="existing-cohorts" class="mt-8 hidden">'
            '<h3 class="text-sm text-gray-500 mb-3">Pick an existing cohort:</h3>'
            f'<div class="grid grid-cols-2 gap-3">{ex_cards}</div></div>'
            '<button class="text-xs text-gray-400 hover:text-gray-400 mt-3" '
            '_="on click toggle .hidden on #existing-cohorts">📂 Use existing cohort</button>'
            if existing
            else ""
        )
        return header_html(0) + (
            '<div class="max-w-2xl mx-auto text-center py-8">'
            '<div class="text-4xl mb-4">👋</div>'
            '<h1 class="text-2xl font-semibold mb-2">Welcome to VibeChecx</h1>'
            '<p class="text-gray-500 text-sm mb-8 max-w-md mx-auto">Track how an account or its '
            'affiliates engage across X. First, what are you tracking?</p>'
            '<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">'
            '<a href="/wizard/2?type=single" class="block bg-gray-900 rounded-xl p-6 '
            'border border-gray-800 hover:border-emerald-500/30 transition">'
            '<div class="text-3xl mb-3">📊</div><div class="text-emerald-400 font-semibold">Single Account</div>'
            '<div class="text-xs text-gray-500 mt-1">Track one X account\'s metrics</div></a>'
            '<a href="/wizard/2?type=cohort" class="block bg-gray-900 rounded-xl p-6 '
            'border border-gray-800 hover:border-emerald-500/30 transition">'
            '<div class="text-3xl mb-3">👥</div><div class="text-emerald-400 font-semibold">Discover Cohort</div>'
            '<div class="text-xs text-gray-500 mt-1">Auto-find a project\'s affiliate network</div></a>'
            '<a href="/wizard/2?type=custom" class="block bg-gray-900 rounded-xl p-6 '
            'border border-gray-800 hover:border-emerald-500/30 transition">'
            '<div class="text-3xl mb-3">✏️</div><div class="text-emerald-400 font-semibold">Custom Cohort</div>'
            '<div class="text-xs text-gray-500 mt-1">Paste any handles, no discovery needed</div></a>'
            f'</div>{ex_section}'
            '<a href="/profiles" class="text-xs text-gray-400 hover:text-gray-400 mt-6 inline-block">Back to profiles →</a>'
            '</div>'
        ) + HF
    if step == 2:
        typ = r.query_params.get("type", "cohort")
        if typ == "custom":
            return header_html(0) + (
                '<div class="max-w-xl mx-auto">'
                '<div class="text-2xl mb-2 font-semibold">✏️ Custom Cohort</div>'
                '<p class="text-gray-500 text-sm mb-6">Paste any handles — one per line, or '
                'comma/space separated. The leading <code class="text-gray-400">@</code> is optional. '
                'No affiliate discovery, no organization seed; this is your list.</p>'
                '<form action="/wizard/create-custom" method="POST" class="space-y-4">'
                '<div><label class="text-xs text-gray-500 block mb-1">Cohort name</label>'
                '<input type="text" name="name" placeholder="My favorite builders" required '
                'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm"></div>'
                '<div><label class="text-xs text-gray-500 block mb-1">Handles</label>'
                '<textarea name="handles" rows="8" required '
                'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm font-mono" '
                'placeholder="@vidor_solflare&#10;@solquicks&#10;@kasparas_sol&#10;or a comma list">'
                '</textarea></div>'
                '<button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Create Cohort</button>'
                '</form>'
                '<a href="/wizard/1" class="text-xs text-gray-400 hover:text-gray-400 mt-4 inline-block">← Back</a></div>'
            ) + HF
        return header_html(0) + (
            '<div class="max-w-lg mx-auto">'
            f'<div class="text-2xl mb-2 font-semibold">{"👥 Pick a Cohort" if typ == "cohort" else "📊 Your Handle"}</div>'
            f'<p class="text-gray-500 text-sm mb-6">Enter the X handle of the project'
            f'{" or account" if typ == "single" else ""} you want to track.</p>'
            '<form action="/wizard/scan" method="POST" class="space-y-4" x-data="{loading:false}" @submit="loading=true">'
            f'<input type="hidden" name="type" value="{html.escape(typ)}">'
            '<input type="text" name="handle" placeholder="@solflare" required '
            'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm">'
            '<button type="submit" :disabled="loading" '
            'class="w-full bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 text-white rounded-lg px-6 py-2.5 text-sm font-medium transition">'
            '<span x-show="!loading">Next →</span>'
            '<span x-show="loading" class="inline-flex items-center gap-2">'
            '<svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/></svg>'
            'Setting up your dashboard…</span>'
            '</button>'
            '</form><a href="/wizard/1" class="text-xs text-gray-400 hover:text-gray-400 mt-4 inline-block">← Back</a></div>'
        ) + HF
    if step == 3:
        dsid = r.query_params.get("sid", "")
        if not dsid:
            return RedirectResponse("/wizard/1", 302)
        typ = r.query_params.get("type", "cohort")
        ws = q(
            "SELECT project_handle, members_json FROM discovery_sessions WHERE id=%s AND user_id=%s",
            (dsid, user["id"]),
        )
        if not ws:
            return RedirectResponse("/wizard/1", 302)
        h, mj = ws[0]["project_handle"], ws[0]["members_json"]
        am = json.loads(mj) if mj else []
        sa = r.query_params.get("sa", "")
        sa_hidden = f'<input type="hidden" name="seed_avatar" value="{html.escape(sa)}">' if sa else ""
        if typ == "single":
            return header_html(0) + (
                '<div class="max-w-lg mx-auto"><div class="text-2xl mb-2 font-semibold">📊 Almost Done</div>'
                f'<p class="text-gray-500 text-sm mb-6">Name your dashboard for @{html.escape(h)}.</p>'
                '<form hx-post="/wizard/create" hx-target="#wiz-result" hx-swap="innerHTML" class="space-y-4">'
                f'<input type="hidden" name="sid" value="{html.escape(dsid)}">'
                '<input type="hidden" name="type" value="single">'
                f'{sa_hidden}<div><label class="text-xs text-gray-500 block mb-1">Dashboard name</label>'
                f'<input type="text" name="name" value="@{html.escape(h)}" '
                'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm"></div>'
                '<button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Done</button>'
                '</form><div id="wiz-result"></div></div>'
            ) + HF
        cards = "".join(
            (
                '<label class="flex items-center gap-3 p-3 rounded-lg border border-gray-800 '
                'hover:border-emerald-500/30 transition cursor-pointer bg-gray-900">'
                f'<input type="checkbox" name="members" value="{html.escape(m["username"])}" checked '
                'class="accent-emerald-500 w-4 h-4">'
                f'<img src="{html.escape(m.get("avatar", ""))}" class="w-8 h-8 rounded-full bg-gray-800" '
                'onerror="this.style.display=\'none\'" loading="lazy">'
                '<div class="flex-1">'
                f'<span class="text-emerald-400 font-medium">@{html.escape(m["username"])}</span>'
                f'<span class="text-gray-500 text-sm ml-2">{html.escape((m.get("display_name") or "")[:30])}</span>'
                f'<div class="text-xs text-gray-400">{m.get("followers", 0)} followers</div></div></label>'
            )
            for m in am[:50]
        )
        other_cohorts = q(
            """
            SELECT c.id, c.name, COUNT(cm.account_id)::int AS members
            FROM cohorts c LEFT JOIN cohort_members cm ON cm.cohort_id = c.id
            WHERE c.user_id = %s
            GROUP BY c.id, c.name
            HAVING COUNT(cm.account_id) > 0
            ORDER BY members DESC, c.name LIMIT 20
            """,
            (user["id"],),
        )
        merge_block = ""
        if other_cohorts:
            cohort_cards = "".join(
                '<label class="flex items-center gap-3 p-2 rounded-lg border border-gray-800 '
                'hover:border-purple-500/30 transition cursor-pointer bg-gray-900">'
                f'<input type="checkbox" name="merge_cohort_ids" value="{c["id"]}" '
                'class="accent-purple-500 w-4 h-4">'
                '<div class="flex-1">'
                f'<div class="text-sm text-purple-300">🔀 {html.escape(c["name"])}</div>'
                f'<div class="text-xs text-gray-400">{c["members"]} members</div>'
                '</div></label>'
                for c in other_cohorts
            )
            merge_block = (
                '<details class="border-t border-gray-800 pt-3 group">'
                '<summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-300 mb-2 flex items-center justify-between">'
                f'<span>🔀 ' + tip(
                    f'Or pull from your other cohorts ({len(other_cohorts)})',
                    'Adds every member from the selected cohorts into this new one. '
                    'Duplicate handles are skipped automatically. Useful if you\'re '
                    'consolidating two communities into one dashboard.'
                ) + '</span>'
                '<span class="text-gray-400 group-open:rotate-90 transition-transform">▶</span>'
                '</summary>'
                f'<div class="max-h-48 overflow-y-auto space-y-1 border border-gray-800 rounded-lg p-2">{cohort_cards}</div>'
                '</details>'
            )

        return header_html(0) + (
            '<div class="max-w-lg mx-auto"><div class="text-2xl mb-2 font-semibold">👥 Confirm Members</div>'
            f'<p class="text-gray-500 text-sm mb-6">{len(am)} affiliates found for @{html.escape(h)}. Uncheck any to exclude.</p>'
            '<form hx-post="/wizard/create" hx-target="#wiz-result" hx-swap="innerHTML" class="space-y-4">'
            f'<input type="hidden" name="sid" value="{html.escape(dsid)}">'
            '<input type="hidden" name="type" value="cohort">'
            f'{sa_hidden}<div class="max-h-80 overflow-y-auto space-y-2 border border-gray-800 rounded-lg p-2">{cards}</div>'
            f'{merge_block}'
            '<div><label class="text-xs text-gray-500 block mb-1">Cohort name</label>'
            f'<input type="text" name="name" value="@{html.escape(h)}" '
            'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm"></div>'
            '<button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Create Dashboard</button>'
            '</form><div id="wiz-result"></div>'
            '<a href="/profiles" class="text-xs text-gray-400 hover:text-gray-400 mt-4 inline-block">Back →</a>'
            '</div>'
        ) + HF
    return RedirectResponse("/wizard/1", 302)


@router.get("/wizard/existing/{cid}", response_class=HTMLResponse)
def wizard_existing(cid: int, r: Request):
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    c = q("SELECT id, name, slug FROM cohorts WHERE id=%s AND user_id=%s", (cid, user["id"]))
    if not c:
        return RedirectResponse("/wizard/1", 302)
    c = c[0]
    rows = q(
        "INSERT INTO profiles(user_id, name, type, cohort_id) "
        "VALUES(%s, %s, 'cohort', %s) RETURNING id",
        (user["id"], c["name"], cid),
    )
    pid = rows[0]["id"]
    return header_html(0) + (
        '<div class="max-w-lg mx-auto text-center py-8">'
        '<div class="text-4xl mb-3">✅</div>'
        '<h2 class="text-xl font-semibold mb-1">Profile Created</h2>'
        f'<p class="text-gray-500 text-sm mb-4">Dashboard for {html.escape(c["name"])} is ready.</p>'
        f'<a href="/set-profile/{pid}" class="inline-block bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Go to Dashboard →</a>'
        '</div>'
    ) + HF


@router.post("/wizard/scan", response_class=HTMLResponse)
async def wizard_scan(r: Request):
    redir = require_login(r)
    if redir:
        return redir
    f = await r.form()
    h = (f.get("handle") or "").strip().lstrip("@").lower()
    typ = f.get("type") or "cohort"
    if not h:
        return "<p class='text-red-400 text-sm'>Enter a handle</p>"
    user = get_user(r)

    if typ == "single":
        pid = _create_single_profile(user["id"], h, name=f"@{h}")
        # Auto-start initial scrape so the user lands on a live progress screen
        # rather than an empty state with a button they have to find and click.
        session_id = ss_start(
            user_id=user["id"],
            session_type="profile_scrape",
            target_handle=h,
            progress_total=100,
        )
        env = {**os.environ, "VIBECHECX_SCRAPE_SESSION_ID": str(session_id)}
        from vibechecx_config import SCRAPER_HEADFUL  # noqa: E402
        cmd = ["python3", os.path.join(COLLECTOR_DIR, "collect.py"), h, "--limit", "500"]
        if SCRAPER_HEADFUL:
            cmd.append("--headful")
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        return RedirectResponse(f"/set-profile/{pid}", status_code=303)

    dsid = secrets.token_hex(16)
    q(
        "INSERT INTO discovery_sessions(id, project_handle, user_id) VALUES (%s, %s, %s)",
        (dsid, h, user["id"]),
    )
    try:
        disc_mod = _load_discover_module()
        res = await disc_mod.discover_affiliates(h)
    except Exception:
        logger.exception("wizard_scan: discover_affiliates failed for %s", h)
        res = None
    if res is None:
        return (
            '<div class="text-center py-8 bg-gray-900 rounded-xl border border-gray-800 mt-4">'
            f'<p class="text-gray-500">Could not check @{html.escape(h)}. '
            f'<a href="/wizard/2?type={html.escape(typ)}" class="text-emerald-400">Try again</a></p></div>'
        )
    members, seed_avatar = res["members"], res.get("seed_avatar", "")
    q(
        "UPDATE discovery_sessions SET members_json=%s WHERE id=%s",
        (json.dumps(members), dsid),
    )
    sa_param = f"&sa={seed_avatar}" if seed_avatar else ""
    return RedirectResponse(f"/wizard/3?sid={dsid}&type={typ}{sa_param}", status_code=302)


@router.post("/wizard/create", response_class=HTMLResponse)
async def wizard_create(r: Request):
    redir = require_login(r)
    if redir:
        return redir
    f = await r.form()
    dsid = f.get("sid") or ""
    typ = f.get("type") or "cohort"
    name = (f.get("name") or "").strip()
    members = list(f.getlist("members")) if typ == "cohort" else []
    user = get_user(r)

    if typ == "cohort":
        merge_ids: list[int] = []
        for s in f.getlist("merge_cohort_ids"):
            try:
                merge_ids.append(int(s))
            except (TypeError, ValueError):
                pass
        if merge_ids:
            extra = q(
                """
                SELECT DISTINCT a.username
                FROM cohort_members cm
                JOIN cohorts c ON c.id = cm.cohort_id
                JOIN accounts a ON a.id = cm.account_id
                WHERE c.id = ANY(%s) AND c.user_id = %s
                """,
                (merge_ids, user["id"]),
            )
            for row in extra:
                if row["username"] not in members:
                    members.append(row["username"])

    ws = q(
        "SELECT project_handle, members_json FROM discovery_sessions WHERE id=%s AND user_id=%s",
        (dsid, user["id"]),
    )
    if not ws:
        return "<p class='text-gray-500'>Session expired. <a href='/wizard/1' class='text-emerald-400'>Start over</a></p>"
    h = ws[0]["project_handle"]
    all_members = json.loads(ws[0]["members_json"]) if ws[0]["members_json"] else []
    if typ == "single":
        pid = _create_single_profile(user["id"], h, name=name)
        return (
            '<div class="text-center py-8 bg-gray-900 rounded-xl border border-emerald-800/30">'
            '<div class="text-4xl mb-3">🎉</div>'
            '<h2 class="text-xl font-semibold mb-1">Dashboard Created!</h2>'
            f'<p class="text-gray-500 text-sm mb-4">{html.escape(name or f"@{h}")} is ready.</p>'
            f'<a href="/set-profile/{pid}" class="inline-block bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Go to Dashboard →</a>'
            '</div>'
        )
    seed_avatar = f.get("seed_avatar") or ""
    _, pid = _create_cohort_with_members(
        user_id=user["id"],
        handle=h,
        all_members=all_members,
        selected_usernames=members,
        cohort_name=name or f"@{h}",
        seed_avatar=seed_avatar,
    )
    return (
        '<div class="text-center py-8 bg-gray-900 rounded-xl border border-emerald-800/30">'
        '<div class="text-4xl mb-3">🎉</div>'
        '<h2 class="text-xl font-semibold mb-1">Cohort Created!</h2>'
        f'<p class="text-gray-500 text-sm mb-4">{len(members)} members from @{html.escape(h)}.</p>'
        f'<a href="/set-profile/{pid}" class="inline-block bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg px-6 py-2.5 text-sm font-medium">Go to Dashboard →</a>'
        '</div>'
    )


@router.post("/wizard/create-custom", response_class=HTMLResponse)
async def wizard_create_custom(r: Request):
    """Custom cohort path: take a free-form list of handles, create the
    cohort and profile directly. No discovery, no organization seed."""
    redir = require_login(r)
    if redir:
        return redir
    user = get_user(r)
    f = await r.form()
    name = (f.get("name") or "").strip() or "Custom cohort"
    handles = _parse_handles_blob(f.get("handles") or "")
    if not handles:
        return HTMLResponse(
            (
                header_html(0)
                + '<div class="max-w-xl mx-auto">'
                '<div class="bg-red-900/30 border border-red-700/60 rounded-lg p-4 text-sm text-red-200 mb-4">'
                'No valid X handles found. Handles must be 1–15 characters '
                '(letters, digits, underscore). Try again — newline, comma, or space separated.'
                '</div>'
                '<a href="/wizard/2?type=custom" class="text-emerald-400 hover:underline text-sm">← Back</a>'
                '</div>' + HF
            )
        )

    slug = f"{_slugify(name)}_{user['id']}"
    all_members = [{"username": h} for h in handles]
    _, pid = _create_cohort_with_members(
        user_id=user["id"],
        handle=_slugify(name),
        all_members=all_members,
        selected_usernames=handles,
        cohort_name=name,
        slug=slug,
    )
    return RedirectResponse(f"/set-profile/{pid}", status_code=303)
