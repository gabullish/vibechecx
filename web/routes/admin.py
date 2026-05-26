"""web/routes/admin.py — /admin page. Requires is_admin=true."""
import html
import sys
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.core import q, get_user, active_profile_name
from web.ui import header_html, rel_time, HF

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web.queue_worker import queue_depth  # noqa: E402
from vibechecx_config import deepseek_api_key, xai_api_key, openai_api_key  # noqa: E402

router = APIRouter()


def _require_admin(r: Request):
    user = get_user(r)
    if not user:
        return RedirectResponse("/login", status_code=302), None
    if not user.get("is_admin"):
        return HTMLResponse(
            '<div class="text-center py-20 text-gray-500">Access denied.</div>',
            status_code=403,
        ), None
    return None, user


def _mask_key(k: str) -> str:
    """Return a 'sk-...abc1' style masked preview, or empty marker if absent."""
    if not k:
        return '<span class="text-red-400 text-xs">not configured</span>'
    if len(k) <= 8:
        return '<span class="text-amber-300 text-xs">set (short)</span>'
    return (
        f'<code class="text-xs text-emerald-300">{html.escape(k[:4])}'
        f'<span class="text-gray-500">...</span>{html.escape(k[-4:])}</code>'
        f'<span class="text-[10px] text-gray-500 ml-1">({len(k)} chars)</span>'
    )


def _provider_row(label: str, key: str, model_env: str, default_model: str):
    model = os.environ.get(model_env, default_model)
    return (
        "<tr class='border-b border-gray-800 text-sm'>"
        f"<td class='py-2 px-3 font-medium'>{html.escape(label)}</td>"
        f"<td class='py-2 px-3'>{_mask_key(key)}</td>"
        f"<td class='py-2 px-3 text-gray-400'><code class='text-xs'>{html.escape(model)}</code></td>"
        "</tr>"
    )


def _process_section() -> str:
    """List VibeChecx-relevant processes: app, coordinator, cloudflared."""
    try:
        import psutil
    except ImportError:
        return '<p class="text-xs text-gray-500">psutil not installed.</p>'

    rows = []
    relevant_patterns = ("web/app.py", "coordinator.py", "patrol.py",
                         "cloudflared", "uvicorn")
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time",
                                      "cpu_percent", "memory_info"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if not any(pat in cmdline for pat in relevant_patterns):
                continue
            # Trim the command line for display
            display = cmdline
            for prefix in ("/usr/bin/", "/home/boto/services/vibechecx/"):
                display = display.replace(prefix, "")
            display = display[:90] + ("..." if len(display) > 90 else "")
            age_sec = time.time() - proc.info["create_time"]
            mem_mb = (proc.info["memory_info"].rss / 1024 / 1024) if proc.info["memory_info"] else 0
            rows.append({
                "pid": proc.info["pid"],
                "display": display,
                "age_sec": age_sec,
                "mem_mb": mem_mb,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
            continue

    rows.sort(key=lambda x: -x["age_sec"])  # oldest first

    def _fmt_age(s):
        if s < 60:
            return f"{int(s)}s"
        if s < 3600:
            return f"{int(s // 60)}m"
        if s < 86400:
            return f"{int(s // 3600)}h{int((s % 3600) // 60)}m"
        return f"{int(s // 86400)}d"

    body = "".join(
        "<tr class='border-b border-gray-800 text-sm font-mono'>"
        f"<td class='py-1.5 px-3 text-gray-500'>{r['pid']}</td>"
        f"<td class='py-1.5 px-3 text-gray-300'>{html.escape(r['display'])}</td>"
        f"<td class='py-1.5 px-3 text-gray-400'>{_fmt_age(r['age_sec'])}</td>"
        f"<td class='py-1.5 px-3 text-gray-400 text-right'>{r['mem_mb']:.0f} MB</td>"
        "</tr>"
        for r in rows
    ) or "<tr><td colspan='4' class='py-3 px-3 text-center text-gray-500 text-sm'>No relevant processes found.</td></tr>"

    return (
        "<div class='bg-gray-900 rounded-xl border border-gray-800 overflow-hidden'>"
        "<table class='w-full'><thead><tr class='text-xs text-gray-500 uppercase bg-gray-800/50'>"
        "<th class='text-left p-3'>PID</th>"
        "<th class='text-left p-3'>Command</th>"
        "<th class='text-left p-3'>Uptime</th>"
        "<th class='text-right p-3'>RSS</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _health_section() -> str:
    """At-a-glance health for the four moving pieces: queue worker,
    metric-refresh worker, cookies, and the DB. Pure read — leans on
    existing data so we don't need a new heartbeats table:
      • queue worker  → most recent scrape_session started (means it ran)
                        + a 'running' row older than 1h is unhealthy
      • refresh worker → MAX(metrics_refreshed_at) (worker writes every tick)
      • cookies        → list cookie files in COOKIE_DIR + mtimes
      • DB             → round-trip latency of one trivial SELECT
    """
    from vibechecx_config import COOKIE_DIR
    import time as _t

    # --- queue worker
    qr = q(
        "SELECT MAX(started_at) AS last_start, "
        "       (SELECT MIN(started_at) FROM scrape_queue WHERE status='running') AS oldest_running "
        "  FROM scrape_sessions"
    )
    last_q = qr[0].get("last_start") if qr else None
    oldest_running = qr[0].get("oldest_running") if qr else None
    # Worker is "alive" if anything started in the last 24h. "Stuck" if
    # a 'running' row is older than 1h — likely a coordinator crash that
    # the queue worker never reconciled.
    if oldest_running:
        from datetime import datetime, timezone
        age_min = (datetime.now(timezone.utc) - oldest_running).total_seconds() / 60
        if age_min > 60:
            q_status = ("text-amber-300", f"⚠ stuck — running {int(age_min)}m")
        else:
            q_status = ("text-emerald-400", f"running for {int(age_min)}m")
    elif last_q:
        q_status = ("text-emerald-400", f"idle — last ran {rel_time(last_q)}")
    else:
        q_status = ("text-gray-500", "no scrapes recorded")

    # --- metric refresh worker
    mr = q("SELECT MAX(metrics_refreshed_at) AS m FROM tweets")
    last_mr = mr[0].get("m") if mr else None
    if last_mr:
        from datetime import datetime, timezone
        mr_age = (datetime.now(timezone.utc) - last_mr).total_seconds()
        # Worker stamps metrics_refreshed_at only when it finds work, so a
        # quiet system (everything already fresh) naturally extends the gap.
        # Don't flag yellow/red just because the worker is healthy and idle —
        # only when the gap is so long the worker has clearly died.
        if mr_age < 3600:
            mr_status = ("text-emerald-400", f"alive — {rel_time(last_mr)}")
        elif mr_age < 86400:
            mr_status = ("text-gray-400", f"idle — {rel_time(last_mr)}")
        else:
            mr_status = ("text-red-400", f"⚠ stalled — {rel_time(last_mr)}")
    else:
        mr_status = ("text-gray-500", "never run")

    # --- cookies
    cookie_rows = []
    try:
        for fname in sorted(os.listdir(COOKIE_DIR)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(COOKIE_DIR, fname)
            try:
                age_days = (_t.time() - os.path.getmtime(path)) / 86400
                size = os.path.getsize(path)
                color = "text-emerald-400" if age_days < 3 else "text-amber-300" if age_days < 7 else "text-red-400"
                cookie_rows.append((fname, color, f"{age_days:.1f}d old · {size // 1024}KB"))
            except OSError:
                continue
    except FileNotFoundError:
        pass
    cookie_inner = (
        "".join(
            f"<div class='flex justify-between text-[11px]'>"
            f"<span class='text-gray-400'>{html.escape(n)}</span>"
            f"<span class='{c}'>{s}</span></div>"
            for n, c, s in cookie_rows
        ) if cookie_rows else
        "<div class='text-[11px] text-gray-500'>no cookies found</div>"
    )

    # --- DB latency
    t0 = _t.time()
    try:
        q("SELECT 1 AS ok")
        db_ms = (_t.time() - t0) * 1000
        if db_ms < 100:
            db_status = ("text-emerald-400", f"{db_ms:.0f}ms")
        elif db_ms < 500:
            db_status = ("text-amber-300", f"{db_ms:.0f}ms")
        else:
            db_status = ("text-red-400", f"{db_ms:.0f}ms — slow")
    except Exception as e:
        db_status = ("text-red-400", f"⚠ {str(e)[:40]}")

    def _card(title: str, status: tuple, sub: str = "") -> str:
        color, label = status
        return (
            "<div class='bg-gray-900 rounded-xl border border-gray-800 p-4'>"
            f"<div class='text-[10px] uppercase tracking-wider text-gray-500'>{title}</div>"
            f"<div class='text-sm font-semibold mt-1 {color}'>{label}</div>"
            + (f"<div class='text-[10px] text-gray-500 mt-1'>{sub}</div>" if sub else "")
            + "</div>"
        )

    return (
        "<div class='grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3'>"
        + _card("Queue worker", q_status)
        + _card("Metric refresh", mr_status)
        + "<div class='bg-gray-900 rounded-xl border border-gray-800 p-4'>"
        + "<div class='text-[10px] uppercase tracking-wider text-gray-500'>Cookies</div>"
        + f"<div class='mt-2 space-y-1'>{cookie_inner}</div>"
        + "</div>"
        + _card("DB latency", db_status)
        + "</div>"
    )


def _system_section() -> str:
    try:
        import psutil
    except ImportError:
        return ""

    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    load1, load5, load15 = (os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0))

    try:
        disk = psutil.disk_usage("/")
        disk_str = f"{disk.percent:.0f}% used ({disk.used // (1024**3)} / {disk.total // (1024**3)} GB)"
    except Exception:
        disk_str = "—"

    def _bar(pct, color):
        return (
            "<div class='h-1.5 bg-gray-800 rounded overflow-hidden mt-1'>"
            f"<div class='h-full bg-{color}-400' style='width:{pct:.0f}%'></div>"
            "</div>"
        )

    return (
        "<div class='grid grid-cols-2 md:grid-cols-4 gap-3'>"

        "<div class='bg-gray-900 rounded-xl border border-gray-800 p-4'>"
        "<div class='text-[10px] uppercase tracking-wider text-gray-500'>CPU</div>"
        f"<div class='text-2xl font-semibold tabular-nums mt-1'>{cpu:.0f}%</div>"
        f"{_bar(cpu, 'cyan')}"
        "</div>"

        "<div class='bg-gray-900 rounded-xl border border-gray-800 p-4'>"
        "<div class='text-[10px] uppercase tracking-wider text-gray-500'>Memory</div>"
        f"<div class='text-2xl font-semibold tabular-nums mt-1'>{mem.percent:.0f}%</div>"
        f"<div class='text-[10px] text-gray-500'>{mem.used // (1024**2)} / {mem.total // (1024**2)} MB</div>"
        f"{_bar(mem.percent, 'emerald')}"
        "</div>"

        "<div class='bg-gray-900 rounded-xl border border-gray-800 p-4'>"
        "<div class='text-[10px] uppercase tracking-wider text-gray-500'>Load avg (1/5/15m)</div>"
        f"<div class='text-2xl font-semibold tabular-nums mt-1'>{load1:.2f}</div>"
        f"<div class='text-[10px] text-gray-500'>{load5:.2f} · {load15:.2f}</div>"
        "</div>"

        "<div class='bg-gray-900 rounded-xl border border-gray-800 p-4'>"
        "<div class='text-[10px] uppercase tracking-wider text-gray-500'>Disk /</div>"
        f"<div class='text-2xl font-semibold tabular-nums mt-1'>{disk.percent:.0f}%</div>"
        f"<div class='text-[10px] text-gray-500'>{disk.used // (1024**3)} / {disk.total // (1024**3)} GB</div>"
        f"{_bar(disk.percent, 'amber')}"
        "</div>"

        "</div>"
    )


def _db_stats_section() -> str:
    rows = q(
        """
        SELECT
          (SELECT COUNT(*) FROM tweets) AS tweets,
          (SELECT COUNT(*) FROM accounts) AS accounts,
          (SELECT COUNT(*) FROM cohorts) AS cohorts,
          (SELECT COUNT(*) FROM scrape_sessions) AS sessions,
          (SELECT COUNT(*) FROM scrape_sessions WHERE started_at > NOW() - INTERVAL '24 hours') AS sessions_24h,
          (SELECT COUNT(*) FROM insights_cache) AS insights_cached,
          (SELECT COUNT(*) FROM users) AS users_total
        """
    )
    s = rows[0] if rows else {}

    def _stat(label, value):
        return (
            "<div class='bg-gray-900 rounded-xl border border-gray-800 p-3'>"
            f"<div class='text-[10px] uppercase tracking-wider text-gray-500'>{html.escape(label)}</div>"
            f"<div class='text-xl font-semibold tabular-nums mt-1'>{value:,}</div>"
            "</div>"
        )

    return (
        "<div class='grid grid-cols-3 md:grid-cols-6 gap-2'>"
        + _stat("Users", s.get("users_total", 0))
        + _stat("Tweets", s.get("tweets", 0))
        + _stat("Accounts", s.get("accounts", 0))
        + _stat("Cohorts", s.get("cohorts", 0))
        + _stat("Sessions (24h)", s.get("sessions_24h", 0))
        + _stat("Insights cached", s.get("insights_cached", 0))
        + "</div>"
    )


@router.get("/admin", response_class=HTMLResponse)
def admin_page(r: Request):
    redir, user = _require_admin(r)
    if redir:
        return redir

    # Users
    users = q(
        """
        SELECT u.id, u.username, u.is_admin, u.created_at,
               MAX(ss.started_at) AS last_scrape
          FROM users u
          LEFT JOIN scrape_sessions ss ON ss.user_id = u.id
         GROUP BY u.id ORDER BY u.id
        """
    )

    def _user_row(u):
        admin_badge = (
            '<span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-900/50 text-purple-300 ml-1">admin</span>'
            if u.get("is_admin") else ""
        )
        last = rel_time(u["last_scrape"]) if u.get("last_scrape") else "—"
        joined = u["created_at"].strftime("%Y-%m-%d") if u.get("created_at") else "?"
        return (
            "<tr class='border-b border-gray-800 text-sm'>"
            f"<td class='py-2 px-3 text-gray-400'>#{u['id']}</td>"
            f"<td class='py-2 px-3 font-medium'>{html.escape(u['username'])}{admin_badge}</td>"
            f"<td class='py-2 px-3 text-gray-400'>{joined}</td>"
            f"<td class='py-2 px-3 text-gray-400'>{last}</td>"
            "</tr>"
        )

    user_rows = "".join(_user_row(u) for u in users)

    # Queue
    queue = q(
        """
        SELECT q.id, q.status, q.position, q.days, q.created_at, q.started_at,
               u.username, p.target_handle, p.cohort_id
          FROM scrape_queue q
          JOIN users u ON u.id = q.user_id
          JOIN profiles p ON p.id = q.profile_id
         WHERE q.status IN ('pending', 'running')
         ORDER BY q.position
        """
    )

    def _queue_row(row):
        status_cls = (
            "text-cyan-300" if row["status"] == "running" else "text-yellow-300"
        )
        scope = html.escape(row.get("target_handle") or f'cohort #{row.get("cohort_id")}' or "?")
        when = rel_time(row["created_at"]) if row.get("created_at") else "?"
        return (
            "<tr class='border-b border-gray-800 text-sm'>"
            f"<td class='py-2 px-3 text-gray-400'>#{row['position']}</td>"
            f"<td class='py-2 px-3'><span class='{status_cls}'>{row['status']}</span></td>"
            f"<td class='py-2 px-3'>{html.escape(row['username'])}</td>"
            f"<td class='py-2 px-3 text-gray-300'>{scope}</td>"
            f"<td class='py-2 px-3 text-gray-500'>{row['days']}d</td>"
            f"<td class='py-2 px-3 text-gray-500'>{when}</td>"
            "</tr>"
        )

    depth = queue_depth()
    queue_rows = "".join(_queue_row(rr) for rr in queue) or (
        "<tr><td colspan='6' class='py-4 text-center text-gray-500 text-sm'>Queue is empty.</td></tr>"
    )
    queue_summary = f"{depth['running']} running · {depth['waiting']} waiting"

    # AI providers
    provider_rows = (
        _provider_row("DeepSeek", deepseek_api_key(), "VIBECHECX_DEEPSEEK_MODEL", "deepseek-reasoner")
        + _provider_row("OpenAI",  openai_api_key(),  "VIBECHECX_OPENAI_MODEL",   "gpt-4o-mini")
        + _provider_row("Grok",    xai_api_key(),     "VIBECHECX_GROK_MODEL",     "grok-4.20-0309")
    )

    # Primary provider toggle — read from DB (persists across Render redeploys)
    try:
        current_primary = (q("SELECT value FROM app_settings WHERE key='primary_provider'") or [{}])[0].get("value", "deepseek").strip().lower()
    except Exception:
        current_primary = "deepseek"
    if current_primary not in ("deepseek", "openai", "grok"):
        current_primary = "deepseek"

    def _radio(value, label):
        checked = "checked" if value == current_primary else ""
        return (
            f'<label class="cursor-pointer">'
            f'<input type="radio" name="primary" value="{value}" {checked} class="sr-only peer">'
            f'<span class="px-3 py-1.5 text-xs rounded border border-gray-700 text-gray-400 '
            f'peer-checked:border-purple-500 peer-checked:text-purple-200 peer-checked:bg-purple-900/30 '
            f'hover:border-gray-500 transition select-none inline-block">{label}</span>'
            f'</label>'
        )

    toggle_form = (
        "<form method='post' action='/admin/primary-provider' "
        "class='flex items-center gap-3 bg-gray-900 rounded-xl border border-gray-800 p-4 mb-8'>"
        "<span class='text-xs text-gray-500 uppercase tracking-wider'>Primary for insights:</span>"
        + _radio("deepseek", "DeepSeek")
        + _radio("openai",   "OpenAI")
        + _radio("grok",     "Grok")
        + "<button type='submit' class='ml-auto text-xs px-3 py-1.5 rounded bg-purple-700 hover:bg-purple-600 text-white transition'>Save</button>"
        + "<span class='text-[10px] text-gray-500'>takes effect on next generation</span>"
        + "</form>"
    )

    return header_html(0, active_profile_name(r), is_admin=True) + (
        "<div class='flex items-center justify-between mb-6'>"
        "<h1 class='text-xl font-semibold'>Admin</h1>"
        f"<span class='text-xs text-gray-500'>{len(users)} users</span>"
        "</div>"

        "<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>Health</h2>"
        f"<div class='mb-6'>{_health_section()}</div>"

        "<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>System</h2>"
        f"<div class='mb-6'>{_system_section()}</div>"

        "<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>Database</h2>"
        f"<div class='mb-6'>{_db_stats_section()}</div>"

        "<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>AI Providers "
        "<span class='text-gray-400 font-normal normal-case'>"
        f", current primary: {current_primary}"
        "</span></h2>"
        f"{toggle_form}"
        "<div class='bg-gray-900 rounded-xl border border-gray-800 overflow-hidden mb-8'>"
        "<table class='w-full'><thead><tr class='text-xs text-gray-500 uppercase bg-gray-800/50'>"
        "<th class='text-left p-3'>Provider</th><th class='text-left p-3'>Key</th>"
        "<th class='text-left p-3'>Model</th>"
        f"</tr></thead><tbody>{provider_rows}</tbody></table></div>"

        "<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>Processes</h2>"
        f"<div class='mb-8'>{_process_section()}</div>"

        "<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>Users</h2>"
        "<div class='bg-gray-900 rounded-xl border border-gray-800 overflow-hidden mb-8'>"
        "<table class='w-full'><thead><tr class='text-xs text-gray-500 uppercase bg-gray-800/50'>"
        "<th class='text-left p-3'>ID</th><th class='text-left p-3'>Username</th>"
        "<th class='text-left p-3'>Joined</th><th class='text-left p-3'>Last scrape</th>"
        f"</tr></thead><tbody>{user_rows}</tbody></table></div>"

        f"<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>Scrape Queue "
        f"<span class='text-gray-400 font-normal normal-case'>, {queue_summary}</span></h2>"
        "<div class='bg-gray-900 rounded-xl border border-gray-800 overflow-hidden mb-8'>"
        "<table class='w-full'><thead><tr class='text-xs text-gray-500 uppercase bg-gray-800/50'>"
        "<th class='text-left p-3'>#</th><th class='text-left p-3'>Status</th>"
        "<th class='text-left p-3'>User</th><th class='text-left p-3'>Scope</th>"
        "<th class='text-left p-3'>Days</th><th class='text-left p-3'>Queued</th>"
        f"</tr></thead><tbody>{queue_rows}</tbody></table></div>"

        "<h2 class='text-xs font-semibold text-gray-500 uppercase mb-3'>Insights Library</h2>"
        "<div class='bg-gray-900 rounded-xl border border-gray-800 overflow-hidden'>"
        "<div hx-get='/admin/insights-library' hx-trigger='load' hx-swap='innerHTML'>"
        "<p class='text-center text-gray-500 text-sm py-4'>Loading…</p>"
        "</div></div>"
    ) + HF


@router.get("/admin/insights-library", response_class=HTMLResponse)
def admin_insights_library(r: Request):
    """HTMX fragment: all users' cached insights (admin only)."""
    redir, _user = _require_admin(r)
    if redir:
        return redir

    rows = q(
        """
        SELECT ic.id, ic.display_name, ic.scope_type, ic.scope_id,
               ic.period, ic.provider, ic.generated_at, u.username,
               c.name      AS cohort_name,
               a.username  AS account_username
          FROM insights_cache ic
          LEFT JOIN users    u ON u.id = ic.user_id
          LEFT JOIN cohorts  c ON ic.scope_type = 'cohort'  AND c.id = ic.scope_id
          LEFT JOIN accounts a ON ic.scope_type = 'account' AND a.id = ic.scope_id
         ORDER BY ic.generated_at DESC
         LIMIT 200
        """
    )
    if not rows:
        return "<p class='text-gray-500 text-xs text-center py-4'>No insights cached yet.</p>"

    import re as _re
    def _row(row):
        raw = row.get("display_name") or (
            row.get("cohort_name") if row["scope_type"] == "cohort"
            else (f"@{row['account_username']}" if row.get("account_username") else None)
        )
        clean = _re.sub(r"\s*[·•|]\s*\d+[dhw]\s*$", "", raw).strip() if raw else f"{row['scope_type']} {row['scope_id']}"
        name = html.escape(clean)
        owner = html.escape(row.get("username") or "—")
        gen_at = row.get("generated_at")
        age = rel_time(gen_at) if gen_at else "?"
        date_str = gen_at.strftime("%Y-%m-%d") if hasattr(gen_at, "strftime") else "—"
        provider = html.escape((row.get("provider") or "")[:20])
        dl_url = f"/insights/library/download/{row['id']}?format=md"
        dl_json_url = f"/insights/library/download/{row['id']}?format=json"
        return (
            "<tr class='border-b border-gray-800 text-sm'>"
            f"<td class='py-2 px-3 text-gray-400'>{row['scope_type']}</td>"
            f"<td class='py-2 px-3 font-medium text-emerald-300'>{name}</td>"
            f"<td class='py-2 px-3 text-gray-400'>{row['period']}</td>"
            f"<td class='py-2 px-3 text-gray-400'>{owner}</td>"
            f"<td class='py-2 px-3 text-gray-500' title='{age}'>{date_str}</td>"
            f"<td class='py-2 px-3 text-gray-500 text-xs'>{provider}</td>"
            f"<td class='py-2 px-3'>"
            f"<a href='{dl_url}' class='text-emerald-400 hover:underline text-xs mr-2'>↓ MD</a>"
            f"<a href='{dl_json_url}' class='text-blue-400 hover:underline text-xs'>↓ JSON</a>"
            f"</td></tr>"
        )

    rows_html = "".join(_row(rr) for rr in rows)
    return (
        "<table class='w-full text-sm'>"
        "<thead><tr class='text-[11px] text-gray-500 uppercase bg-gray-800/50 border-b border-gray-800'>"
        "<th class='text-left p-3'>Type</th><th class='text-left p-3'>Name</th>"
        "<th class='text-left p-3'>Period</th><th class='text-left p-3'>User</th>"
        "<th class='text-left p-3'>Generated</th><th class='text-left p-3'>Provider</th>"
        "<th class='text-left p-3'>Download</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
    )


@router.post("/admin/primary-provider")
async def set_primary_provider(r: Request):
    redir, _user = _require_admin(r)
    if redir:
        return redir
    form = await r.form()
    choice = (form.get("primary") or "").strip().lower()
    if choice not in ("deepseek", "openai", "grok"):
        return RedirectResponse("/admin", status_code=303)
    q(
        "INSERT INTO app_settings(key, value, updated_at) VALUES ('primary_provider', %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
        (choice,),
    )
    return RedirectResponse("/admin", status_code=303)
