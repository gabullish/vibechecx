"""web/routes/auth.py — /login, /register, /logout"""
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.core import get_user, require_login
from web.ui import AH, AF

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vibechecx_auth import (  # noqa: E402
    register,
    login as auth_login,
    create_session,
    logout as auth_logout,
    rotate_sessions,
)
from vibechecx_config import REGISTRATION_OPEN  # noqa: E402
from web.security import record_failed_login, is_login_blocked, clear_failed_logins  # noqa: E402

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(r: Request, error: str = ""):
    if get_user(r):
        return RedirectResponse("/", status_code=302)
    e = (
        f'<div class="bg-red-900/50 text-red-300 text-sm p-3 rounded-lg mb-4">{html.escape(error)}</div>'
        if error
        else ""
    )
    return AH + (
        '<div class="text-center mb-8"><div class="text-2xl font-bold bg-gradient-to-r '
        'from-emerald-400 to-cyan-400 bg-clip-text text-transparent">VibeChecx</div>'
        '<div class="text-sm text-gray-500 mt-1">Sign in</div></div>'
        f'{e}<form method="post" class="space-y-4">'
        '<input type="text" name="username" placeholder="Username" required '
        'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm">'
        '<input type="password" name="password" placeholder="Password" required '
        'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm">'
        '<button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white '
        'rounded-lg py-2.5 text-sm">Sign in</button></form>'
        '<p class="text-center text-sm text-gray-500 mt-4">No account? '
        '<a href="/register" class="text-emerald-400">Create one</a></p>'
    ) + AF


@router.post("/login", response_class=HTMLResponse)
async def login_post(r: Request):
    if is_login_blocked(r):
        return HTMLResponse(
            AH + '<div class="bg-red-900/50 text-red-300 text-sm p-4 rounded-lg text-center">'
            'Too many failed attempts. Please wait 5 minutes and try again.</div>' + AF,
            status_code=429,
        )
    f = await r.form()
    u = (f.get("username") or "").strip()
    p = f.get("password") or ""
    uid, err = auth_login(u, p)
    if err:
        record_failed_login(r)
        return login_page(r, error=err)
    clear_failed_logins(r)
    rotate_sessions(uid)
    sid = create_session(uid)
    resp = RedirectResponse("/profiles", status_code=302)
    secure = r.headers.get("x-forwarded-proto", "") == "https"
    resp.set_cookie(
        "vibechecx_session", sid, max_age=30 * 86400, httponly=True, samesite="lax", secure=secure,
    )
    # Clear any leftover profile cookie from a prior user on the same browser.
    resp.delete_cookie("vibechecx_profile")
    return resp


def _register_closed_page():
    return AH + (
        '<div class="text-center mb-8"><div class="text-2xl font-bold bg-gradient-to-r '
        'from-emerald-400 to-cyan-400 bg-clip-text text-transparent">VibeChecx</div>'
        '<div class="text-sm text-gray-500 mt-1">Registration closed</div></div>'
        '<div class="bg-gray-800 border border-gray-700 rounded-xl p-6 text-center">'
        '<p class="text-gray-300 mb-2">Registration is currently invite-only.</p>'
        '<p class="text-gray-500 text-sm">Contact the admin to get access.</p>'
        '</div>'
        '<p class="text-center text-sm text-gray-500 mt-4">Have an account? '
        '<a href="/login" class="text-emerald-400">Sign in</a></p>'
    ) + AF


@router.get("/register", response_class=HTMLResponse)
def register_page(r: Request, error: str = ""):
    if get_user(r):
        return RedirectResponse("/", status_code=302)
    if not REGISTRATION_OPEN:
        return _register_closed_page()
    e = (
        f'<div class="bg-red-900/50 text-red-300 text-sm p-3 rounded-lg mb-4">{html.escape(error)}</div>'
        if error
        else ""
    )
    return AH + (
        '<div class="text-center mb-8"><div class="text-2xl font-bold bg-gradient-to-r '
        'from-emerald-400 to-cyan-400 bg-clip-text text-transparent">VibeChecx</div>'
        '<div class="text-sm text-gray-500 mt-1">Create</div></div>'
        f'{e}<form method="post" class="space-y-4">'
        '<input type="text" name="username" placeholder="Username" required '
        'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm">'
        '<input type="password" name="password" placeholder="Password" minlength="4" required '
        'class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm">'
        '<button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white '
        'rounded-lg py-2.5 text-sm">Create</button></form>'
        '<p class="text-center text-sm text-gray-500 mt-4">Have one? '
        '<a href="/login" class="text-emerald-400">Sign in</a></p>'
    ) + AF


@router.post("/register", response_class=HTMLResponse)
async def register_post(r: Request):
    if not REGISTRATION_OPEN:
        return _register_closed_page()
    f = await r.form()
    u = (f.get("username") or "").strip()
    p = f.get("password") or ""
    if len(u) < 2:
        return register_page(r, error="Username too short")
    if len(p) < 4:
        return register_page(r, error="Password too short")
    ok, err = register(u, p)
    if not ok:
        return register_page(r, error=err)
    uid, _ = auth_login(u, p)
    rotate_sessions(uid)
    sid = create_session(uid)
    resp = RedirectResponse("/profiles", status_code=302)
    secure = r.headers.get("x-forwarded-proto", "") == "https"
    resp.set_cookie(
        "vibechecx_session", sid, max_age=30 * 86400, httponly=True, samesite="lax", secure=secure,
    )
    resp.delete_cookie("vibechecx_profile")
    return resp


@router.get("/logout", response_class=HTMLResponse)
def logout_route(r: Request):
    auth_logout(r.cookies.get("vibechecx_session"))
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("vibechecx_session")
    resp.delete_cookie("vibechecx_profile")
    return resp
