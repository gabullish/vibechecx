"""Shared configuration for VibeChecx.

Single source of truth for DB credentials, API keys, and paths. Reads from
environment variables with localhost defaults so dev works without setup.
"""
import os


def _env(name, default):
    v = os.environ.get(name)
    return v if v else default


DB_CONFIG = {
    "host": _env("VIBECHECX_DB_HOST", "localhost"),
    "port": int(_env("VIBECHECX_DB_PORT", "5432")),
    "dbname": _env("VIBECHECX_DB_NAME", "vibechecx"),
    "user": _env("VIBECHECX_DB_USER", "vibechecx"),
    "password": _env("VIBECHECX_DB_PASSWORD", "vibechecx_pass"),
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
