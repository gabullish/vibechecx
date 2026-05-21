"""VibeChecx Precision — per-profile data-quality signal.

Replaces the previous fake 97.5% baseline. Components are computed against the
**active profile's scope** (single account or cohort), not a hardcoded handle.
Each component is honest: missing data shows as "n/a", never as a fake number.

Components (each 0-1, weighted):
  - freshness:  hours since last completed scrape for this scope
  - coverage:   tweets/day collected over last 14d vs. account's historical rate
  - patrol:     fraction of stored tweets whose metrics were refreshed in last 7d
  - accuracy:   CSV-validation agreement rate, if a validation CSV exists
"""
import os
import sys
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG, VALIDATION_DIR
from web.ui import scrape_depth_picker_html

WEIGHTS = {"freshness": 0.35, "coverage": 0.25, "patrol": 0.20, "accuracy": 0.20}


def _conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def _scope_account_ids(prof):
    """Return list of account_ids for the active profile, or [] if unresolvable."""
    if not prof:
        return []
    with _conn() as conn, conn.cursor() as cur:
        if prof.get("cohort_id"):
            cur.execute(
                "SELECT account_id FROM cohort_members WHERE cohort_id=%s",
                (prof["cohort_id"],),
            )
            return [r["account_id"] for r in cur.fetchall()]
        if prof.get("target_handle"):
            cur.execute(
                "SELECT id FROM accounts WHERE username=%s",
                (prof["target_handle"],),
            )
            r = cur.fetchone()
            return [r["id"]] if r else []
    return []


def _freshness(prof, account_ids):
    """1.0 if scraped <6h ago, decays linearly to 0 over 7 days. None if never."""
    if not account_ids:
        return None, "never scraped"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(ended_at) AS t
              FROM scrape_sessions
             WHERE status = 'completed'
               AND (target_account_id = ANY(%s) OR cohort_id = %s)
            """,
            (account_ids, prof.get("cohort_id")),
        )
        row = cur.fetchone()
    if not row or not row["t"]:
        return None, "never scraped"
    t = row["t"]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    if hours < 6:
        score = 1.0
    elif hours >= 7 * 24:
        score = 0.0
    else:
        score = max(0.0, 1.0 - (hours - 6) / (7 * 24 - 6))
    if hours < 1:
        label = f"{int(hours*60)}m ago"
    elif hours < 24:
        label = f"{int(hours)}h ago"
    else:
        label = f"{int(hours/24)}d ago"
    return score, label


def _coverage(account_ids):
    """tweets-per-day in last 14d vs the account's all-time tweets/day rate."""
    if not account_ids:
        return None, "no data"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '14 days') AS recent,
              COUNT(*) AS total,
              MIN(created_at) AS first_seen,
              MAX(created_at) AS last_seen
            FROM tweets
            WHERE author_account_id = ANY(%s)
              AND is_retweet = FALSE
            """,
            (account_ids,),
        )
        row = cur.fetchone()
    if not row or not row["total"] or not row["first_seen"]:
        return None, "no tweets"
    recent_per_day = row["recent"] / 14.0
    first = row["first_seen"]
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    span_days = max(1.0, (datetime.now(timezone.utc) - first).total_seconds() / 86400)
    historical_per_day = row["total"] / span_days
    if historical_per_day <= 0:
        return None, "no baseline"
    score = min(1.0, recent_per_day / historical_per_day)
    return score, f"{recent_per_day:.1f}/d vs {historical_per_day:.1f}/d"


def _patrol(account_ids):
    """Fraction of last-14d tweets whose metrics were re-observed in last 7d."""
    if not account_ids:
        return None, "no data"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (
                WHERE last_measured_at >= NOW() - INTERVAL '7 days'
              ) AS fresh
            FROM tweets
            WHERE author_account_id = ANY(%s)
              AND is_retweet = FALSE
              AND created_at >= NOW() - INTERVAL '14 days'
            """,
            (account_ids,),
        )
        row = cur.fetchone()
    if not row or not row["total"]:
        return None, "no recent tweets"
    score = row["fresh"] / row["total"]
    return score, f"{row['fresh']}/{row['total']} refreshed"


def _accuracy(prof):
    """CSV-validation agreement rate, if validation/{handle}.csv exists."""
    handle = (prof or {}).get("target_handle") or ""
    if not handle:
        return None, "no CSV"
    path = os.path.join(VALIDATION_DIR, f"{handle}.csv")
    if not os.path.exists(path):
        return None, "no CSV"
    try:
        import csv
        rows = []
        with open(path) as f:
            for r in csv.DictReader(f):
                rows.append(r)
        if not rows:
            return None, "empty CSV"
    except Exception:
        return None, "CSV unreadable"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tweet_id, likes, views FROM tweets WHERE author_account_id = "
            "(SELECT id FROM accounts WHERE username=%s)",
            (handle,),
        )
        db = {r["tweet_id"]: r for r in cur.fetchall()}
    matches = 0
    compared = 0
    for r in rows:
        tid = r.get("tweet_id") or r.get("Tweet ID") or ""
        if not tid or tid not in db:
            continue
        compared += 1
        try:
            csv_likes = int(r.get("likes") or r.get("Likes") or 0)
            db_likes = db[tid]["likes"] or 0
            if abs(csv_likes - db_likes) <= max(2, int(csv_likes * 0.05)):
                matches += 1
        except (ValueError, TypeError):
            pass
    if not compared:
        return None, "no overlap"
    score = matches / compared
    return score, f"{matches}/{compared} agree"


def compute(prof):
    """Return dict of component scores + overall + labels for the active profile."""
    account_ids = _scope_account_ids(prof)
    f_s, f_l = _freshness(prof, account_ids)
    c_s, c_l = _coverage(account_ids)
    p_s, p_l = _patrol(account_ids)
    a_s, a_l = _accuracy(prof)
    components = {
        "freshness": {"score": f_s, "label": f_l},
        "coverage": {"score": c_s, "label": c_l},
        "patrol": {"score": p_s, "label": p_l},
        "accuracy": {"score": a_s, "label": a_l},
    }
    weighted_sum = 0.0
    weight_total = 0.0
    for name, w in WEIGHTS.items():
        s = components[name]["score"]
        if s is not None:
            weighted_sum += s * w
            weight_total += w
    overall = (weighted_sum / weight_total) if weight_total > 0 else None
    return {"components": components, "overall": overall, "account_count": len(account_ids)}


def precision_badge_html(prof):
    """Compact per-profile precision badge. Honest when there's no data."""
    if not prof:
        return ""
    res = compute(prof)
    overall = res["overall"]
    comps = res["components"]
    if overall is None:
        return (
            '<div class="text-xs bg-gray-900 border border-gray-800 rounded-lg px-3 py-2 text-gray-500">'
            '<span class="mr-2">No data yet —</span>'
            + scrape_depth_picker_html(hx_target="#trigger-status-nd",
                                       submit_label="run a scrape", compact=True)
            + '<span id="trigger-status-nd"></span>'
            '</div>'
        )
    pct = int(round(overall * 100))
    if overall >= 0.85:
        tone = "text-emerald-300 bg-emerald-900/40 border-emerald-700"
    elif overall >= 0.65:
        tone = "text-green-300 bg-green-900/40 border-green-700"
    elif overall >= 0.4:
        tone = "text-yellow-300 bg-yellow-900/40 border-yellow-700"
    else:
        tone = "text-red-300 bg-red-900/40 border-red-700"

    def _row(name, comp):
        s, lbl = comp["score"], comp["label"]
        if s is None:
            v = '<span class="text-gray-500">n/a</span>'
        else:
            v = f'<span class="text-gray-300">{s:.2f}</span>'
        return f'<div class="flex justify-between gap-3 text-[11px]"><span class="text-gray-500 capitalize">{name}</span><span>{v} <span class="text-gray-400">{lbl}</span></span></div>'

    detail = "".join(_row(n, comps[n]) for n in ("freshness", "coverage", "patrol", "accuracy"))
    return (
        f'<div class="flex items-center gap-3 text-xs rounded-lg border px-3 py-2 {tone}" '
        f'x-data="{{open:false}}" @mouseenter="open=true" @mouseleave="open=false">'
        f'<span class="font-semibold">Precision {pct}%</span>'
        f'<div class="w-16 bg-gray-800/60 rounded-full h-1.5"><div class="h-1.5 rounded-full bg-current" style="width:{pct}%"></div></div>'
        + scrape_depth_picker_html(hx_target="#trigger-status-pb",
                                    submit_label="↻ scrape", compact=True)
        + '<span id="trigger-status-pb"></span>'
        f'<div x-cloak x-show="open" class="absolute mt-2 ml-0 bg-gray-900 border border-gray-700 rounded-lg p-2 shadow-xl z-20 w-60 space-y-0.5" style="margin-top:60px">'
        f'{detail}'
        f'</div>'
        f'</div>'
    )


# Back-compat shim: some templates may still call the old no-arg API.
def precision_pill_html(prof=None):
    if not prof:
        return ""
    res = compute(prof)
    overall = res["overall"]
    if overall is None:
        return '<span class="text-xs text-gray-500">precision: no data</span>'
    pct = int(round(overall * 100))
    dot = "bg-emerald-400" if overall >= 0.85 else (
        "bg-green-400" if overall >= 0.65 else (
            "bg-yellow-400" if overall >= 0.4 else "bg-red-400"))
    return f'<span class="text-xs text-gray-500"><span class="inline-block w-1.5 h-1.5 rounded-full {dot} mr-1"></span>{pct}%</span>'
