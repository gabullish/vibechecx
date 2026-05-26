"""Shared configuration for VibeChecx.

Single source of truth for DB credentials, API keys, and paths. Reads from
environment variables with localhost defaults so dev works without setup.
"""
import os
import socket
import logging

_log = logging.getLogger("vibechecx.config")


def _env(name, default):
    v = os.environ.get(name)
    return v if v else default


def _resolve_supabase_host(host, port, user):
    """Auto-resolve Supabase IPv6-only hosts to the IPv4-compatible pooler.

    Render's free tier can't make outbound IPv6 connections. Supabase's
    direct connection hostnames (db.XXXXX.supabase.co) resolve only to
    IPv6. The Supavisor pooler (aws-0-{region}.pooler.supabase.com)
    has IPv4 and works from Render.

    Returns (host, port, user) — unchanged if host already has IPv4 or
    isn't a Supabase host.
    """
    if not host or ".supabase.co" not in host:
        return host, port, user

    # Already has IPv4? Use as-is.
    try:
        socket.getaddrinfo(host, port, socket.AF_INET)
        return host, port, user
    except socket.gaierror:
        pass

    # Extract project ref from hostname like "db.abcdef.supabase.co"
    # Parts: ["db", "PROJECT_REF", "supabase", "co"]
    parts = host.split(".")
    project_ref = parts[1] if len(parts) >= 4 and parts[0] == "db" else parts[0]

    # Determine region from the project host or default to us-east-1
    # The pooler format is aws-0-{region}.pooler.supabase.com
    pooler_host = f"aws-0-us-east-1.pooler.supabase.com"
    pooler_port = 6543  # Transaction mode pooler
    pooler_user = f"{user}.{project_ref}"

    _log.info(
        "Supabase host %s is IPv6-only; switching to pooler %s:%d as user %s",
        host, pooler_host, pooler_port, pooler_user,
    )
    return pooler_host, pooler_port, pooler_user


DB_CONFIG = {
    "host": _env("VIBECHECX_DB_HOST", "localhost"),
    "port": int(_env("VIBECHECX_DB_PORT", "5432")),
    "dbname": _env("VIBECHECX_DB_NAME", "vibechecx"),
    "user": _env("VIBECHECX_DB_USER", "vibechecx"),
    "password": _env("VIBECHECX_DB_PASSWORD", "vibechecx_pass"),
}
# Auto-resolve IPv6-only Supabase hosts to the IPv4 pooler.
DB_CONFIG["host"], DB_CONFIG["port"], DB_CONFIG["user"] = _resolve_supabase_host(
    DB_CONFIG["host"], DB_CONFIG["port"], DB_CONFIG["user"],
)


SUPABASE_DB_CONFIG: dict | None = None
_supabase_host = _env("SUPABASE_DB_HOST", "")
_supabase_password = _env("SUPABASE_DB_PASSWORD", "")
# Hard kill-switch: VIBECHECX_DISABLE_SUPABASE=1 turns the dual-write off
# regardless of any other env. Defaults to enabled — but only attempts
# the connection when BOTH host and password are non-empty. Previously
# just the host was checked, so empty/expired credentials in
# /etc/environment would slip through and fill every coordinator log
# with 'Tenant or user not found' warnings on every query.
_supabase_disabled = _env("VIBECHECX_DISABLE_SUPABASE", "").lower() in ("1", "true", "yes")
if _supabase_host and _supabase_password and not _supabase_disabled:
    _supabase_port = int(_env("SUPABASE_DB_PORT", "5432"))
    _supabase_user = _env("SUPABASE_DB_USER", "postgres")
    _supabase_host, _supabase_port, _supabase_user = _resolve_supabase_host(
        _supabase_host, _supabase_port, _supabase_user,
    )
    SUPABASE_DB_CONFIG = {
        "host": _supabase_host,
        "port": _supabase_port,
        "dbname": _env("SUPABASE_DB_NAME", "postgres"),
        "user": _supabase_user,
        "password": _supabase_password,
    }


def db_dsn():
    c = DB_CONFIG
    return f"host={c['host']} port={c['port']} dbname={c['dbname']} user={c['user']} password={c['password']}"


def deepseek_api_key():
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k:
        return k
    p = os.path.expanduser("~/.openclaw/credentials/deepseek.key")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return ""


def xai_api_key():
    """Grok / xAI API key, used as enrichment fallback when DeepSeek is down."""
    for var in ("XAI_API_KEY", "GROK_API_KEY"):
        k = os.environ.get(var, "").strip()
        if k:
            return k
    for path in ("~/.openclaw/credentials/xai.key", "~/.openclaw/credentials/grok.key"):
        p = os.path.expanduser(path)
        if os.path.exists(p):
            with open(p) as f:
                return f.read().strip()
    return ""


def openai_api_key():
    """OpenAI API key, used as a third fallback for insights when both DeepSeek and Grok fail."""
    k = os.environ.get("OPENAI_API_KEY", "").strip()
    if k:
        return k
    p = os.path.expanduser("~/.openclaw/credentials/openai.key")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return ""


CUTOFF_DAYS_DEFAULT = int(_env("VIBECHECX_CUTOFF_DAYS", "30"))
REGISTRATION_OPEN = _env("VIBECHECX_REGISTRATION_OPEN", "true").lower() == "true"
SCRAPER_HEADFUL = _env("VIBECHECX_SCRAPER_HEADFUL", "true").lower() == "true"


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.join(REPO_ROOT, "collector")
COOKIE_DIR = os.path.join(REPO_ROOT, "cookies")
RAW_DIR = os.path.join(REPO_ROOT, "raw")
VALIDATION_DIR = os.path.join(REPO_ROOT, "validation")
