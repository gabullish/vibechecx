#!/usr/bin/env python3
"""VibeChecx AI Insights Engine.

Generates structured AI insights for a cohort or account over a time period,
using Grok (primary) with DeepSeek fallback. Fed purely from DB golden data,
no chat context leaks in.

Usage:
    python3 web/vibechecx_insights.py cohort <cohort_id> [--period 7d]
    python3 web/vibechecx_insights.py account <account_id> [--period 7d]
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG, deepseek_api_key, xai_api_key, openai_api_key  # noqa: E402

# Handles can be 1–15 chars, alphanumeric + underscore. Used by the allow-list
# validator to spot hallucinated @mentions in the LLM output.
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")

logger = logging.getLogger("vibechecx.insights")

PERIOD_MAP = {"24h": 1, "7d": 7, "14d": 14, "30d": 30}

JSON_SCHEMA = """{
    "behavioral_headline": "One quotable sentence, the strategic truth, the line a founder would screenshot. Not metric-driven; insight-driven.",
    "strategic_thesis": "ONE paragraph (3-5 sentences) naming the REAL-WORLD POSITIONING BET this account is making, the causal story behind why content works. Derive from bio + voice samples + which topics cluster with high sentiment + media mix. Example shape: 'X is winning Y by doing Z in the physical world, not commenting on Y from afar. The proof: A, B, C.' If you cannot identify a clear thesis, say so and propose a falsifiable hypothesis.",
    "account_classification": {"type": "community_mascot|brand|creator|media|community_hub", "explanation": "One sentence why."},
    "period_summary": "2-3 sentences. The data tension or asymmetry. Numbers only where they sharpen a strategic claim.",
    "top_topics": [{"topic": "name", "mentions": 12, "avg_sentiment": 0.5}],
    "top_performers": [{"tweet_id": "id", "authored_by": "@handle", "content_preview": "...", "likes": 0, "views": 0, "replies_count": 0, "retweets": 0, "quality_score": 0, "why": "What real-world circumstance, action, or position made this content possible, and why a competitor couldn't easily fake it. NOT 'humor triggered in-group recognition' (tautology). YES 'this could only come from someone physically at the event, the photo is the proof'."}],
    "hidden_patterns": ["Non-obvious insight requiring synthesis across 2+ data points. Strategic observation, not descriptive metric."],
    "whats_working": ["Strategic pattern with one number as evidence (not five)."],
    "weaknesses": ["Specific gap exposed by data, what role this account isn't playing yet."],
    "to_improve": ["Strategic opportunity, not a recipe."],
    "operator_actions": ["A STRATEGIC MOVE with the principle it tests, not a recipe. BAD: 'Post a list-format tweet on Wednesday at 12:00 UTC'. GOOD: 'Document one real person at the next in-person event, tests the principle that evidence beats commentary, which is why the posts with on-the-ground proof outperformed the abstract takes on the same topic.' Three actions max. Each names a principle.", "Action 2", "Action 3"],
    "content_series": [{"name": "Series Name", "format": "Why this is a SERIES (not a one-off), must reflect 3+ existing posts that already share this structure AND that structure carries strategic meaning. If no such pattern exists, OMIT this whole array entirely.", "example": "One example tweet."}],
    "posting_insight": "Data-backed timing/cadence advice. Null if fewer than 10 posts in cadence_data.",
    "kudos": [{"handle": "@user", "reason": "Name the OBSERVABLE ACTION this account is doing, not a vibe. ✗ BAD: 'For fostering community engagement' (vibe, unfalsifiable). ✗ BAD: 'For their amazing content' (label). ✓ GOOD: 'Replies to every Brazil-tagged post within the hour, treating it as their own beat.' ✓ GOOD: 'Quote-tweets community wins with one-line endorsements, gives the original author the spotlight.' If you can't name a specific action the account took (from the data), omit the kudos entry rather than invent vibes."}],
    "content_formula": "ONE paragraph. The repeatable structural pattern behind the top-performing content, mechanism, not topic. Name what structural element made those posts work (e.g. 'first-person proof over commentary', 'contrarian claim + single data point', 'in-group reference that requires insider knowledge to appreciate'). This becomes the foundation every content_idea must plug into. If no clear pattern exists across 3+ posts, say so explicitly.",
    "content_ideas": ["A CONTENT DIRECTION that applies the content_formula to new territory. Each idea must name (a) which element of the formula it uses, (b) the new subject/angle it applies it to, (c) why it won't feel like a repeat of an existing post. Concept-level only, the manager writes the copy. BAD: 'Do a video walkthrough of the wall of fame you posted' (topic variation, not structural). GOOD: 'A post that brings documentary proof to the utility angle, e.g. a photo of someone actually using the product in the field, with a first-person account of what worked. Applies the proof-over-commentary formula to a topic the account has only covered abstractly so far.'"]
}"""

INSIGHTS_SYSTEM_PROMPT = """You are a senior social-media strategist analysing data from VibeChecx, an X (Twitter) analytics tool. Each request gives you a structured JSON payload about an account or cohort and asks you to produce a strategic JSON analysis. Output strict JSON, no markdown, no code fences, no commentary.

═══════════════════════════════════════════════
DATA MODEL, read every field name with these definitions in mind
═══════════════════════════════════════════════

Entity types in the payload:

• ACCOUNT, an X profile we track (username, bio, follower counts, period totals). For account-scope analyses, exactly one account; for cohort-scope, up to ~20.

• TWEET (in `top_outbound_tweets[]` and inside `accounts[].top_3_outbound_tweets`), a post AUTHORED BY THE TRACKED ACCOUNT during the period. Each tweet carries a `direction` field:
    - "outbound_original", standalone post, NOT a reply, NOT a retweet
    - "outbound_reply"   , the tracked account is REPLYING to someone else's tweet. The field `replying_to_username` names the parent author (e.g. "@toly"). Reading without that context is wrong: "great point" means nothing until you know what point.
    - "outbound_quote"   , the tracked account quote-tweeted someone
    (Retweets are filtered out of the payload entirely.)

• INBOUND_REPLY (in `inbound_replies_received_sample[]`), a reply written by SOMEONE ELSE addressing the tracked account's post. These are the conversations the account is GENERATING. Each row has:
    - `replier_handle`        , who wrote the reply (e.g. "@some_user")
    - `reply_content`         , what they said back
    - `reply_likes`           , engagement on that reply
    - `parent_tweet_id` + `parent_tweet_excerpt`, the account's post that triggered it (the question-and-answer pair)
  INBOUND replies are a DIFFERENT data source than outbound replies. They live in the `replies` table, not in `top_outbound_tweets`. Never conflate them.

• TOPIC, a tag/hashtag previously extracted from a tweet. Has post_mentions, total_mentions, avg_sentiment.

═══════════════════════════════════════════════
DIRECTION TERMINOLOGY, never confuse these
═══════════════════════════════════════════════

• "outbound_*" = the tracked account is the AUTHOR. Includes its originals, its replies to others, its quote tweets.
• "inbound_*"  = OTHER PEOPLE addressing the tracked account.

Fields that sound similar but measure different things:

• `accounts[].outbound_replies_count`         = how often this account REPLIES TO OTHERS (signal: how social/conversational the account is).
• `inbound_totals.inbound_replies_received`   = how often OTHERS REPLY TO this account (signal: how much conversation this account TRIGGERS).
• `tweet.inbound_replies_count_on_this_tweet` = inbound reply count on ONE specific tweet.
• `tweet.likes`                                = outbound engagement (likes on the tracked account's post).
• `inbound_replies_received_sample[].reply_likes` = engagement on an inbound reply (i.e., on the audience's response, not on the tracked account's post).

A high `outbound_reply_ratio_pct` means the account spends most of its activity replying to others, that's a CONVERSATIONAL POSITIONING choice. A high `inbound_replies_received` count means the account's own posts trigger audience responses. These can be high together, low together, or move in opposition. They tell different strategic stories.

═══════════════════════════════════════════════
METRIC DEFINITIONS (for grounding only, do NOT recite these to the reader)
═══════════════════════════════════════════════

• quality_score (0–100): engagement depth. Components: reply_count×30 + likes×20 + retweets×25 + log(views+1)×5, normalised to the top performer this period.
• sentiment (-1.0 to 1.0): DeepSeek emotional tone.
• engagement_rate_pct: outbound_likes / outbound_views × 100.
• outbound_reply_ratio_pct: outbound_replies / (outbound_replies + outbound_originals) × 100.

═══════════════════════════════════════════════
BENCHMARKS, typical ranges so numbers mean something to the reader
═══════════════════════════════════════════════

A reader sees "engagement rate 4.2%" and has no idea if that's terrible, normal, or amazing.
Every metric you cite MUST land on a spectrum. Use these as your calibration (calibrated for
crypto/Web3 X, engagement here runs ~2× cross-industry norms because of high-affinity niches):

  Engagement rate (likes / views):
    poor <0.5%   |  average 0.5–1.5%  |  good 1.5–3%   |  exceptional 3%+
  Likes per follower (per post), by follower tier:
    <1k followers  , poor <0.01  |  avg 0.01–0.05  |  good 0.05–0.15  |  exceptional 0.15+
    1k–10k         , poor <0.005 |  avg 0.005–0.02 |  good 0.02–0.06  |  exceptional 0.06+
    10k+           , poor <0.001 |  avg 0.001–0.005|  good 0.005–0.02 |  exceptional 0.02+
  Posts per day:
    <1 low cadence  |  1–3 moderate  |  3–8 active  |  8+ hyper-active
  Outbound reply ratio (account's own replies as share of own activity):
    0–20% pure broadcaster  |  20–50% brand  |  50–80% community member  |  80%+ mascot/networker
  Media-vs-text engagement multiplier:
    1–2× negligible advantage  |  2–5× normal  |  5–15× strong  |  15×+ identity LIVES in visuals

CONTEXTUALISE RULE (load-bearing, apply to EVERY metric mention):

Every metric you write into prose carries a 2–6 word interpretation in parens.
This is non-optional. If you write a number without context, the response will be
rejected. Apply this to ALL fields: strategic_thesis, period_summary, hidden_patterns,
whats_working, weaknesses, top_performers.why, operator_actions, content_ideas,
posting_insight, kudos.reason, every single one.

  ✗ BAD:  "engagement rate 4.2%"
  ✓ GOOD: "engagement rate 4.2% (well above the 1–2% Web3 norm)"
  ✗ BAD:  "21 originals in 30 days"
  ✓ GOOD: "21 originals in 30 days (low cadence, under 1/day)"
  ✗ BAD:  "90% reply ratio"
  ✓ GOOD: "90% reply ratio (mascot-tier conversational)"
  ✗ BAD:  "0.6 likes per follower"
  ✓ GOOD: "0.6 likes per follower (exceptional for a 1.6k-follower account)"
  ✗ BAD:  "16x more engagement on media"
  ✓ GOOD: "16x more engagement on media (strong, top of the 5–15x normal range)"

If the number is ALREADY a ratio framing (e.g., "29:1 efficiency gap", "16x multiplier"),
that counts as contextualised, no extra parens needed.

RATIO FRAMING PREFERENCE (load-bearing):

When two metrics describe the same axis at different settings (e.g., media-likes vs text-likes,
weekday vs weekend, reply-engagement vs original-engagement, posts-with-photos vs posts-without),
PREFER the ratio form over the two raw numbers. Ratios are memorable, raw pairs are not.

  ✗ BAD:  "33 avg likes on media posts vs 2 avg likes on text-only posts"
  ✓ GOOD: "media posts pull 16.5x more likes than text-only (33 vs 2)"
  ✗ BAD:  "Brazil-tagged posts average 0.85 sentiment, non-Brazil 0.62"
  ✓ GOOD: "Brazil-tagged posts run 1.4x higher sentiment than the rest (0.85 vs 0.62)"

If you have the two raw numbers, compute the multiplier yourself and lead with it.
The raw pair stays in parens as supporting evidence, not as the headline number.

If the input data contains `peer_distribution` (cohort/peer percentiles), PREFER that comparison
over the static benchmarks, own-data peers are always a tighter, more defensible comparison:
  ✓ EXCELLENT: "engagement rate 4.2% (top quartile in this cohort; cohort median 1.8%)"

DO NOT blindly label every number as "good" or "bad". Sometimes a low number IS the strategy
(a broadcast brand should have a low reply ratio; an analyst account should post infrequently).
Interpret in the context of the account's positioning (the strategic thesis you're building).

═══════════════════════════════════════════════
YOUR OUTPUT, graded on strategic reasoning, NOT number coverage
═══════════════════════════════════════════════

The reader sees the metrics already. They want a thesis, a causal story, and content directions that prove you understood the account's positioning.

Three failure modes that will get the response rejected:

  FAIL 1, METRIC NARRATION. Sentences like "21 originals produced 960 likes at 45.7 avg" are useless. The reader has the metric. Cite numbers only where they sharpen a strategic claim, not in every clause.

  FAIL 2, PAINT-BY-NUMBERS REPLICATION. If the best-performing post is a joke about chickens crossing the road, do NOT propose ten chicken variations. Identify WHAT made it work (e.g. in-group reference, photo as proof, timing of an event) and propose DISTINCT content directions that share the same underlying principle.

  FAIL 3, TAUTOLOGICAL "WHY"s. "Humor triggered in-group recognition driving 86 likes" is a tautology, it describes the content and restates the metric. The right answer names the REAL-WORLD CIRCUMSTANCE that made the content possible: physical presence at an event, an insider reference only a member would catch, a moment of public alignment with a partner. Strategy lives in causes, not labels.

STRATEGIC THESIS (most important field):

The strategic_thesis is the centerpiece. It answers: "What real-world bet is this account making, and why does that bet work?" Derive it from:
  • The bio (language, location, role)
  • Which topics cluster with high sentiment
  • The voice samples (how this person actually writes, including WHO they reply to, not just what they say)
  • The media-vs-text split (what kind of evidence they bring)
  • Which posts overperform relative to followers
  • What the audience is saying back (the inbound replies, do they confirm the positioning?)

Length: 3-5 sentences. First sentence is the punchline (the strategic claim).
Then 2-3 sentences of proof from the data. An optional 5th sentence may state
the strategic bet explicitly ("the bet is..."). Over 5 sentences = padding. Cut.

Example shape (DON'T copy verbatim, use as calibration of *shape*, not framing. Do NOT default to "winning" language; calibrate to what the data actually supports per H3):
"@example_account is positioned to own the [niche] association by being physically inside the community rather than commenting from afar. The supporting evidence: the three best posts share documentary proof (photos, in-person framing). The [niche] topic appears alongside higher engagement, plausibly because the audience reads this content as coming from someone present on the ground. The bet the account is making: own the proof, not the commentary."

If you cannot identify a clear thesis from the data, say so explicitly and propose a falsifiable hypothesis the operator can test.

OPERATOR ACTIONS:

STRATEGIC MOVES with the PRINCIPLE THEY TEST. Three max. Each one names the principle being tested.

HARD CONSTRAINT, these phrases will fail the review:
  ✗ "Schedule a weekly post on [day]"
  ✗ "Post at [hour]:00 UTC"
  ✗ "Every [day of week], publish..."
  ✗ "Use the [tweet_id] format"
  ✗ Any time-of-day or day-of-week recipe
Cadence data is for INTERPRETING WHY content works (audience timezone, lifestyle pattern), not for filling the operator_actions calendar. If you're tempted to write "schedule X on Wednesday at 12 UTC," ask: what's the PRINCIPLE that makes Wednesday land? Write that principle, not the calendar.

  ✗ BAD:  "Schedule one weekly post on Wednesday at 12:00 UTC..."
  ✓ GOOD: "When you're next at an in-person event, post one photo of a real person, not the venue. Tests the principle that overperformance comes from documentary proof, which is why the posts with physical-presence evidence outperformed the abstract commentary in the same topic."
  ✓ GOOD: "Take the next 5 outbound replies you write and rewrite one as an original post with the same content. Tests whether your reply-heavy output is reaching the same audience as your originals or a different (smaller) one."

Three actions. Each one is a TESTABLE BET about audience behavior, not a publishing schedule.

CONTENT SERIES:

Only suggest a series when 3+ posts ALREADY in the data share a repeatable structure AND that structure carries strategic meaning. If no such pattern genuinely exists, OMIT the content_series array entirely, return an empty list `[]`. Do not invent series by templating one-off jokes.

CONTENT IDEAS:

CONCEPT-LEVEL directions, not template-fillers. Name the idea, name the role it plays in the positioning. The manager writes the actual copy.

  BAD: "Photo + caption post of a recent event on Wednesday 12 UTC."
  GOOD: "A post that answers the implicit question the audience keeps asking in replies, stated explicitly, with evidence. Owns the angle the existing content hints at but never makes the direct claim."

═══════════════════════════════════════════════
HONESTY, PRECISION, AND FALSIFIABILITY
═══════════════════════════════════════════════

These rules exist because the previous version of this prompt produced confident-sounding output that was sometimes flattering, sometimes fabricated, and rarely falsifiable. Read carefully:

H1. NUMERIC PRECISION. Percentages: 1 decimal max ("3.9%", never "3.922%"). Multipliers: 1 decimal ("18x" or "18.4x", never "18.426x"). Rates per follower: 2 decimals max. Never echo source precision when it implies certainty the sample size cannot support.

H2. DATA-GROUNDED CITATIONS ONLY. Every number, every named day, every named hour, every claimed pattern in your output must trace to a specific field in the input payload. If you write "Wednesdays produce 84 avg likes", the payload must contain a day-of-week breakdown with that figure. If the payload has no day-of-week or time-of-day breakdown, you may NOT cite days or hours. Inferring from training data is fabrication. When in doubt, omit the figure and describe the pattern qualitatively instead.

H3. HONEST CALIBRATION. Do NOT default to a "winning" framing. The strategic thesis must degrade gracefully:
  • Exceptional metrics across the board → confident thesis about what role the account owns.
  • Mixed metrics → thesis names the role the account is ATTEMPTING with explicit acknowledgment of where execution falls short.
  • Below-average metrics → thesis names the structural problem honestly. Do not invent a niche the account is "winning" if the data does not support it.
  Test your thesis: would it apply equally to a much better account in the same niche? If yes, it is flattery, not analysis. Tighten until the thesis is falsifiable.

H4. CORRELATION VS CAUSATION. The data is observational, not experimental. Use "associated with", "pairs with", "co-occurs with", "performs alongside" when describing relationships between content traits and metrics. Reserve "drives", "causes", "produces", "delivers" for cases where the input payload contains an explicit causal field (very rare; usually none). The 18x media advantage is correlation: higher-effort posts are also more likely to be media posts. Phrase accordingly.

H5. SEPARATE FACTS FROM HYPOTHESES. When citing X platform mechanics (algorithm behaviour, link penalties, out-of-network ranking, ASR effects, completion-rate boosts), prefix with "widely-believed" or "hypothesised" UNLESS it is a documented X feature with a public source. Strategic moves may bet on hypotheses, but state the hypothesis as a hypothesis. Example: "tests the widely-held belief that external links reduce initial distribution", not "removes the external-link penalty".

H6. NO EM DASHES. Never use em dashes (,) anywhere in your output. Use a comma, period, parenthesis, colon, or semicolon instead. This rule is absolute, including inside strategic_thesis prose and bullet copy.

H7. EXPLAIN UNFAMILIAR LABELS. If you cite a metric label that is not in plain English (Q30, EPF, EPV, WER, ASR, vibe score), either expand it inline on first use or omit it. The reader is a smart operator, not a VibeChecx insider.

═══════════════════════════════════════════════
OTHER RULES
═══════════════════════════════════════════════

1. ZERO made-up numbers, handles, tweet IDs, or topics. Only what's in the data.
2. ZERO external references (no "the industry", "typically", "competitors").
3. top_performers: copy exact likes/views/replies_count/retweets from `top_outbound_tweets[]`. Include tweet_id AND authored_by (copy from the `authored_by` field in `top_outbound_tweets[]`). `replies_count` in your output maps to `inbound_replies_count_on_this_tweet` in the input.
4. weaknesses: name a strategic gap, not a metric anomaly. What ROLE is this account not playing yet?
5. behavioral_headline: one quotable sentence. Strategic, not statistical.
6. account_classification: community_mascot|brand|creator|media|community_hub.
7. hidden_patterns: must be non-obvious, a synthesis across 2+ data points. Strategic, not descriptive.

NEVER USE these phrases (marketing-junior cliché): "post more consistently", "engage with your audience", "build community", "leverage your content", "amplify your voice", "drive engagement".

{platform_context}
"""

# Load platform_context.md once at startup. Injected into INSIGHTS_SYSTEM_PROMPT
# above via .format(). Edit the .md file when X ships algo changes, no code change.
_PLATFORM_CONTEXT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform_context.md")
try:
    with open(_PLATFORM_CONTEXT_PATH) as _f:
        _platform_context_raw = _f.read().strip()
    # Strip comment lines (lines starting with #) from the md header
    _platform_lines = [l for l in _platform_context_raw.splitlines() if not l.startswith("# ")]
    _PLATFORM_CONTEXT = "\n".join(_platform_lines).strip()
except FileNotFoundError:
    _PLATFORM_CONTEXT = ""

INSIGHTS_SYSTEM_PROMPT = INSIGHTS_SYSTEM_PROMPT.format(platform_context=(
    "═══════════════════════════════════════════════\n"
    "PLATFORM MECHANICS, ground every recommendation in these scoring signals\n"
    "═══════════════════════════════════════════════\n\n"
    + _PLATFORM_CONTEXT
) if _PLATFORM_CONTEXT else "")


INSIGHTS_USER_PROMPT = """SCOPE: {cohort_name}
PERIOD: {period}
ACCOUNTS: {account_count}
TOTAL OUTBOUND TWEETS: {tweet_count}

{personality_context}
DATA PAYLOAD (every number is exact from the database; field names follow the direction terminology in your instructions):
{totals_json}

Produce the strategic analysis as JSON matching this schema:
{schema}
"""

def _conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


class Provider:
    name = "none"
    model = None

    def __init__(self, api_key, base_url, model):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def call(self, user_prompt, system_prompt=None, max_tokens=4000):
        # temperature=0: zero randomness, the model picks the most-grounded
        # token every step. Critical for "only use these handles / numbers"
        # tasks. Reasoning models pair with this naturally, they do their
        # chain-of-thought separately, then commit to a deterministic answer.
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0,
        )
        return resp.choices[0].message.content.strip()


def make_grok():
    # Use the reasoning variant of Grok. The non-reasoning model
    # (`grok-4.20-0309-non-reasoning`) is faster but skips the chain-of-thought
    # step that makes strict data-grounding tasks reliable. Reasoning model
    # name is the same id without the `-non-reasoning` suffix; allow env
    # override for easy swap.
    key = xai_api_key()
    if not key:
        return None
    import os as _os
    model = _os.environ.get("VIBECHECX_GROK_MODEL", "grok-4.20-0309")
    p = Provider(key, "https://api.x.ai/v1", model)
    p.name = "grok"
    return p


def make_deepseek():
    # `deepseek-reasoner` is DeepSeek's chain-of-thought model. Slower than
    # `deepseek-chat` but better instruction-following on schema-strict
    # outputs. Override via env if needed.
    key = deepseek_api_key()
    if not key:
        return None
    import os as _os
    model = _os.environ.get("VIBECHECX_DEEPSEEK_MODEL", "deepseek-reasoner")
    p = Provider(key, "https://api.deepseek.com/v1", model)
    p.name = "deepseek"
    return p


def make_openai():
    # GPT-4o-mini default: cheapest OpenAI model that handles structured
    # JSON output reliably. Override via env if needed.
    key = openai_api_key()
    if not key:
        return None
    import os as _os
    model = _os.environ.get("VIBECHECX_OPENAI_MODEL", "gpt-4o-mini")
    p = Provider(key, "https://api.openai.com/v1", model)
    p.name = "openai"
    return p


def _parse_response(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("\n", 1)
        text = parts[1].rsplit("```", 1)[0].strip() if len(parts) == 2 else parts[0].replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _compute_peer_distribution(cur, account_ids: list[int], days: int,
                                min_n: int = 5) -> dict | None:
    """Return p25/median/p75 across the given accounts for the metrics the
    LLM is asked to interpret. Returns None when N < min_n (small sets give
    noisy percentiles that mislead more than they help)."""
    if not account_ids or len(account_ids) < min_n:
        return None
    cur.execute(
        """
        WITH per_account AS (
            SELECT a.id AS account_id,
                   GREATEST(a.followers_count, 1) AS followers,
                   COUNT(*) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet)::int AS posts,
                   COALESCE(SUM(t.likes) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet),0)::int AS post_likes,
                   COALESCE(SUM(t.views) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet),0)::int AS post_views,
                   COUNT(*) FILTER (WHERE t.is_reply)::int AS replies
            FROM accounts a
            LEFT JOIN tweets t
                ON t.author_account_id = a.id
               AND t.created_at > NOW() - INTERVAL '%s days'
            WHERE a.id = ANY(%s)
            GROUP BY a.id, a.followers_count
        ),
        derived AS (
            SELECT
                CASE WHEN post_views > 0
                     THEN post_likes::float / post_views * 100 ELSE NULL END AS engagement_rate_pct,
                CASE WHEN posts > 0
                     THEN post_likes::float / posts ELSE NULL END AS avg_likes_per_original,
                CASE WHEN posts > 0
                     THEN post_likes::float / followers ELSE NULL END AS likes_per_follower,
                posts::float / %s AS posts_per_day,
                CASE WHEN (posts + replies) > 0
                     THEN replies::float / (posts + replies) * 100 ELSE NULL END AS outbound_reply_ratio_pct
            FROM per_account
        )
        SELECT
            percentile_cont(0.25) WITHIN GROUP (ORDER BY engagement_rate_pct)      AS er_p25,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY engagement_rate_pct)      AS er_p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY engagement_rate_pct)      AS er_p75,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY avg_likes_per_original)   AS lp_p25,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY avg_likes_per_original)   AS lp_p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY avg_likes_per_original)   AS lp_p75,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY likes_per_follower)       AS lf_p25,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY likes_per_follower)       AS lf_p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY likes_per_follower)       AS lf_p75,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY posts_per_day)            AS pd_p25,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY posts_per_day)            AS pd_p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY posts_per_day)            AS pd_p75,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY outbound_reply_ratio_pct) AS rr_p25,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY outbound_reply_ratio_pct) AS rr_p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY outbound_reply_ratio_pct) AS rr_p75
        FROM derived
        """,
        (days, account_ids, days),
    )
    row = cur.fetchone()
    if not row:
        return None

    def _r2(x):
        return None if x is None else round(float(x), 3)

    return {
        "n_peers": len(account_ids),
        "engagement_rate_pct":       {"p25": _r2(row["er_p25"]), "median": _r2(row["er_p50"]), "p75": _r2(row["er_p75"])},
        "avg_likes_per_original":    {"p25": _r2(row["lp_p25"]), "median": _r2(row["lp_p50"]), "p75": _r2(row["lp_p75"])},
        "likes_per_follower":        {"p25": _r2(row["lf_p25"]), "median": _r2(row["lf_p50"]), "p75": _r2(row["lf_p75"])},
        "posts_per_day":             {"p25": _r2(row["pd_p25"]), "median": _r2(row["pd_p50"]), "p75": _r2(row["pd_p75"])},
        "outbound_reply_ratio_pct":  {"p25": _r2(row["rr_p25"]), "median": _r2(row["rr_p50"]), "p75": _r2(row["rr_p75"])},
    }


def _fetch_cohort_data(cohort_id, days):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, description, ecosystem_handles "
            "FROM cohorts WHERE id=%s",
            (cohort_id,),
        )
        cohort = cur.fetchone()
        if not cohort:
            return None
        ecosystem_handles = cohort.get("ecosystem_handles") or []

        cur.execute(
            """
            SELECT a.username, a.display_name, a.followers_count, a.following_count,
                a.tweets_count, a.bio,
                COUNT(*) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet AND NOT t.is_quote)::int AS posts,
                COUNT(*) FILTER (WHERE t.is_reply)::int AS replies,
                COALESCE(SUM(t.likes) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet AND NOT t.is_quote), 0)::int AS post_likes,
                COALESCE(SUM(t.likes) FILTER (WHERE t.is_reply), 0)::int AS reply_likes,
                COALESCE(SUM(t.views) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet AND NOT t.is_quote), 0)::int AS post_views,
                COALESCE(SUM(t.views) FILTER (WHERE t.is_reply), 0)::int AS reply_views,
                COALESCE(SUM(t.retweets) FILTER (WHERE NOT t.is_retweet), 0)::int AS retweets,
                COALESCE(AVG(t.quality_score) FILTER (WHERE NOT t.is_retweet AND t.quality_score IS NOT NULL), 0)::real AS avg_quality,
                COALESCE(AVG(t.sentiment) FILTER (WHERE t.sentiment IS NOT NULL), 0)::real AS avg_sentiment,
                COALESCE(AVG(t.quality_score) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet AND NOT t.is_quote AND t.quality_score IS NOT NULL), 0)::real AS posts_avg_quality
            FROM cohort_members cm JOIN accounts a ON a.id=cm.account_id
            LEFT JOIN tweets t ON t.author_account_id=a.id AND t.created_at > NOW() - INTERVAL '%s days'
            WHERE cm.cohort_id=%s
            GROUP BY a.username, a.display_name, a.followers_count, a.following_count, a.tweets_count, a.bio
            ORDER BY COALESCE(SUM(t.likes) FILTER (WHERE NOT t.is_reply AND NOT t.is_retweet),0) DESC
            """, (days, cohort_id),
        )
        accounts = cur.fetchall()

        cur.execute(
            """
            SELECT a.username, t.tweet_id, t.content, t.likes, t.replies, t.retweets, t.views,
                   t.quality_score, t.sentiment,
                   CASE WHEN t.is_reply THEN 'reply' ELSE 'original_post' END AS type
            FROM cohort_members cm JOIN accounts a ON a.id=cm.account_id
            JOIN tweets t ON t.author_account_id=a.id
            WHERE cm.cohort_id=%s AND NOT t.is_retweet AND t.created_at > NOW() - INTERVAL '%s days' AND t.likes>0
            ORDER BY (t.likes+t.retweets+t.replies) DESC LIMIT 30
            """, (cohort_id, days),
        )
        top_tweets = cur.fetchall()

        cur.execute(
            """
            SELECT unnest(t.tags) AS topic,
                   COUNT(*) FILTER (WHERE NOT t.is_reply) AS post_mentions, COUNT(*) AS total_mentions,
                   COALESCE(AVG(t.sentiment) FILTER (WHERE NOT t.is_reply),0)::real AS post_sentiment,
                   COALESCE(AVG(t.sentiment),0)::real AS overall_sentiment
            FROM cohort_members cm JOIN accounts a ON a.id=cm.account_id
            JOIN tweets t ON t.author_account_id=a.id
            WHERE cm.cohort_id=%s AND NOT t.is_retweet AND t.created_at > NOW() - INTERVAL '%s days'
              AND t.tags IS NOT NULL AND array_length(t.tags,1)>0
            GROUP BY topic ORDER BY COUNT(*) DESC LIMIT 15
            """, (cohort_id, days),
        )
        topics = cur.fetchall()

        # Per-account top topics
        cur.execute(
            """
            SELECT a.username, t.topic, t.mentions
            FROM (
                SELECT author_account_id, unnest(tags) AS topic, COUNT(*) AS mentions
                FROM tweets JOIN cohort_members cm ON author_account_id=cm.account_id
                WHERE cm.cohort_id=%s AND NOT is_retweet AND created_at > NOW() - INTERVAL '%s days'
                  AND tags IS NOT NULL AND array_length(tags,1)>0
                GROUP BY author_account_id, topic
            ) t JOIN accounts a ON a.id=t.author_account_id
            ORDER BY a.username, t.mentions DESC
            """, (cohort_id, days),
        )
        raw_account_topics = cur.fetchall()

        # Topic leaders: which accounts dominate each topic
        cur.execute(
            """
            SELECT topic, username, mentions, rank
            FROM (
                SELECT unnest(tags) AS topic, a.username, COUNT(*) AS mentions,
                       ROW_NUMBER() OVER (PARTITION BY unnest(tags) ORDER BY COUNT(*) DESC) AS rank
                FROM tweets t JOIN cohort_members cm ON t.author_account_id=cm.account_id
                JOIN accounts a ON a.id=t.author_account_id
                WHERE cm.cohort_id=%s AND NOT t.is_retweet AND t.created_at > NOW() - INTERVAL '%s days'
                  AND t.tags IS NOT NULL AND array_length(t.tags,1)>0
                GROUP BY topic, a.username
            ) ranked WHERE rank <= 3
            ORDER BY topic, rank
            """, (cohort_id, days),
        )
        topic_leaders = cur.fetchall()

        # Cohort-wide posting cadence (best day-of-week by avg likes)
        cur.execute(
            """
            SELECT EXTRACT(DOW FROM t.created_at)::int AS dow,
                   COUNT(*)::int AS tweet_count,
                   COALESCE(AVG(t.likes), 0)::real AS avg_likes,
                   COALESCE(AVG(t.views), 0)::real AS avg_views
            FROM tweets t JOIN cohort_members cm ON t.author_account_id=cm.account_id
            WHERE cm.cohort_id=%s AND NOT t.is_retweet
              AND t.created_at > NOW() - INTERVAL '%s days'
            GROUP BY dow ORDER BY avg_likes DESC
            """, (cohort_id, days),
        )
        cadence = cur.fetchall()

        # Content format split across cohort
        cur.execute(
            """
            SELECT CASE WHEN EXISTS(SELECT 1 FROM media m WHERE m.tweet_id=t.tweet_id)
                        THEN 'media' ELSE 'text' END AS fmt,
                   COUNT(*)::int AS count,
                   COALESCE(AVG(t.likes), 0)::real AS avg_likes,
                   COALESCE(AVG(t.views), 0)::real AS avg_views
            FROM tweets t JOIN cohort_members cm ON t.author_account_id=cm.account_id
            WHERE cm.cohort_id=%s AND NOT t.is_retweet
              AND t.created_at > NOW() - INTERVAL '%s days'
            GROUP BY fmt
            """, (cohort_id, days),
        )
        format_split = cur.fetchall()

        # Peer distribution, p25/median/p75 across this cohort's members for
        # the metrics the LLM should interpret. None if cohort has <5 members.
        cur.execute(
            "SELECT account_id FROM cohort_members WHERE cohort_id=%s",
            (cohort_id,),
        )
        member_ids = [r["account_id"] for r in cur.fetchall()]
        peer_distribution = _compute_peer_distribution(cur, member_ids, days)

    return {
        "cohort": cohort, "accounts": accounts, "top_tweets": top_tweets, "topics": topics,
        "account_topics": raw_account_topics, "topic_leaders": topic_leaders,
        "cadence": cadence, "format_split": format_split,
        "ecosystem_handles": ecosystem_handles,
        "peer_distribution": peer_distribution,
    }


def _fetch_account_data(account_id, days):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT username, display_name, followers_count, following_count, tweets_count, bio, first_seen_at "
            "FROM accounts WHERE id=%s", (account_id,)
        )
        account = cur.fetchone()
        if not account:
            return None

        cur.execute(
            """
            SELECT t.tweet_id, t.content, t.likes, t.replies, t.retweets,
                   t.views, t.bookmarks, t.quality_score, t.sentiment,
                   t.is_reply, t.is_quote,
                   parent_acct.username AS reply_to_username,
                   CASE
                     WHEN t.is_reply THEN 'outbound_reply'
                     WHEN t.is_quote THEN 'outbound_quote'
                     ELSE 'outbound_original'
                   END AS type
            FROM tweets t
            LEFT JOIN tweets parent ON parent.tweet_id = t.reply_to_tweet_id
            LEFT JOIN accounts parent_acct ON parent_acct.id = parent.author_account_id
            WHERE t.author_account_id=%s AND NOT t.is_retweet
              AND t.created_at > NOW() - INTERVAL '%s days'
            ORDER BY (t.likes + t.retweets + t.replies) DESC
            """, (account_id, days),
        )
        tweets = cur.fetchall()

        cur.execute(
            """
            SELECT COUNT(*)::int AS total, COUNT(*) FILTER (WHERE NOT is_reply)::int AS posts,
                   COUNT(*) FILTER (WHERE is_reply)::int AS replies,
                   COALESCE(SUM(likes) FILTER (WHERE NOT is_reply),0)::int AS post_likes,
                   COALESCE(SUM(likes) FILTER (WHERE is_reply),0)::int AS reply_likes,
                   COALESCE(SUM(views) FILTER (WHERE NOT is_reply),0)::int AS post_views,
                   COALESCE(SUM(views) FILTER (WHERE is_reply),0)::int AS reply_views,
                   COALESCE(SUM(retweets) FILTER (WHERE NOT is_reply),0)::int AS post_retweets,
                   COALESCE(AVG(quality_score) FILTER (WHERE quality_score IS NOT NULL),0)::real AS avg_quality,
                   COALESCE(AVG(quality_score) FILTER (WHERE NOT is_reply AND quality_score IS NOT NULL),0)::real AS posts_avg_quality,
                   COALESCE(AVG(sentiment) FILTER (WHERE sentiment IS NOT NULL),0)::real AS avg_sentiment
            FROM tweets WHERE author_account_id=%s AND NOT is_retweet AND created_at > NOW() - INTERVAL '%s days'
            """, (account_id, days),
        )
        breakdown = cur.fetchone()

        cur.execute(
            """
            SELECT unnest(tags) AS topic,
                   COUNT(*) FILTER (WHERE NOT is_reply) AS post_mentions, COUNT(*) AS total_mentions,
                   COALESCE(AVG(sentiment) FILTER (WHERE NOT is_reply),0)::real AS post_sentiment,
                   COALESCE(AVG(sentiment),0)::real AS overall_sentiment
            FROM tweets WHERE author_account_id=%s AND NOT is_retweet AND created_at > NOW() - INTERVAL '%s days'
              AND tags IS NOT NULL AND array_length(tags,1)>0
            GROUP BY topic ORDER BY COUNT(*) DESC LIMIT 15
            """, (account_id, days),
        )
        topics = cur.fetchall()

        # Voice samples, outbound replies AUTHORED BY this account, with the
        # username of who they were replying TO and (when stored) an excerpt of
        # the parent tweet. Without parent context, "good point" is illegible.
        cur.execute(
            """
            SELECT t.content, t.likes,
                   parent.content AS parent_content,
                   parent_acct.username AS parent_author
            FROM tweets t
            LEFT JOIN tweets parent ON parent.tweet_id = t.reply_to_tweet_id
            LEFT JOIN accounts parent_acct ON parent_acct.id = parent.author_account_id
            WHERE t.author_account_id=%s AND t.is_reply AND NOT t.is_retweet
              AND t.likes > 0
              AND t.created_at > NOW() - INTERVAL '%s days'
            ORDER BY t.likes DESC LIMIT 5
            """, (account_id, days),
        )
        voice_samples = cur.fetchall()

        # Inbound replies, what OTHER people said back when replying to this
        # account's posts. Pulled from the `replies` table (populated by
        # replyminer). Top-engaging only, with parent excerpt so the LLM sees
        # the question→answer pair. Excludes gab's own self-thread replies.
        cur.execute(
            """
            SELECT r.content AS reply_content,
                   r.likes AS reply_likes,
                   replier.username AS replier_handle,
                   r.tweet_id AS parent_tweet_id,
                   parent.content AS parent_tweet_excerpt
            FROM replies r
            JOIN accounts replier ON replier.id = r.author_account_id
            JOIN tweets parent ON parent.tweet_id = r.tweet_id
            WHERE parent.author_account_id = %s
              AND r.is_author_reply = FALSE
              AND r.created_at > NOW() - INTERVAL '%s days'
            ORDER BY r.likes DESC NULLS LAST LIMIT 10
            """, (account_id, days),
        )
        inbound_replies = cur.fetchall()

        # Aggregate inbound reply stats, counts + which of our posts attracted
        # the most conversation.
        cur.execute(
            """
            SELECT COUNT(*)::int AS inbound_replies_received,
                   COUNT(DISTINCT r.author_account_id)::int AS distinct_repliers,
                   COALESCE(SUM(r.likes), 0)::int AS inbound_reply_likes_total
            FROM replies r
            JOIN tweets parent ON parent.tweet_id = r.tweet_id
            WHERE parent.author_account_id = %s
              AND r.is_author_reply = FALSE
              AND r.created_at > NOW() - INTERVAL '%s days'
            """, (account_id, days),
        )
        inbound_stats = cur.fetchone()

        # Posting cadence, DOW + hour by avg likes. Tells LLM when content lands best.
        cur.execute(
            """
            SELECT EXTRACT(DOW FROM created_at)::int AS dow,
                   EXTRACT(HOUR FROM created_at)::int AS hour,
                   COUNT(*)::int AS tweet_count,
                   COALESCE(AVG(likes), 0)::real AS avg_likes,
                   COALESCE(AVG(views), 0)::real AS avg_views
            FROM tweets WHERE author_account_id=%s AND NOT is_retweet
              AND created_at > NOW() - INTERVAL '%s days'
            GROUP BY dow, hour ORDER BY avg_likes DESC LIMIT 20
            """, (account_id, days),
        )
        cadence = cur.fetchall()

        # Content format split, media posts vs text-only.
        cur.execute(
            """
            SELECT CASE WHEN EXISTS(SELECT 1 FROM media m WHERE m.tweet_id=t.tweet_id)
                        THEN 'media' ELSE 'text' END AS fmt,
                   COUNT(*)::int AS count,
                   COALESCE(AVG(t.likes), 0)::real AS avg_likes,
                   COALESCE(AVG(t.views), 0)::real AS avg_views
            FROM tweets t WHERE t.author_account_id=%s AND NOT t.is_retweet
              AND t.created_at > NOW() - INTERVAL '%s days'
            GROUP BY fmt
            """, (account_id, days),
        )
        format_split = cur.fetchall()

        # Follower growth over the period (requires account_snapshots, migration 005).
        follower_growth = None
        try:
            cur.execute(
                """
                SELECT (MAX(followers) - MIN(followers))::int AS follower_delta,
                       MIN(followers)::int AS followers_start,
                       MAX(followers)::int AS followers_end,
                       COUNT(*)::int AS snapshot_count
                FROM account_snapshots
                WHERE account_id=%s AND recorded_at > NOW() - INTERVAL '%s days'
                """, (account_id, days),
            )
            row = cur.fetchone()
            if row and row.get("snapshot_count", 0) >= 2:
                follower_growth = dict(row)
        except Exception:
            pass  # table may not exist yet; silently skip

        # Inherit ecosystem_handles from any cohort the account belongs to.
        # Union across all of them, being in multiple cohorts shouldn't
        # narrow the legitimate-reference set.
        ecosystem_handles = []
        try:
            cur.execute(
                """
                SELECT DISTINCT jsonb_array_elements_text(c.ecosystem_handles) AS h
                FROM cohort_members cm
                JOIN cohorts c ON c.id = cm.cohort_id
                WHERE cm.account_id = %s
                  AND c.ecosystem_handles IS NOT NULL
                  AND jsonb_array_length(c.ecosystem_handles) > 0
                """,
                (account_id,),
            )
            ecosystem_handles = [r["h"] for r in cur.fetchall()]
        except Exception:
            pass  # column may not exist yet pre-migration-007

        # Peer distribution: union of cohort members across all cohorts this
        # account belongs to, excluding self. Gives the LLM "you're at p87
        # vs your peers" without needing site-wide aggregation.
        peer_distribution = None
        try:
            cur.execute(
                """
                SELECT DISTINCT cm2.account_id
                FROM cohort_members cm1
                JOIN cohort_members cm2 ON cm1.cohort_id = cm2.cohort_id
                WHERE cm1.account_id = %s AND cm2.account_id != %s
                """,
                (account_id, account_id),
            )
            peer_ids = [r["account_id"] for r in cur.fetchall()]
            if peer_ids:
                peer_distribution = _compute_peer_distribution(cur, peer_ids, days)
        except Exception:
            pass

    return {
        "account": account, "tweets": tweets, "breakdown": breakdown, "topics": topics,
        "voice_samples": voice_samples, "cadence": cadence,
        "format_split": format_split, "follower_growth": follower_growth,
        "inbound_replies": inbound_replies, "inbound_stats": inbound_stats,
        "ecosystem_handles": ecosystem_handles,
        "peer_distribution": peer_distribution,
    }


_DOW_NAMES = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]


def _build_personality_context(data, scope_type):
    """Build a natural-language context block injected at the top of the prompt.
    For account scope: bio, voice samples, cadence, format split, follower growth.
    For cohort scope: cadence and format split aggregated across all members."""
    lines = []

    cadence = data.get("cadence", [])
    format_split = data.get("format_split", [])

    if scope_type == "account":
        acct = data.get("account", {})
        bio = (acct.get("bio") or "").strip()
        followers = acct.get("followers_count", 0)
        following = acct.get("following_count", 0)
        lifetime_tweets = acct.get("tweets_count", 0)
        first_seen = acct.get("first_seen_at")

        if bio:
            lines.append(f'Bio: "{bio}"')
        if followers:
            ratio = round(following / followers, 2) if followers else 0
            lines.append(f"Followers: {followers:,} | Following: {following:,} | Ratio: {ratio}")
        if lifetime_tweets:
            lines.append(f"Lifetime tweets (account total): {lifetime_tweets:,}")
        if first_seen:
            try:
                age_days = (datetime.now(timezone.utc) - first_seen.replace(tzinfo=timezone.utc)).days
                lines.append(f"Account age: ~{age_days} days")
            except Exception:
                pass

        # Outbound voice samples, replies AUTHORED by this account, WITH
        # context on who they were replying to. Without parent context, "good
        # point" is illegible.
        samples = data.get("voice_samples", [])
        if samples:
            lines.append("\nHow this account writes (top-liked outbound replies, with what they were responding to):")
            for s in samples[:3]:
                content = (s.get("content") or "")[:110].replace("\n", " ")
                lk = s.get("likes", 0)
                parent_author = s.get("parent_author") or s.get("reply_to_username")
                parent_excerpt = (s.get("parent_content") or "")[:70].replace("\n", " ")
                if not content:
                    continue
                if parent_author and parent_excerpt:
                    lines.append(
                        f'  • Replying to @{parent_author} ("{parent_excerpt}"): "{content}" ({lk} ♥)'
                    )
                elif parent_author:
                    lines.append(f'  • Replying to @{parent_author}: "{content}" ({lk} ♥)')
                else:
                    lines.append(f'  • "{content}" ({lk} ♥)')

        # Inbound, what audience is saying back. This is the conversation the
        # account is GENERATING, distinct from the conversations it joins.
        inbound = data.get("inbound_replies") or []
        inbound_stats = data.get("inbound_stats") or {}
        ir_total = inbound_stats.get("inbound_replies_received", 0) if inbound_stats else 0
        if ir_total:
            distinct = inbound_stats.get("distinct_repliers", 0)
            lines.append(
                f"\nWhat the audience says back (inbound replies, others replying to this account): "
                f"{ir_total} total replies from {distinct} distinct people."
            )
            for r in inbound[:3]:
                rc = (r.get("reply_content") or "")[:110].replace("\n", " ")
                pc = (r.get("parent_tweet_excerpt") or "")[:60].replace("\n", " ")
                rl = r.get("reply_likes", 0)
                rh = r.get("replier_handle") or "?"
                if rc and pc:
                    lines.append(
                        f'  • @{rh} replied "{rc}" ({rl} ♥), to the account\'s post: "{pc}"'
                    )

        # Follower growth
        growth = data.get("follower_growth")
        if growth and growth.get("snapshot_count", 0) >= 2:
            delta = growth.get("follower_delta", 0)
            if delta != 0:
                sign = "+" if delta > 0 else ""
                lines.append(f"\nFollower growth this period: {sign}{delta} "
                              f"({growth.get('followers_start',0):,} → {growth.get('followers_end',0):,})")

    # Cadence (both scopes)
    if cadence and len(cadence) >= 3:
        best = cadence[0]
        dow = best.get("dow")
        if dow is not None and 0 <= int(dow) <= 6:
            best_day = _DOW_NAMES[int(dow)]
            avg_lk = round(best.get("avg_likes", 0))
            cadence_line = f"Best posting day: {best_day} (avg {avg_lk} likes/post)"
            if scope_type == "account" and best.get("hour") is not None:
                cadence_line += f" · Best hour (UTC): {int(best['hour'])}:00"
            lines.append(f"\nPosting cadence, {cadence_line}")

    # Format split (both scopes)
    if format_split:
        media_row = next((r for r in format_split if r.get("fmt") == "media"), None)
        text_row  = next((r for r in format_split if r.get("fmt") == "text"), None)
        if media_row and text_row:
            m_lk = round(media_row.get("avg_likes", 0))
            t_lk = round(text_row.get("avg_likes", 0))
            m_ct = media_row.get("count", 0)
            t_ct = text_row.get("count", 0)
            mult  = f"{m_lk/t_lk:.1f}x" if t_lk > 0 else "N/A"
            lines.append(
                f"Content format: {m_ct} media posts (avg {m_lk} ♥) vs "
                f"{t_ct} text-only (avg {t_lk} ♥), media gets {mult} engagement"
            )

    if not lines:
        return ""
    header = "ACCOUNT CONTEXT:" if scope_type == "account" else "COHORT CONTEXT:"
    return header + "\n" + "\n".join(lines) + "\n\n"


def _build_prompt(data, period, scope_type="cohort"):
    if scope_type == "cohort":
        cohort_name = data["cohort"]["name"]
        accounts = data["accounts"]
        top_tweets = data["top_tweets"]
        topics = data["topics"]
        account_topics = data.get("account_topics", [])
        topic_leaders = data.get("topic_leaders", [])
    else:
        cohort_name = data["account"]["username"]
        # Merge the breakdown row (posts/likes/views totals) into the single
        # account dict so the prompt sees real numbers, not zeros. Without
        # this, totals are computed as 0 and the LLM faithfully reports
        # "posted 0 times" while top_tweets still surfaces real content.
        merged_account = dict(data["account"])
        breakdown = data.get("breakdown") or {}
        merged_account.update({
            "posts":             breakdown.get("posts", 0),
            "replies":           breakdown.get("replies", 0),
            "post_likes":        breakdown.get("post_likes", 0),
            "reply_likes":       breakdown.get("reply_likes", 0),
            "post_views":        breakdown.get("post_views", 0),
            "reply_views":       breakdown.get("reply_views", 0),
            "avg_quality":       breakdown.get("avg_quality", 0),
            "posts_avg_quality": breakdown.get("posts_avg_quality", 0),
            "avg_sentiment":     breakdown.get("avg_sentiment", 0),
        })
        accounts = [merged_account]
        top_tweets = data["tweets"]
        topics = data["topics"]
        account_topics = []
        topic_leaders = []

    total_posts = sum(a.get("posts", 0) for a in (accounts or []))
    total_replies = sum(a.get("replies", 0) for a in (accounts or []))
    total_post_likes = sum(a.get("post_likes", a.get("total_likes", 0)) for a in (accounts or []))
    total_reply_likes = sum(a.get("reply_likes", 0) for a in (accounts or []))
    total_post_views = sum(a.get("post_views", a.get("total_views", 0)) for a in (accounts or []))
    total_reply_views = sum(a.get("reply_views", 0) for a in (accounts or []))

    # Build per-account topic map
    acct_topics_map = {}
    for at in account_topics:
        uname = at["username"]
        if uname not in acct_topics_map:
            acct_topics_map[uname] = []
        if len(acct_topics_map[uname]) < 3:
            acct_topics_map[uname].append(at["topic"])

    # Per-account top tweets, helps the LLM ground "kudos" / "top performers"
    # in real text instead of inventing details. Include full metrics so
    # the LLM can cite exact numbers (judges flagged missing likes/views).
    acct_top_tweets = {}
    for t in (top_tweets or []):
        uname = t.get("username")
        if not uname:
            continue
        if uname not in acct_top_tweets:
            acct_top_tweets[uname] = []
        if len(acct_top_tweets[uname]) < 3:
            if t.get("is_reply"):
                tdir = "outbound_reply"
            elif t.get("is_quote"):
                tdir = "outbound_quote"
            else:
                tdir = "outbound_original"
            entry = {
                "tweet_id": t.get("tweet_id"),
                "direction": tdir,
                "content_preview": (t.get("content") or "")[:120],
                "likes": t.get("likes", 0),
                "views": t.get("views", 0),
                "inbound_replies_count_on_this_tweet": t.get("replies", 0),
                "retweets": t.get("retweets", 0),
                "quality_score": round(t.get("quality_score") or 0, 1),
            }
            if tdir == "outbound_reply" and t.get("reply_to_username"):
                entry["replying_to_username"] = "@" + t["reply_to_username"]
            acct_top_tweets[uname].append(entry)

    # Per-account metrics. EVERY field name is direction-tagged:
    #   "outbound_*" = this account is the AUTHOR (their posts, their replies)
    #   "inbound_*"  = OTHERS addressing this account (replies received by them)
    accounts_json = []
    for a in (accounts or [])[:20]:
        uname = a.get("username", "?")
        post_likes = a.get("post_likes", a.get("total_likes", 0))
        post_views = a.get("post_views", a.get("total_views", 0))
        replies_n = a.get("replies", 0)
        posts_n = a.get("posts", 0)
        followers = a.get("followers_count", 0)
        following = a.get("following_count", 0)
        eng_rate = (post_likes / post_views) if post_views > 0 else 0.0
        reply_ratio = (replies_n / (replies_n + posts_n)) if (replies_n + posts_n) > 0 else 0.0
        likes_per_follower = round(post_likes / followers, 3) if followers > 0 else 0.0
        entry = {
            "username": uname,
            "display_name": a.get("display_name") or "",
            "bio": (a.get("bio") or "")[:120],
            "followers": followers,
            "following": following,
            # Outbound (authored by this account)
            "outbound_originals_count":   posts_n,
            "outbound_originals_likes":   post_likes,
            "outbound_originals_views":   post_views,
            "outbound_replies_count":     replies_n,
            "outbound_replies_likes":     a.get("reply_likes", 0),
            "outbound_replies_views":     a.get("reply_views", 0),
            "avg_likes_per_outbound_original": round(post_likes / posts_n, 1) if posts_n > 0 else 0,
            "avg_likes_per_outbound_reply":    round(a.get("reply_likes", 0) / replies_n, 2) if replies_n > 0 else 0,
            "likes_per_follower":      likes_per_follower,
            "engagement_rate_pct":     round(eng_rate * 100, 3),
            "outbound_reply_ratio_pct": round(reply_ratio * 100, 1),
            "avg_quality":             round(a.get("avg_quality", 0) or 0, 1),
            "originals_avg_quality":   round(a.get("posts_avg_quality", 0) or 0, 1),
            "avg_sentiment":           round(a.get("avg_sentiment", 0) or 0, 2),
            "top_topics":              acct_topics_map.get(uname, []),
            "top_3_outbound_tweets":   acct_top_tweets.get(uname, []),
        }
        accounts_json.append(entry)

    # Top tweets are AUTHORED BY THE TRACKED ACCOUNT. Each one is tagged with
    # a precise direction. "outbound_reply" entries carry replying_to_username
    # so the LLM can read them in context.
    tweets_json = []
    for t in (top_tweets or [])[:15]:
        raw_type = t.get("type", "original_post")
        # Normalise to explicit categories
        if raw_type == "reply" or t.get("is_reply"):
            direction = "outbound_reply"
        elif t.get("is_quote"):
            direction = "outbound_quote"
        else:
            direction = "outbound_original"
        entry = {
            "tweet_id": t.get("tweet_id", ""),
            "authored_by": t.get("username", "?"),
            "direction": direction,
            "content_preview": (t.get("content") or "")[:120],
            "likes": t.get("likes", 0),
            "inbound_replies_count_on_this_tweet": t.get("replies", 0),
            "retweets": t.get("retweets", 0),
            "bookmarks": t.get("bookmarks", 0),
            "views": t.get("views", 0),
            "quality_score": round(t.get("quality_score") or 0, 1),
            "sentiment": round(t.get("sentiment") or 0, 2),
        }
        if direction == "outbound_reply":
            ru = t.get("reply_to_username")
            if ru:
                entry["replying_to_username"] = f"@{ru}"
        tweets_json.append(entry)

    topics_json = []
    for tp in (topics or [])[:15]:
        topics_json.append({
            "topic": tp["topic"],
            "in_posts": tp.get("post_mentions", 0),
            "total_mentions": tp.get("total_mentions", tp.get("mentions", 0)),
            "avg_sentiment": round(tp.get("overall_sentiment", tp.get("avg_sentiment", 0)), 2),
        })

    # Build topic leaders: per-topic, accounts that dominate
    topic_leaders_json = {}
    for tl in topic_leaders:
        topic = tl["topic"]
        if topic not in topic_leaders_json:
            topic_leaders_json[topic] = []
        topic_leaders_json[topic].append(tl["username"])

    # Inbound replies (account scope only, sampled people-replying-to-account)
    inbound_replies_json = []
    inbound_stats_block = None
    if scope_type == "account":
        ir_rows = data.get("inbound_replies") or []
        for r in ir_rows[:8]:
            inbound_replies_json.append({
                "replier_handle":       "@" + (r.get("replier_handle") or ""),
                "reply_content":        (r.get("reply_content") or "")[:160],
                "reply_likes":          r.get("reply_likes", 0),
                "parent_tweet_id":      r.get("parent_tweet_id"),
                "parent_tweet_excerpt": (r.get("parent_tweet_excerpt") or "")[:120],
            })
        ist = data.get("inbound_stats") or {}
        if ist and ist.get("inbound_replies_received", 0) > 0:
            inbound_stats_block = {
                "inbound_replies_received": ist.get("inbound_replies_received", 0),
                "distinct_inbound_repliers": ist.get("distinct_repliers", 0),
                "inbound_reply_likes_total": ist.get("inbound_reply_likes_total", 0),
            }

    data_json = {
        "scope": cohort_name, "period": period, "accounts_count": len(accounts),
        # NOTE on naming convention: "outbound" = authored by the tracked
        # account(s). "inbound" = others addressing the tracked account(s).
        # See system prompt for the entity model.
        "outbound_totals": {
            "originals_count":     total_posts,
            "replies_count":       total_replies,
            "originals_likes":     total_post_likes,
            "replies_likes":       total_reply_likes,
            "originals_views":     total_post_views,
            "replies_views":       total_reply_views,
            "total_engagement_outbound": total_post_likes + total_reply_likes,
            "avg_likes_per_original": round(total_post_likes / total_posts, 1) if total_posts > 0 else 0,
            "avg_likes_per_outbound_reply": round(total_reply_likes / total_replies, 2) if total_replies > 0 else 0,
        },
        "accounts": accounts_json,
        "top_outbound_tweets": tweets_json,
        "inbound_replies_received_sample": inbound_replies_json,
        "inbound_totals": inbound_stats_block,
        "top_topics": topics_json,
        "topic_leaders": topic_leaders_json,
        # Peer percentiles for the LLM's CONTEXTUALISE rule (Follow-up D.2).
        # Present only when the comparison set has >=5 accounts; otherwise
        # the LLM falls back to the static BENCHMARKS in the system prompt.
        "peer_distribution": data.get("peer_distribution"),
    }
    totals_json = json.dumps(data_json, indent=2)
    personality_context = _build_personality_context(data, scope_type)

    user_prompt = INSIGHTS_USER_PROMPT.format(
        cohort_name=cohort_name, period=period,
        account_count=len(accounts), tweet_count=total_posts + total_replies,
        personality_context=personality_context,
        totals_json=totals_json, schema=JSON_SCHEMA,
    )
    return INSIGHTS_SYSTEM_PROMPT, user_prompt


_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,15})\b")


def _mentions_in(text: str) -> set[str]:
    """Extract @handles from arbitrary text. Lowercase, no leading @."""
    if not text:
        return set()
    return {m.lower() for m in _MENTION_RE.findall(text)}


def _build_allowlist(data, scope_type):
    """Set of {internal_handles, tweet_ids, external_handles} the LLM may reference.

    Two-tier handle classification:
      - internal_handles: tracked accounts + repliers + accounts mentioned in
        any tweet/reply/bio content for the scope. These render as VibeChecx
        deeplinks (/account/X) and are completely safe.
      - external_handles: handles in `cohorts.ecosystem_handles` (curated by
        the user). These render as external x.com links. Allowed but tagged
        as off-data.

    Handles outside both sets are surfaced as advisory ("external references"
    chip in the UI). They are NOT redacted from prose anymore, only tweet_ids
    are still hard-stripped from structured fields because those are factual
    claims that must be grounded.

    Returns (internal_handles, tweet_ids, external_handles).
    """
    internal = set()
    external = set()
    tweet_ids = set()

    if scope_type == "cohort":
        for a in (data.get("accounts") or []):
            uname = a.get("username")
            if uname:
                internal.add(uname.lower())
            internal |= _mentions_in(a.get("bio"))
        for t in (data.get("top_tweets") or []):
            tid = t.get("tweet_id")
            if tid:
                tweet_ids.add(str(tid))
            ru = t.get("reply_to_username")
            if ru:
                internal.add(ru.lower())
            internal |= _mentions_in(t.get("content"))
        # cohort-level ecosystem handles
        for h in (data.get("ecosystem_handles") or []):
            external.add(h.lower().lstrip("@"))
    else:
        acct = data.get("account") or {}
        uname = acct.get("username")
        if uname:
            internal.add(uname.lower())
        internal |= _mentions_in(acct.get("bio"))
        for t in (data.get("tweets") or []):
            tid = t.get("tweet_id")
            if tid:
                tweet_ids.add(str(tid))
            ru = t.get("reply_to_username")
            if ru:
                internal.add(ru.lower())
            internal |= _mentions_in(t.get("content"))
        # Voice samples: parents of outbound replies + content
        for s in (data.get("voice_samples") or []):
            pa = s.get("parent_author")
            if pa:
                internal.add(pa.lower())
            internal |= _mentions_in(s.get("content"))
            internal |= _mentions_in(s.get("parent_content"))
        # Inbound replies: people who replied + their reply text
        for r in (data.get("inbound_replies") or []):
            rh = r.get("replier_handle")
            if rh:
                internal.add(rh.lower().lstrip("@"))
            internal |= _mentions_in(r.get("reply_content"))
            internal |= _mentions_in(r.get("parent_tweet_excerpt"))

    return internal, tweet_ids, external


def _scan_violations(parsed, internal_handles, allow_tweet_ids,
                     external_handles=None):
    """Walk the parsed JSON.  Return ({unknown_handles}, {bad_tweet_ids}).

    Tier 2 semantics:
      - tweet_ids NOT in allow_tweet_ids are hard violations (factual claims
        that must be grounded, these get stripped from structured fields).
      - handles NOT in (internal_handles ∪ external_handles) are advisory:
        returned so the UI can surface them as "external references", but
        NOT used to censor prose. Wrong handle ≠ fabricated metric.
    """
    external_handles = external_handles or set()
    allowed_handles = internal_handles | external_handles
    unknown_handles = set()
    bad_tweet_ids = set()

    def _walk(node, key_hint=None):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, key_hint=k)
        elif isinstance(node, list):
            for item in node:
                _walk(item, key_hint=key_hint)
        elif isinstance(node, str):
            for m in _HANDLE_RE.findall(node):
                if m.lower() not in allowed_handles:
                    unknown_handles.add(m)
            if key_hint == "handle":
                stripped = node.lstrip("@").strip().lower()
                if stripped and stripped not in allowed_handles:
                    unknown_handles.add(node)
            if key_hint == "tweet_id":
                if str(node) not in allow_tweet_ids:
                    bad_tweet_ids.add(str(node))

    _walk(parsed)
    return unknown_handles, bad_tweet_ids


def _strip_violations(parsed, unknown_handles, bad_tweet_ids):
    """Drop structured entries that reference unknown handles in a *handle*
    field or unknown tweet_ids in a *tweet_id* field. PROSE IS NEVER MODIFIED.

    Tier 2 reasoning: handles inside prose may legitimately reference real
    ecosystem entities the user hasn't tracked yet. Stripping them was
    information loss. The UI surfaces them in an advisory chip instead.

    Tweet-ids ARE hard rejected because they always carry a factual metric
    claim, a fabricated id = a fabricated number.
    """
    if not (unknown_handles or bad_tweet_ids):
        return parsed
    unknown_lower = {h.lower().lstrip("@") for h in unknown_handles}

    cleaned = json.loads(json.dumps(parsed))  # deep copy
    # Drop kudos entries whose `handle` field references something completely
    # unknown, crediting a made-up account is bad. Top-performers with a bad
    # tweet_id also drop (the metrics in that entry can't be verified).
    for list_key in ("kudos", "top_performers"):
        items = cleaned.get(list_key)
        if not isinstance(items, list):
            continue
        kept = []
        for it in items:
            if isinstance(it, dict):
                h = (it.get("handle") or "").lstrip("@").strip().lower()
                if h and h in unknown_lower:
                    continue
                tid = it.get("tweet_id")
                if tid and str(tid) in bad_tweet_ids:
                    continue
            kept.append(it)
        cleaned[list_key] = kept
    return cleaned


def _strict_retry_suffix(internal_handles, external_handles, allow_tweet_ids,
                         bad_tweet_ids):
    """Only tweet_ids trigger the strict retry now. Handles are advisory and
    don't warrant burning a second LLM call."""
    allowed_handles = sorted(internal_handles | external_handles)
    return (
        "\n\nSTRICT RETRY: The previous response cited tweet_ids not in the input data.\n"
        f"- Fabricated tweet_ids: {', '.join(sorted(bad_tweet_ids))}\n\n"
        "Re-emit the JSON. tweet_id values in top_performers MUST come from this list:\n"
        f"  {', '.join(sorted(allow_tweet_ids)) or '(none)'}\n"
        "@handles in prose may reference real ecosystem entities; only the "
        "fabricated tweet_ids above are the issue. Return JSON only."
    )


def generate_insights(scope_type, scope_id, period="7d"):
    days = PERIOD_MAP.get(period, 7)
    data = _fetch_cohort_data(scope_id, days) if scope_type == "cohort" else _fetch_account_data(scope_id, days)
    if not data:
        return None, f"{scope_type} {scope_id} not found"

    if scope_type == "cohort" and not data["accounts"]:
        return None, "no accounts in cohort"
    if scope_type == "account" and not data["tweets"]:
        return None, "no tweets found for period"

    system_prompt, user_prompt = _build_prompt(data, period, scope_type)
    grok, deepseek, openai_p = make_grok(), make_deepseek(), make_openai()
    if not grok and not deepseek and not openai_p:
        return None, "no API keys configured"

    internal_handles, allow_tweet_ids, external_handles = _build_allowlist(data, scope_type)
    # Default order: DeepSeek primary, OpenAI 4o-mini second, Grok last.
    # Admin can override via the `data/primary_provider` file (written from /admin).
    _primary_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "primary_provider")
    _primary = ""
    try:
        if os.path.exists(_primary_file):
            _primary = open(_primary_file).read().strip().lower()
    except Exception:
        pass
    _orders = {
        "openai":   [openai_p, deepseek, grok],
        "grok":     [grok, deepseek, openai_p],
        "deepseek": [deepseek, openai_p, grok],
    }
    providers = [p for p in _orders.get(_primary, [deepseek, openai_p, grok]) if p]
    last_error = None
    for provider in providers:
        try:
            text = provider.call(user_prompt, system_prompt=system_prompt)
            parsed = _parse_response(text)
            if parsed is None:
                raise ValueError(f"failed to parse: {text[:200]}")

            unknown_handles, bad_tweet_ids = _scan_violations(
                parsed, internal_handles, allow_tweet_ids, external_handles,
            )

            # Only fabricated tweet_ids trigger the strict retry. Unknown
            # handles are advisory, surfaced as an info chip, never censored.
            if bad_tweet_ids:
                logger.warning(
                    "%s fabricated %d tweet_ids, retrying strict",
                    provider.name, len(bad_tweet_ids),
                )
                retry_user_prompt = user_prompt + _strict_retry_suffix(
                    internal_handles, external_handles,
                    allow_tweet_ids, bad_tweet_ids,
                )
                retry_text = provider.call(retry_user_prompt, system_prompt=system_prompt)
                retry_parsed = _parse_response(retry_text)
                if retry_parsed is not None:
                    retry_unknown, retry_bad_t = _scan_violations(
                        retry_parsed, internal_handles, allow_tweet_ids, external_handles,
                    )
                    if not retry_bad_t:
                        logger.info("Retry resolved tweet_ids via %s", provider.name)
                        parsed = retry_parsed
                        unknown_handles = retry_unknown
                        bad_tweet_ids = set()
                    else:
                        # Retry still has fabricated ids, strip them.
                        parsed = _strip_violations(retry_parsed, retry_unknown, retry_bad_t)
                        unknown_handles = retry_unknown
                        bad_tweet_ids = retry_bad_t
                else:
                    parsed = _strip_violations(parsed, unknown_handles, bad_tweet_ids)

            # Attach metadata: any unknown handles get surfaced as info, not
            # as a censorship warning. The `external_handles_in_prose` list
            # is also used by the renderer to deeplink them to x.com.
            if unknown_handles or bad_tweet_ids:
                parsed["_warnings"] = {
                    "external_handles_in_prose": sorted(unknown_handles),
                    "removed_tweet_ids":         sorted(bad_tweet_ids),
                }
            # Pass the handle sets through so the renderer can deeplink
            # without re-deriving them.
            parsed["_handle_sets"] = {
                "internal": sorted(internal_handles),
                "external": sorted(external_handles | unknown_handles),
            }
            provider_label = (
                provider.name + "+warnings" if bad_tweet_ids else provider.name
            )
            logger.info(
                "Insights via %s for %s %s (period=%s), %d unknown handles, %d fabricated tweet_ids",
                provider.name, scope_type, scope_id, period,
                len(unknown_handles), len(bad_tweet_ids),
            )
            return parsed, provider_label
        except Exception as e:
            last_error = str(e)
            logger.warning("%s failed: %s", provider.name, last_error)
            continue
    return None, f"all providers failed: {last_error}"


def _ensure_timely_angles_table():
    """Create the timely_angles_cache table on first use, no migration file needed."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS timely_angles_cache (
            scope_type  TEXT NOT NULL,
            scope_id    INTEGER NOT NULL,
            period      TEXT NOT NULL,
            angles      TEXT,          -- JSON array of {headline, context, why_it_fits}
            generated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (scope_type, scope_id, period)
        )
    """)
    conn.commit()
    conn.close()


def generate_timely_angles(scope_type: str, scope_id: int, period: str, insight: dict) -> list | None:
    """Fire a Grok web-search call to surface timely content angles grounded in
    real current events. Requires Grok API key; silently returns None on any error."""
    thesis = (insight.get("strategic_thesis") or "").strip()
    topics = [t.get("topic") for t in (insight.get("top_topics") or [])[:5] if t.get("topic")]
    if not thesis or not topics:
        return None

    grok = make_grok()
    if not grok:
        return None

    topics_str = ", ".join(topics)
    user_prompt = (
        f"Strategic positioning of the account being analysed:\n\"{thesis}\"\n\n"
        f"Top topics they cover: {topics_str}\n\n"
        f"Use your web search to find 2-3 real, recent developments (news, announcements, "
        f"events, or community moments) from the past {period} that intersect with these topics "
        f"and create a genuine content opening for an account with this positioning.\n\n"
        f"Return ONLY a JSON array. Each item: "
        f"{{\"headline\": \"one-line angle\", \"context\": \"what specifically happened (source/date if known)\", "
        f"\"why_it_fits\": \"why this account's positioning makes them the right voice on this angle\"}}\n\n"
        f"Strict rules: only include angles grounded in verifiable events you actually found. "
        f"If you cannot find strong real-world hooks, return an empty array []. "
        f"No hallucinated events. Return JSON only, no markdown."
    )

    try:
        resp = grok.client.chat.completions.create(
            model=grok.model,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=800,
            temperature=0,
            search_parameters={"mode": "auto"},
        )
        text = resp.choices[0].message.content.strip()
        parsed = _parse_response(text)
        if isinstance(parsed, list) and parsed:
            return parsed[:3]
        return None
    except Exception as e:
        logger.warning("Timely angles search failed: %s", e)
        return None


def cache_timely_angles(scope_type: str, scope_id: int, period: str, angles: list | None):
    _ensure_timely_angles_table()
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO timely_angles_cache (scope_type, scope_id, period, angles, generated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (scope_type, scope_id, period)
        DO UPDATE SET angles=EXCLUDED.angles, generated_at=NOW()
    """, (scope_type, scope_id, period, json.dumps(angles) if angles is not None else None))
    conn.commit()
    conn.close()


def get_cached_timely_angles(scope_type: str, scope_id: int, period: str) -> tuple[list | None, bool]:
    """Returns (angles_or_None, exists_in_cache). exists_in_cache=True means
    the search already ran (angles may be None/empty if it found nothing)."""
    try:
        _ensure_timely_angles_table()
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            SELECT angles FROM timely_angles_cache
            WHERE scope_type=%s AND scope_id=%s AND period=%s
        """, (scope_type, scope_id, period))
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None, False
        raw = row[0]
        angles = json.loads(raw) if raw else None
        return angles, True
    except Exception:
        return None, False


def _fire_timely_angles_bg(scope_type: str, scope_id: int, period: str, insight: dict):
    """Spawn a daemon thread to run the timely angles search without blocking the
    insight response. Stores result in timely_angles_cache when done."""
    import threading

    def _run():
        try:
            angles = generate_timely_angles(scope_type, scope_id, period, insight)
            cache_timely_angles(scope_type, scope_id, period, angles)
            logger.info("Timely angles cached for %s %s %s (%d found)",
                        scope_type, scope_id, period, len(angles) if angles else 0)
        except Exception as e:
            logger.warning("Timely angles bg thread failed: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def cached_insights(scope_type, scope_id, period="7d",
                    max_age_minutes: int | None = None, force: bool = False,
                    generate_if_missing: bool = False):
    """Read-through cache for a (scope, period) insight.

    TOKEN-SAFETY CONTRACT (do not break):
      Defaults are READ-ONLY. With default args this function never calls an
      LLM, on a miss it returns (None, None, False, None) so callers render
      a "Generate" button instead. This is the safe default because page
      renders / tab switches / polls call this function freely, and an
      accidental LLM call here would burn tokens on every visitor.

      Only two opt-ins fire the LLM:
      - `force=True`, explicit user click (the POST /generate-insights
        endpoints). Ignores cache, generates fresh, replaces the prior row.
      - `generate_if_missing=True`, back-compat opt-in for CLI / batch jobs.
        DO NOT pass this from a request handler.

    Cache invalidation:
      - A scrape complete (coordinator) invalidates rows via
        `lib/storage.invalidate_insights_cache`.
      - `max_age_minutes` is preserved as an optional safety net but defaults
        to None (no TTL). Callers who want a TTL pass it explicitly.

    Returns (insights, provider, from_cache, age_minutes_or_None).
    """
    if not force:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT insights, provider, generated_at FROM insights_cache "
                "WHERE scope_type=%s AND scope_id=%s AND period=%s "
                "ORDER BY generated_at DESC LIMIT 1",
                (scope_type, scope_id, period),
            )
            cached = cur.fetchone()
            if cached:
                age = (
                    datetime.now(timezone.utc)
                    - cached["generated_at"].replace(tzinfo=timezone.utc)
                ).total_seconds() / 60
                if max_age_minutes is None or age < max_age_minutes:
                    return cached["insights"], cached["provider"], True, round(age)

    if not generate_if_missing and not force:
        return None, None, False, None

    insights, provider_or_error = generate_insights(scope_type, scope_id, period)
    if insights:
        with _conn() as conn, conn.cursor() as cur:
            # Replace the prior cache row so we don't accumulate orphans on
            # explicit regenerations.
            cur.execute(
                "DELETE FROM insights_cache "
                "WHERE scope_type=%s AND scope_id=%s AND period=%s",
                (scope_type, scope_id, period),
            )
            cur.execute(
                "INSERT INTO insights_cache (scope_type,scope_id,period,generated_at,insights,provider) "
                "VALUES (%s,%s,%s,NOW(),%s,%s)",
                (scope_type, scope_id, period, json.dumps(insights), provider_or_error),
            )
        # Invalidate the prior timely angles so we search fresh on next view.
        cache_timely_angles(scope_type, scope_id, period, None)
        # Fire the search in a background thread, doesn't block the response.
        _fire_timely_angles_bg(scope_type, scope_id, period, insights)
        return insights, provider_or_error, False, 0
    return None, provider_or_error, False, None


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("VIBECHECX_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s insights: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("scope_type", choices=["cohort", "account"])
    parser.add_argument("scope_id", type=int)
    parser.add_argument("--period", default="7d", choices=list(PERIOD_MAP.keys()))
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    if args.no_cache:
        r, p = generate_insights(args.scope_type, args.scope_id, args.period)
        print(json.dumps({"insights": r, "provider": p}, indent=2, default=str))
    else:
        r, p, c, a = cached_insights(args.scope_type, args.scope_id, args.period,
                                     generate_if_missing=True)
        print(json.dumps({"insights": r, "provider": p, "from_cache": c, "age": a}, indent=2, default=str))
