"""
orchestrator.py — LLM call 1.

Parses a natural language question into a QueryObject by filling typed slots
from the catalog vocabulary. The LLM NEVER writes SQL here — only fills fields
whose values are constrained to what exists in the catalog.

Steps:
1. Build a compact catalog vocabulary (event names, filterable columns + values,
   breakdown dimensions, pre-built metric ids + optional sql hints, glossary,
   conventions)
2. Send one LLM call: question → JSON slots
3. Parse + validate into QueryObject
"""

import json
import os
import re
from datetime import date
from typing import Optional, Union

from core.infra.llm import call_llm, make_llm_client, resolve_model, LLM_STRONG
from core.infra.tracer import track
from core.pipeline.activation_window import (
    apply_activation_window_from_prompt,
    apply_retention_window_from_prompt,
)
from core.pipeline.catalog_vocab import (
    extract_dimension_filters_from_question,
    merge_filter_vocab,
)

# ── Time-signal detector ──────────────────────────────────────────────────────

_EXPLICIT_TIME_RE = re.compile(
    r"\b("
    r"january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
    r"|last\s+\d+\s+days?"
    r"|past\s+\d+\s+days?"
    r"|last\s+\d+\s+weeks?"
    r"|past\s+\d+\s+weeks?"
    r"|last\s+\d+\s+months?"
    r"|past\s+\d+\s+months?"
    r"|last\s+week|this\s+week"
    r"|last\s+month|this\s+month"
    r"|last\s+year|this\s+year"
    r"|ytd|year.to.date"
    r"|q[1-4]|\b\d{4}\b"
    r"|recently|lately|past\s+few\s+days?"
    r"|momn?|woww?|month\.?\s+on\s+month|month\.?\s+over\s+month|week\.?\s+over\s+week"
    r"|monthly\s+trend|weekly\s+trend|show\s+trend|share\s+trend|compare\s+over\s+time"
    r")\b",
    re.IGNORECASE,
)


def _question_has_time(question: str) -> bool:
    """True when the question contains an explicit time reference."""
    return bool(_EXPLICIT_TIME_RE.search(question))


from core.sql.query_object import QueryObject
from core.semantic.semantic_index import get_or_build_index, SemanticRetrievalResult

# ── Prompt budget (orchestrator context) ───────────────────────────────────────
# Keeps any single deployment from flooding the system prompt; values are generic.
_PROMPT_METRIC_SQL_MAX = 900
_PROMPT_GLOSSARY_SQL_MAX = 650
_PROMPT_GLOSSARY_DESC_MAX = 420
_PROMPT_GLOSSARY_TERM_MAX = 120
_PROMPT_MAX_GLOSSARY_TERMS = 32
_PROMPT_MAX_CONVENTION_LINES = 14
_PROMPT_CONVENTION_LINE_MAX = 220


def _truncate_prompt_text(s: Optional[str], max_len: int) -> str:
    """Single-line-ish text cap for orchestrator injection (any industry)."""
    if not s or not isinstance(s, str):
        return ""
    t = " ".join(s.split())  # collapse runs of whitespace for compactness
    if len(t) <= max_len:
        return t
    return t[: max_len - 18].rstrip() + " …[truncated]"


def _biz_glossary_raw(biz: dict) -> list:
    """Glossary may live at business_context root or under exclusions (catalog variants)."""
    raw = biz.get("glossary")
    if isinstance(raw, list):
        return raw
    excl = biz.get("exclusions")
    if isinstance(excl, dict):
        raw2 = excl.get("glossary")
        if isinstance(raw2, list):
            return raw2
    return []


def _biz_conventions_raw(biz: dict) -> Optional[Union[list, str]]:
    raw = biz.get("conventions")
    if isinstance(raw, list) or (isinstance(raw, str) and raw.strip()):
        return raw
    excl = biz.get("exclusions")
    if isinstance(excl, dict):
        raw2 = excl.get("conventions")
        if isinstance(raw2, list) or (isinstance(raw2, str) and str(raw2).strip()):
            return raw2
    return None


def _extract_glossary_for_prompt(catalog: dict) -> list[dict]:
    """
    Optional glossary[] — term, description, optional sql.
    Stays schema-agnostic: only passes through fields present on each entry.
    """
    biz = (catalog or {}).get("__business_context__", {}) or {}
    raw = _biz_glossary_raw(biz)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:_PROMPT_MAX_GLOSSARY_TERMS]:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        row: dict = {
            "term": _truncate_prompt_text(term, _PROMPT_GLOSSARY_TERM_MAX),
            "description": _truncate_prompt_text(
                str(item.get("description") or ""),
                _PROMPT_GLOSSARY_DESC_MAX,
            ),
        }
        dc = item.get("domain_category")
        if dc:
            row["domain_category"] = _truncate_prompt_text(str(dc), 96)
        rs = item.get("review_status")
        if rs:
            row["review_status"] = _truncate_prompt_text(str(rs), 32)
        sq = (item.get("sql") or "").strip()
        if sq:
            row["sql"] = _truncate_prompt_text(sq, _PROMPT_GLOSSARY_SQL_MAX)
        out.append(row)
    return out


def _extract_conventions_for_prompt(catalog: dict) -> list[str]:
    """Optional conventions — list of short deployment notes (several catalog layouts)."""
    biz = (catalog or {}).get("__business_context__", {}) or {}
    raw = _biz_conventions_raw(biz)
    lines: list[str] = []
    if isinstance(raw, list):
        lines = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        lines = [raw.strip()]
    return [
        _truncate_prompt_text(x, _PROMPT_CONVENTION_LINE_MAX)
        for x in lines[:_PROMPT_MAX_CONVENTION_LINES]
    ]


# ── Vocabulary builder ────────────────────────────────────────────────────────

def build_vocab(catalog: dict, sampled_values: dict[str, dict[str, list]]) -> dict:
    """
    Returns a dict with catalog vocabulary for the orchestrator prompt.

    sampled_values: {table_name: {col_name: [val1, val2, ...]}}
    Already computed by chat.py at startup — passed in to avoid re-querying DB.

    Metrics may include optional ``sql`` (truncated): suggested_metrics and
    custom_events surface warehouse-oriented definitions so NL can align with
    composite cohorts without hard-coding any one product vertical.
    """
    event_names = []
    metrics = []
    funnels = []
    dimension_cols = []   # good candidates for breakdown
    filter_cols = {}      # col → list of values
    dim_value_hints = {}  # dim col -> representative values

    for tname, tdata in catalog.items():
        if tname.startswith("__"):
            continue

        # Event names
        for ename in tdata.get("event_semantics", {}).keys():
            event_names.append(ename)

        # Pre-built metrics (optional sql from catalog editor / semantic layer)
        for m in tdata.get("suggested_metrics", []):
            entry = {
                "id":          m.get("id", ""),
                "name":        m.get("name", ""),
                "description": m.get("description", ""),
            }
            mtype = str(m.get("type") or "").strip()
            if mtype:
                entry["type"] = mtype
            mid = str(m.get("id") or "").lower()
            if mtype == "retention" or "retention" in mid:
                entry["route_as"] = "retention"
            ms = (m.get("sql") or m.get("sql_hint") or "").strip()
            if ms:
                entry["sql"] = _truncate_prompt_text(ms, _PROMPT_METRIC_SQL_MAX)
            metrics.append(entry)

        # Saved funnels curated in catalog editor
        for f in tdata.get("saved_funnels", []):
            if not f.get("name") or not f.get("steps"):
                continue
            funnels.append({
                "name": f.get("name", ""),
                "description": f.get("description", ""),
                "steps": f.get("steps", []),
            })

    # Filter columns: catalog value_meanings + DB samples (long-term vocab source of truth)
    filter_cols, dim_value_hints, dimension_cols = merge_filter_vocab(
        catalog, sampled_values,
    )

    # Business-context custom events (saved in catalog editor) are surfaced as
    # pseudo-metrics so NL queries can refer to them by name.
    biz = catalog.get("__business_context__", {}) or {}
    # ce_column_conditions: col → list of (op, ce_name, ce_desc) for IS NOT NULL / IS NULL
    # conditions derived from CEs. Surfaced in the prompt so the LLM knows which phrases
    # map to column-condition concepts rather than literal sampled values.
    ce_column_conditions: dict[str, list] = {}
    for ce in biz.get("custom_events", []) or []:
        ce_name = (ce.get("name") or "").strip()
        ce_sql = (ce.get("sql") or "").strip()
        if not ce_name or not ce_sql:
            continue
        ce_id = f"ce_{re.sub(r'[^a-z0-9_]+', '_', ce_name.lower()).strip('_')}"
        metrics.append({
            "id": ce_id,
            "name": ce_name.replace("_", " ").title(),
            "description": ce.get("description", f"Custom event: {ce_name}"),
            "sql": _truncate_prompt_text(ce_sql, _PROMPT_METRIC_SQL_MAX),
        })
        # Collect IS NOT NULL / IS NULL conditions from builder_definition
        ce_label = ce_name.replace("_", " ").title()
        ce_desc = ce.get("description") or ""
        for group in ((ce.get("builder_definition") or {}).get("groups") or []):
            for rule in (group.get("filters") or []):
                field = str(rule.get("field") or "").strip().lower()
                op = str(rule.get("op") or "").strip().upper()
                if field and op in ("IS NOT NULL", "IS NULL"):
                    ce_column_conditions.setdefault(field, []).append(
                        (op, ce_label, ce_desc)
                    )

    glossary = _extract_glossary_for_prompt(catalog)
    conventions = _extract_conventions_for_prompt(catalog)

    return {
        "event_names":          sorted(set(event_names)),
        "metrics":              metrics,
        "funnels":              funnels,
        "filter_cols":          filter_cols,
        "dimension_cols":       sorted(set(dimension_cols)),
        "dim_value_hints":      dim_value_hints,
        "ce_column_conditions": ce_column_conditions,
        "glossary":             glossary,
        "conventions":          conventions,
    }


# ── Analysis type registry ───────────────────────────────────────────────────
# Each entry: description (prompt one-liner), keywords (scoring), rules (type-specific
# rules injected only when selected), always (always inject regardless of query).

_ANALYSIS_TYPES: dict[str, dict] = {
    "metric": {
        "description": '- "metric"            → trend over time for one event (e.g. "daily active users", "app opens per day")',
        "keywords": ["trend", "daily", "monthly", "weekly", "how many", "count", "over time", "per day", "per month", "active users"],
        "rules": None,
        "always": True,
    },
    "segment": {
        "description": '- "segment"           → metric broken down by ONE dimension (e.g. "DAU by platform", "transactions by city")',
        "keywords": ["by platform", "by city", "by channel", "breakdown", "split by", "per city", "per platform", "broken down"],
        "rules": (
            '- METRIC vs SEGMENT disambiguation — most common routing error:\n'
            '  • Use analysis_type="metric" when the user asks for a TREND or count over time with a WHERE-style filter. A filter value does NOT make it a segment — put it in `filters`, leave breakdown=null.\n'
            '  • Use analysis_type="segment" ONLY when the user explicitly asks to SPLIT or COMPARE across different values of a dimension. segment always requires a non-null breakdown.\n'
            '  • NEVER set analysis_type="segment" with breakdown=null — invalid. When in doubt use metric.\n'
            '- For "by X" or "per X" or "broken down by X": use analysis_type=segment and set breakdown.'
        ),
        "always": True,
    },
    "demographic_breakdown": {
        "description": '- "demographic_breakdown" → user profile across ALL demographic dimensions at once (e.g. "user demographics", "split users by demographics", "who are these users", "user distribution across age/gender/city")',
        "keywords": ["demographics", "demographic", "who are these", "user distribution", "age gender", "user profile", "all demographics"],
        "rules": None,
    },
    "funnel": {
        "description": '- "funnel"            → ordered conversion steps (e.g. "onboarding funnel", "registration drop-off")',
        "keywords": ["funnel", "step", "conversion", "drop-off", "dropoff", "drop off", "onboarding flow", "signup flow"],
        "rules": '- If the user asks for a funnel by name and it matches CURATED FUNNELS, set analysis_type="funnel" and use the exact listed steps in funnel_steps.',
    },
    "funnel_compare": {
        "description": '- "funnel_compare"    → same funnel steps but compare user reach **recent half vs prior half** of the window (e.g. "did onboarding worsen vs last month"). Only use when the subject is a **funnel**. MoM/WoW on a metric or retention → use "metric" or "retention", not funnel_compare.',
        "keywords": ["compare funnel", "funnel vs", "funnel worsen", "funnel improve", "funnel better", "funnel worse", "funnel prior", "funnel last month"],
        "rules": '- If the user asks to **compare** the same funnel across time (MoM, vs last period, "did the funnel get worse", "funnel now vs before"), use analysis_type="funnel_compare" with the same funnel_steps as a normal funnel.',
    },
    "funnel_property_drilldown": {
        "description": '- "funnel_property_drilldown" → at the first funnel drop-off step, break down converters vs droppers by a dimension (e.g. "why do users drop at step 2 — breakdown by platform"). Set funnel_steps to the relevant steps and breakdown to the dimension.',
        "keywords": ["funnel drop", "drop-off by", "why drop", "funnel breakdown by", "funnel drilldown", "which platform funnel", "worst funnel conversion"],
        "rules": '- For funnel_property_drilldown: set funnel_steps to the 2+ steps; set breakdown to the property column to diagnose (e.g. "platform", "city"). Use when user asks "why does the funnel drop" + mentions a dimension.',
    },
    "journey": {
        "description": '- "journey"           → **path + flow**: after an anchor event, what users do next within 7 days (e.g. "what happens after signup", "user journey from purchase"). Set event=anchor; optional event_b=goal next event.',
        "keywords": ["journey", "path", "what happens after", "what do users do", "user paths", "flow after", "next steps after"],
        "rules": '- If the user asks for **paths / journey / what happens after** an event, use analysis_type="journey" with event=the anchor; set event_b only when they name a specific "goal" next event.',
    },
    "retention": {
        "description": '- "retention"         → cohort return rate with D1/D7/D30 survival curve (e.g. "D7 retention", "how many users come back", "show survival curve")',
        "keywords": ["retention", "d7", "d30", "d1", "come back", "return", "survival", "cohort return", "retained", "day 7", "day 30"],
        "rules": (
            '- For retention: retention_window_days = **first N days** after cohort entry — '
            'did the user return at least once in days 0..N-1 after their cohort anchor (NOT "days N..2N"). '
            'retention_window_days is ALWAYS in whole days — convert hours before setting: '
            '"24hr retention"→1, "48hr retention"→2, sub-day→1 (same rule as activation). '
            'When the user says "for 14 days", "first 14 days", or "14 day retention", set retention_window_days=14. '
            'For "MOM"/"month over month" retention with an explicit N-day window: analysis_type=retention, '
            'time_granularity=month, time_range_days≈180, retention_window_days=N (one retention_pct per cohort month). '
            'Scale defaults when no explicit N: time_range_days ≤ 14 → 1, ≤ 60 → 7, > 60 → 30. '
            'When the user names "D1"/"Day 1" use 1; "D7"/"Day 7" use 7; "D30"/"Day 30" use 30; "D0"/"Day 0" use 0.\n'
            '- For activation rate with an explicit window ("7-day activation", "D30 activation", "activate within 1 day", "24hr conversion", "48hr conversion"): '
            'set metric_id=<activation metric> AND activation_window_days=<N in whole days> — ALWAYS convert hours to days first (24hr→1, 48hr→2, sub-day→1). '
            'Use activation_window_days for "7 day activation", "D7 activation", "7-day activation", hour-based windows — NOT retention_window_days. '
            '"D0 activation" / "same-day activation" / "day-0 activation" → activation_window_days=0 '
            '(signals multi-window: compiler shows all catalog D-windows — D1/D7/D14/D30 — as a cohort trend). '
            'Without a window ("activation rate", "MOM activation"): set activation_window_days=null (same multi-window behavior).\n'
            '- For retention with no explicit event: check PRE-BUILT METRICS for metrics of type "retention" and set metric_id '
            'to the highest-confidence approved one. Only fall back to a representative session or core-action event if no catalog retention metric exists.'
        ),
    },
    "behavioral_cohort": {
        "description": '- "behavioral_cohort" → users who did event_a but NOT event_b (e.g. "signed up but never converted", "registered but never completed setup")',
        "keywords": ["but not", "without transact", "never converted", "signed up but", "registered but", "overlap", "who also", "and also did"],
        "rules": (
            '- For behavioral_cohort (single window): event = activity they DID, event_b = the second activity to check.\n'
            '  - metric_variant=null (default): user wants overlap count — "how many did A AND also did B".\n'
            '  - metric_variant="anti_cohort": user says "not/no/without/never/excluding B" → ONLY users who did A but NOT B.\n'
            '- For behavioral_cohort **cross-period overlap** (same event, two calendar periods): e.g. "Of users who transacted in March, how many also transacted in February?" → '
            'set event and event_b to the same event; date_from/date_to = the cohort window (March); secondary_date_from/secondary_date_to = the other window (February).\n'
            '- TWO COHORTS / POPULATIONS (not calendar periods): When the question compares two named user populations using "vs", "versus", "compare … vs …": '
            'use analysis_type="metric" with breakdown=null. The resolver routes these to custom_split via __business_context__ custom_events.'
        ),
    },
    "time_between": {
        "description": '- "time_between"      → how long from event_a to event_b (e.g. "how long does onboarding take", "time to first transaction")',
        "keywords": ["how long", "time to", "time between", "duration", "how many days to", "days to complete", "takes to"],
        "rules": '- For time_between: event = the EARLIER/START event (the trigger), event_b = the LATER/COMPLETION event (the outcome). The start event must come BEFORE the end event chronologically.',
    },
    "user_lifecycle": {
        "description": '- "user_lifecycle"    → classify users into lifecycle stages: New · Casual · Engaged · Power User · At Risk · Churned (e.g. "where are users in their lifecycle", "how many users are churned vs active", "user health breakdown"). Set event = the core action event. Set xyz_axis1=null.',
        "keywords": ["lifecycle", "lifecycle stages", "churned vs", "active vs churned", "user health", "at risk", "lifecycle distribution", "where are users in"],
        "rules": '- For user_lifecycle: set event = the most representative core-action event in the catalog. Do NOT set breakdown or metric_id.',
    },
    "stickiness": {
        "description": '- "stickiness"        → DAU/WAU/MAU stickiness ratios + power user frequency distribution (e.g. "how sticky is the product", "DAU/MAU ratio", "L7/L28", "how often do users come back"). Set event = the core action event.',
        "keywords": ["sticky", "stickiness", "dau/mau", "l7/l28", "dau/wau", "engagement depth", "how often come back"],
        "rules": '- For stickiness: set event = the core engagement event. Leave breakdown and metric_id null.',
    },
    "xyz_matrix": {
        "description": '- "xyz_matrix"        → 3-axis cohort table: cohort_month × dimension × metric (e.g. "cohort analysis by platform", "retention matrix by city"). Set event = core action, xyz_axis1 = the second dimension. NEVER use xyz_matrix when user asks to "show X and Y together" — use analysis_type="metric" instead.',
        "keywords": ["cohort matrix", "cohort analysis by", "cohort by platform", "cohort by city", "retention matrix", "cohort × platform"],
        "rules": '- For xyz_matrix: set event = core action, xyz_axis1 = the second dimension (from BREAKDOWN DIMENSIONS). If user doesn\'t name a dimension, default xyz_axis1 to "platform".',
    },
    "same_month_anchor": {
        "description": '- "same_month_anchor" → among users with **activity** in the window, split by whether **calendar month of first-ever event_b** equals **calendar month of first qualifying activity**. event_b = lifecycle anchor (e.g. signup_completed). Set breakdown=null. Not for marketing channel splits.',
        "keywords": ["same month", "onboarded in same month", "same calendar month", "onboarding month", "split by whether onboarded", "calendar month of signup"],
        "rules": (
            '- LIFECYCLE TIMING (onboarding month vs activity month) — not the same as acquisition_cohort:\n'
            '  Phrases like "onboarded in the same month as", "same month onboard vs others" → analysis_type="same_month_anchor" with '
            'event = in-window activity, event_b = lifecycle anchor from AVAILABLE EVENTS. Set breakdown=null. '
            'Never use breakdown="acquisition_cohort" for this.\n'
            '- For same_month_anchor: (1) composite PRE-BUILT METRIC → set metric_id and event. (2) single primitive stream → metric_id=null, event = that stream.\n'
            '- If anchor/activity events are not in AVAILABLE EVENTS, return analysis_type="clarify".\n'
            '  Example clarify_message: "The requested split compares onboarding month to activity month. Which two events should we use for the activity and the onboarding anchor?"'
        ),
    },
    "diagnose": {
        "description": '- "diagnose"          → why did metric X drop or spike (e.g. "why did DAU drop", "what caused the spike in transactions")',
        "keywords": ["why did", "why has", "what caused", "root cause", "explain the decline", "explain the growth", "went down", "went up", "decreased", "increased unexpectedly", "cause of"],
        "rules": (
            '- For diagnose: set event to the base metric event and time_range_days=60. '
            'IMPORTANT: if follow-up that does NOT name a new event, inherit the event from the most recent turn.\n'
            '- For diagnose asking about a SPECIFIC MONTH (e.g. "in March", "last month", "why did X increase in February"): '
            'set diagnose_period_end to the last day of that month as YYYY-MM-DD, and time_range_days=60.\n'
            '- For diagnose comparing **two calendar months** (e.g. "March vs January"): set time_granularity="month", '
            'date_from = first day of earlier month, date_to = exclusive first day after later month. '
            'Still set diagnose_period_end to last day of later month.\n'
            '- For diagnose with no month reference: set diagnose_period_end=null (uses today as reference point).'
        ),
    },
    "forecast": {
        "description": '- "forecast"          → project / predict future trend based on historical data (e.g. "predict DAU next 2 weeks", "forecast transactions", "project growth")',
        "keywords": ["predict", "forecast", "project", "future", "next week", "next month", "next 2 weeks", "will happen", "expected to"],
        "rules": None,
    },
    "clarify": {
        "description": '- "clarify"           → question is too ambiguous to answer without more info',
        "keywords": [],
        "rules": '- AMBIGUITY RULE: if the requested split/filter term can plausibly map to multiple dimensions (or to both a dimension and a value under another dimension), return analysis_type="clarify" and ask a short disambiguation question listing the options. Do not guess.',
        "always": True,
    },
    "out_of_scope": {
        "description": (
            '- "out_of_scope"      → cannot be answered with event data: (a) operational requests (sending emails, ML training, data export, etc.), '
            'OR (b) the user names a specific product screen/feature/button/action that does NOT exist in AVAILABLE EVENTS or PRE-BUILT METRICS. '
            'NEVER substitute a catalog event that is semantically adjacent but different. '
            'DECOMPOSE BEFORE REJECTING: if the phrase is a compound of a qualifier (channel, geo, tier, segment, etc.) '
            'plus a metric/event name that IS in the catalog, do NOT use out_of_scope — resolve it as '
            'metric_id=[matched metric] + filters=[qualifier]. Only use out_of_scope when NO component of '
            'the phrase matches any metric or event, even after decomposition. '
            'Set clarify_message to: "I don\'t have [user\'s exact term] tracked in this catalog. Events available include: [list up to 5 real event names from AVAILABLE EVENTS]."'
        ),
        "keywords": [],
        "rules": None,
        "always": True,
    },
    "multi_intent": {
        "description": (
            '- "multi_intent"      → the user asks to compare or simultaneously display TWO OR MORE distinct named catalog metrics or rates (e.g. "compare activation rate and churn rate", "show DAU and retention together"). '
            'Use ONLY when two separate pre-built metric names both appear in the question. Do NOT use for user cohort comparisons like "active users vs paying users" — those are custom_split.'
        ),
        "keywords": [],
        "rules": None,
        "always": True,
    },
}

_ALWAYS_INCLUDE_TYPES = {t for t, m in _ANALYSIS_TYPES.items() if m.get("always")}
# Ordered list for deterministic prompt output
_TYPE_ORDER = [
    "metric", "segment", "demographic_breakdown", "funnel", "funnel_compare",
    "funnel_property_drilldown", "journey", "retention", "behavioral_cohort",
    "time_between", "user_lifecycle", "stickiness", "xyz_matrix", "same_month_anchor",
    "diagnose", "forecast", "clarify", "out_of_scope", "multi_intent",
]


def _score_analysis_types(question: str) -> list[tuple[str, int]]:
    """Score each non-always analysis type by keyword overlap with the question."""
    q_lower = question.lower()
    scores = []
    for atype, meta in _ANALYSIS_TYPES.items():
        if meta.get("always"):
            continue
        score = sum(1 for kw in meta.get("keywords", []) if kw in q_lower)
        scores.append((atype, score))
    return sorted(scores, key=lambda x: (-x[1], x[0]))


def _build_dynamic_types(question: str, top_n: int = 5) -> tuple[str, str]:
    """
    Returns (descriptions_block, rules_block) injecting only the most relevant
    analysis type details for this question.

    Always-included types always appear in full. Non-selected types get a one-liner
    stub so the LLM knows they exist without consuming their full description tokens.
    """
    scored = _score_analysis_types(question)
    top_scored = {t for t, s in scored[:top_n] if s > 0}
    selected = _ALWAYS_INCLUDE_TYPES | top_scored

    desc_lines: list[str] = []
    rules: list[str] = []
    stubs: list[str] = []

    for atype in _TYPE_ORDER:
        meta = _ANALYSIS_TYPES.get(atype)
        if not meta:
            continue
        if atype in selected:
            desc_lines.append(meta["description"])
            if meta.get("rules"):
                rules.append(meta["rules"])
        else:
            stubs.append(f'"{atype}"')

    if stubs:
        desc_lines.append(
            f"- Other types (use if question clearly matches — full details omitted): {', '.join(stubs)}"
        )

    return "\n".join(desc_lines), "\n".join(rules)


# ── Orchestrator prompt ───────────────────────────────────────────────────────

_SYSTEM = """You are an analytics query classifier. Parse the user question into a
structured QueryObject. Use ONLY the vocabulary provided below — never invent event
names, column names, or values.

{hypothesis_block}

TODAY'S DATE: {today}

REFERENCE DATA (any industry — use only to align user phrases with allowed JSON slots):
- Metrics below may include optional **"sql"**: a warehouse-oriented predicate or expression from *this* catalog showing how that metric/cohort is encoded (often on the events table). Use it to resolve synonyms and composite definitions; your reply is still **only** the JSON schema at the end — never paste or invent SQL.
- **glossary**: optional business terms; optional **sql** per term when the catalog author provided one.
- **conventions**: optional short notes for this deployment (defaults, currency, etc.). If empty arrays, ignore.

BUSINESS GLOSSARY (optional catalog entries; may be empty):
{glossary_json}

DEPLOYMENT CONVENTIONS (optional; may be empty):
{conventions_json}

ANALYSIS TYPES:
{types_descriptions}

PRE-BUILT METRICS & COMPOSITE COHORTS — use **metric_id** only per METRIC_ID MATCHING RULES below. When an entry includes **sql**, treat it as the authoritative encoding hint for that id/name (e.g. OR across event types, status filters).
{metrics_json}

CURATED FUNNELS (use when user asks by funnel name):
{funnels_json}

METRIC_ID MATCHING RULES — be conservative:
- ONLY set metric_id when the user explicitly asks for a NAMED RATE, RATIO, or COMPOSITE metric by name:
    ✓ "activation rate", "activation numbers", "activation data", "activation stats", "activation metrics"
    ✓ "D7 retention", "churn rate", "DAU/MAU ratio", "first transaction rate"
    ✓ "show me DAU", "what is WAU", "daily active users" (named engagement metrics)
  IMPORTANT: "activation [any informal]" ALWAYS maps to the pre-built activation metric (metric_id), NOT to
    an event count. "activation numbers" ≠ "count of an event called activation" — there is no such event;
    "activation" is a business concept measured by the PRE-BUILT METRICS entry for activation.
- NEVER set metric_id for plain count / how-many questions on primitive events without a pre-built metric:
    ✗ "how many users completed a purchase" → event="<purchase event>", metric_id=null
    ✗ "count of signups" → event="<signup event>", metric_id=null
    NOTE: "how many activations" is NOT in this list — activation has a pre-built metric; always use metric_id.
- If in doubt, leave metric_id null and fill event instead.
- QUALIFIER DECOMPOSITION (mandatory before clarify/out_of_scope):
  Step A: If a word in the question exactly matches a value under DIMENSION VALUE HINTS, set filters[col]=that value.
  Step B: Match a PRE-BUILT METRICS id/name token (activation, retention, churn, DAU, …).
  Step C: Never treat "[qualifier] [metric]" as one unknown phrase when A and B succeed.
  Informal words ("numbers", "stats", "data", "figures", "share") still mean "show that metric".
  Patterns (any industry):
    "[qualifier] activation [informal]" → analysis_type=metric, metric_id=<activation metric>, filters=<qualifier>.
    "activation numbers/count/data/stats/metrics" → analysis_type=metric, metric_id=<activation metric> (no event needed).
    "[qualifier] retention [informal]" → analysis_type=retention, metric_id=<retention metric with route_as retention>, filters=<qualifier>, retention_window_days = first N days (D7→7, "for 14 days"/"first 14 days"→14).
  Use DIMENSION VALUE HINTS for qualifiers — includes catalog meanings, not only DB samples.
- ACTIVATION WINDOW — for "% of users" ratio metrics (e.g. activation rate, conversion rate):
  activation_window_days is ALWAYS in whole days. Convert before setting:
    "24hr" / "24 hours" / "1 day"  → 1
    "48hr" / "48 hours" / "2 days" → 2
    "12hr" / "6hr" / sub-day       → round up to 1 (smallest representable unit is 1 day)
  When the user names an explicit day or hour window ("7-day", "D7", "D30", "24hr", "within N days"):
    set metric_id=<that metric> AND activation_window_days=<N in whole days after conversion>.
  Without an explicit window: set activation_window_days=null (compiler uses its default window).

AVAILABLE EVENTS — use ONLY the exact strings shown for "event", "event_b", and "funnel_steps":
{event_names}

FILTERABLE COLUMNS — use for "filters". When the user says a word that semantically maps to a value, use that value:
{filter_cols}

BREAKDOWN DIMENSIONS (use ONLY these for "breakdown"):
{dimension_cols}

DIMENSION VALUE HINTS (for disambiguation):
{dim_value_hints}

CUSTOM EVENT COLUMN CONDITIONS — some user phrases map to IS NOT NULL / IS NULL conditions via catalog custom events. These are NOT literal sampled values. Use the sentinel value shown to represent the condition:
{ce_column_conditions_block}
SENTINEL USAGE: when a qualifier phrase matches a concept above, set filters[col] = the sentinel value shown ("__IS_NOT_NULL__" or "__IS_NULL__"). The compiler renders these as `col IS NOT NULL` or `col IS NULL` in SQL. Example: "in app activation rate" → filters: {{"transaction_channel": "__IS_NOT_NULL__"}} (do NOT use "IMPS", "in_app", or any sampled value).

TIME GRANULARITY — how to bucket the time axis:
- "day"   → default, daily trend
- "week"  → "WOW", "weekly", "by week", "week over week"
- "month" → "MOM", "monthly", "by month", "month over month"

{corrections_block}{history_block}Reply with ONLY a JSON object, no markdown, no explanation.
Think step by step in the "_reasoning" field before filling any other slot:
  1. What is the user really asking for?
  2. DECOMPOSE the phrase: strip qualifiers (channel, geo, tier, segment words, informal words like
     "numbers"/"stats"/"data") and check if the remaining core concept matches a metric or event.
     If it does, the qualifiers become filters — do NOT treat the compound as an unknown entity.
  3. Which analysis_type fits?
  4. Which event/metric_id? (After decomposition in step 2.)
  5. Any status/rate distinction needed?
  6. Which filters and dimensions? (Include qualifiers extracted in step 2.)

{{
  "_reasoning":            "<step-by-step reasoning — filled first, discarded after>",
  "analysis_type":         "metric|segment|funnel|funnel_compare|funnel_property_drilldown|journey|retention|behavioral_cohort|time_between|user_lifecycle|stickiness|xyz_matrix|same_month_anchor|diagnose|forecast|clarify|out_of_scope|multi_intent",
  "metric_id":             "<pre-built metric id or null>",
  "event":                 "<primary event name from list or null>",
  "metric_variant":        "null|event_count|status_rate|per_user_count|per_user_value|per_user_dual",
  "metric_value_col":      "<numeric column from catalog for per_user_value, else null>",
  "metric_status_col":     "<outcome/status column for status_rate, else null>",
  "metric_status_target":  "<value from FILTERABLE COLUMNS for that column, else null>",
  "filters":               {{"col": "value"}},
  "filter_excludes":       {{"col": "value_or_list"}},
  "time_range_days":       30,
  "date_from":             "<YYYY-MM-DD inclusive start, or null>",
  "date_to":               "<YYYY-MM-DD exclusive end (first day AFTER the period), or null>",
  "secondary_date_from":   "<for behavioral_cohort cross-period SAME event: second window inclusive start, else null>",
  "secondary_date_to":     "<for behavioral_cohort cross-period SAME event: second window exclusive end, else null>",
  "breakdown":             "<dimension column or null — also used as the property for funnel_property_drilldown>",
  "breakdown_source":      "explicit|default",
  "funnel_steps":          [],
  "event_b":               "<for behavioral_cohort: event they did NOT do; retention: return event; time_between: end event; same_month_anchor: anchor/lifecycle event (e.g. signup_completed); else optional>",
  "retention_window_days":        7,
  "retention_window_days_source": "explicit|default",
  "activation_window_days": "<whole days after onboarding for '% of users' metrics — always convert hours to days (24hr→1, 48hr→2, sub-day→1); 0 for D0/multi-window; null for compiler default>",
  "activation_window_days_source": "explicit|default",
  "time_granularity":       "day",
  "time_granularity_source": "explicit|default",
  "time_source":           "explicit|inherited|default",
  "diagnose_period_end":   "<YYYY-MM-DD last day of the month being diagnosed, or null>",
  "xyz_axis1":             "<for xyz_matrix: second groupby dimension beyond cohort_month (e.g. 'platform', 'acquisition_cohort', 'city'); null for other types>",
  "threshold":             "<integer N for threshold_user_count variant (e.g. 5 for 'more than 5 times'); null otherwise>",
  "clarify_message":       "<question to ask user or null>",
  "depth":                 "quick|deep"
}}

Rules:
- FILTER SEMANTICS: Map the user's words to **columns and values that appear above** in FILTERABLE COLUMNS / DIMENSION VALUE HINTS. Use **exact** strings from the samples (case/spelling as listed). If the user names a category (region, channel, tier, product, outcome word, etc.), find the catalog column whose sampled values include that meaning and set filters[col]=value. Never invent columns or values not supported by the vocabulary.
- EXCLUSION FILTERS: When the user says "excluding X", "except X", "not X", "without X", "non-X users" — use **filter_excludes[col]=value** instead of filters. The compiler renders this as `AND col != 'value'` (or `NOT IN` for lists). Do not guess the positive inverse (e.g. do not set filters[platform]=ios to mean "excluding android" — use filter_excludes[platform]=android). Use exact catalog vocabulary same as filters. Example: "show DAU excluding android users for Feb" → {{"metric_id": "dau", "filter_excludes": {{"platform": "android"}}, "date_from": "2026-02-01", "date_to": "2026-03-01"}}
- QUALIFIERS vs RATE SLOTS: Any **categorical slice** of the population (geography, channel, plan, department, etc.) belongs in **filters** on the appropriate catalog column. Reserve **metric_status_col** / **metric_status_target** only for **status_rate** (ratio of one outcome vs all outcomes on a single outcome column). Do not stuff arbitrary dimensions into metric_status_*.
- Named **channel or instrument slices** in the user's question (a specific payment method, acquisition channel, platform tier, product type, etc.) belong in **filters** on the appropriate catalog column, **metric_variant=null**, and clear **metric_status_*** unless the user truly asked for a **rate** on that column.
- **Same metric** / **same as before** follow-ups (e.g. new breakdown dimension, new time window, "avg X per user" after a filtered query): copy **all** prior context from the last turn — including **filters**, **metric_status_col**, **metric_status_target**, and **event** — unless the user's new question explicitly overrides them. A follow-up question that adds a new angle (breakdown, granularity, variant) must carry forward ALL equality constraints from the prior query. If the previous turn had `metric_status_col=order_status`, `metric_status_target=completed`, and `filters=channel:organic`, a follow-up "avg orders per user" must inherit all three.
- METRIC VARIANT:
  - If the user asks for total/count/number of **events** (raw volume, not distinct users), set metric_variant="event_count".
  - If user asks for a **rate** or **ratio** of outcomes on a status/outcome field (success vs failure, pass vs fail, etc.), set metric_variant="status_rate".
    - Set metric_status_col to the catalog column that holds those outcomes; metric_status_target to the numerator outcome **as listed** in FILTERABLE COLUMNS for that column.
    - For status_rate, do not duplicate that same outcome in filters (it breaks numerator/denominator).
  - If user asks "<measure> per user" / "per-user" / "average per user", set metric_variant for analysis_type metric/segment:
    - per_user_dual when both a numeric "amount-like" and a count-like per-user measure are explicitly requested together
    - per_user_count when the numerator is **event/action count-like** — i.e. user names an event or action ("avg transactions per user", "events per user", "orders per user", "logins per user"). Even if that event has a numeric amount column, if the user said the EVENT NAME (not the column name), they mean COUNT of events per user.
    - per_user_value when user **explicitly names a numeric column** (not an event name): "avg **amount** per user", "avg **spend** per user", "avg **revenue** per user", "avg **value** per user". Only set this when the word is a column/monetary concept, never when it is an event name.
  - If user asks "how many users did X **more than N times**" / "**at least N times**" / "**N+ transactions**" / "users who did X **over N times**" / "**heavy users** (>N)" → set metric_variant="threshold_user_count" and threshold=N (the integer cutoff). The compiler will generate a 2-row power-user vs regular split with HAVING logic. Do NOT use per_user_count for this.
  - Otherwise set metric_variant=null.

  STATUS FILTER vs RATE QUERIES — critical distinction:
  - ADJECTIVE pattern: status word modifies an event as a filter ("successful orders", "failed logins", "completed checkouts") → metric_variant=null, put the status value in filters[status_col]. Example output: {{"metric_variant": null, "filters": {{"<status_col>": "<value>", "<other_dim>": "<val>"}}, "metric_status_col": null}}
  - RATE pattern: the user explicitly asks for a ratio/rate on an outcome column ("what is the X rate", "X success/failure rate", "pass rate", "error rate", "how often does X succeed") → metric_variant MUST be "status_rate". Set metric_status_col=<catalog outcome column>, metric_status_target=<numerator value>. Do NOT put the status value in filters — the rate denominator must be all outcomes; filtering it makes every rate 100%. Other dimension filters (channel, geography, tier, etc.) stay in filters as normal. Example output: {{"metric_variant": "status_rate", "metric_status_col": "<outcome_col>", "metric_status_target": "<value>", "filters": {{"<other_dim>": "<val>"}}}}
  KEY SIGNAL: any question containing the word "rate" referring to an outcome (success rate, failure rate, completion rate, error rate) → metric_variant="status_rate". Never output metric_variant=null when the user asked for a rate.
  EXCEPTION — pre-built metric names already contain "rate"/"percentage"/"percent"/"pct" (activation rate, activation percentage, retention rate, churn rate): when the user qualifies a named pre-built metric with a dimension filter ("UPI activation rate", "UPI activation percentage", "iOS churn rate", "mobile activation pct"), set metric_id to that metric, put the dimension in filters (e.g. filters={{"transaction_channel":"UPI"}}), and set metric_variant=null. Do NOT set metric_variant="status_rate" just because "rate", "percentage", "percent", or "pct" appears in or near the pre-built metric name.
  QUALIFIER VALIDATION (critical — prevents wrong filter mappings): before placing a qualifier in filters, verify it **exactly matches** (case-insensitive) a value listed in DIMENSION VALUE HINTS for the relevant column. Do NOT approximate or infer — "IN APP", "in-app", "app-based", "app", or any phrase not literally present in the sampled values MUST NOT be mapped to an invented filter value (e.g. do NOT map "in app" → "IMPS" just because IMPS is a sampled channel). Instead check CUSTOM EVENT COLUMN CONDITIONS: if the qualifier matches a CE concept, set filters[col] = the sentinel value (__IS_NOT_NULL__ or __IS_NULL__) shown there. Example: "in app activation rate" → filters: {{"transaction_channel": "__IS_NOT_NULL__"}}. If the qualifier matches neither sampled values nor a CE concept, set analysis_type="clarify".
- TIME WINDOW RULES — the orchestrator is the single source of truth for the time window.
  The SQL compiler uses exactly what you output here — no further interpretation.

  PRIORITY ORDER (highest to lowest):
    1. NAMED MONTHS / NAMED CALENDAR PERIOD explicitly stated in the question → set date_from + date_to
    2. TREND KEYWORD only (MOM/WOW with no named period) → set time_granularity + time_range_days
    3. RELATIVE ROLLING WINDOW ("last 7 days", "past month") → set time_range_days only
    4. No qualifier → time_range_days=30 (default)

  Rule: when date_from and date_to are set, the SQL will use them directly.
  MOM/WOW keywords only set time_granularity — they never override explicit dates.

  ── 1. NAMED CALENDAR PERIODS — always set date_from + date_to + time_granularity ──
    "in January" / "Jan 2026"    → date_from="2026-01-01", date_to="2026-02-01", granularity="month"
    "last month" (today={today}) → date_from=1st of prior month, date_to=1st of current month
    "Jan to March" / "Q1 2026"  → date_from="2026-01-01", date_to="2026-04-01", granularity="month"
    "Q4 2025"                   → date_from="2025-10-01", date_to="2026-01-01", granularity="month"
    "last week"                 → date_from=Monday of last week, date_to=this Monday, granularity="week"
    "in 2025" / "YTD"           → date_from=Jan 1 of that/current year, date_to=null, granularity="month"
    "last quarter"              → date_from=1st day of prior quarter, date_to=1st of current quarter
  Named periods are NOT rolling windows — leave time_range_days=30 (unused fallback).

  ── 2. TREND KEYWORDS (no named period) ───────────────────────────────────────
    "MOM" / "monthly trend" → time_granularity="month", time_range_days=180, date_from=null, date_to=null
    "WOW" / "weekly trend"  → time_granularity="week",  time_range_days=90,  date_from=null, date_to=null
  Named period beats trend keyword: "MOM Jan–Mar" → use Rule 1.

  ── 3. RELATIVE ROLLING WINDOWS ──────────────────────────────────────────────
    "recently"/"past few days" → 14 | "past week"/"last 7d" → 7 | "past month" → 30
    "past 3 months" → 90 | "past year"/"last 12 months" → 365 | no qualifier → 30
  COMPOUND: when a rolling window in months (e.g. "last 6 months", "past 3 months") is combined
    with a trend intent ("trend", "share trend", "show trend", "over time"), also set
    time_granularity="month". Similarly "last N weeks" + trend → time_granularity="week".
    Example: "last 6 months onboarding trend" → time_range_days=180, time_granularity="month"
    Example: "share last 3 months trend" → time_range_days=90, time_granularity="month"
{type_specific_rules}
- TIME WINDOW INHERITANCE: if the current question does NOT mention a new time period, copy the exact date_from, date_to, time_range_days, and time_granularity from the most recent turn in PREVIOUS QUERIES.
  EXCEPTIONS (treat as a new time instruction, do NOT inherit fixed dates):
  - If question includes trend keywords (MOM/WOW/month-over-month/week-over-week/monthly trend/weekly trend), set date_from=null and date_to=null, and apply Rule 2 trend settings.
  - If question asks to "share trend", "show trend", or "compare over time", treat as trend intent and use time_granularity + time_range_days (no fixed date inheritance) unless an explicit named period is present. Apply the COMPOUND rule: if the rolling window is in months, set time_granularity="month"; if in weeks, set time_granularity="week".
  Follow-up filters/breakdowns still inherit time when no trend/new-period instruction is present.
  CRITICAL: time exceptions affect ONLY the four time fields (date_from, date_to, time_range_days,
  time_granularity). They NEVER reset event, metric_id, filters, breakdown, or any other slot.
  A follow-up like "show the same MOM" or "city breakdown MOM" must inherit event and filters
  unchanged from the prior turn and only update the time granularity.
- FOLLOW-UP RESOLUTION: if the current question does NOT name a new event or metric — copy the previous query's event and metric_id, then apply only the new constraint (new analysis_type, new filter, new breakdown). Do NOT return clarify for follow-up modifiers like "now filter to X", "break it down by Y", "split by demographics", "show me the same for Z platform".
- Set time_source:
  - "explicit" when question contains a new time instruction (named month/range, MOM/WOW, last/past N unit).
  - "inherited" when time is copied from previous turn.
  - "default" when neither explicit nor inherited is used.
- Set time_granularity_source:
  - "explicit" when user said "by month", "weekly", "MOM", "WOW", "daily trend", "monthly", "week over week", etc.
  - "default" when granularity was not mentioned — leave time_granularity="day" but mark source="default".
  Compilers use this to distinguish "user wants daily buckets" from "system defaulted to day".
- Set retention_window_days_source:
  - "explicit" when user named a specific window ("D7", "14-day retention", "for 14 days", "24hr retention").
  - "default" when no window was stated (compiler uses catalog default or scale rule).
- Set activation_window_days_source:
  - "explicit" when user named a specific window ("D7", "24hr", "7-day", "within 30 days").
  - "default" when no window was stated (activation_window_days=null).
- Set breakdown_source:
  - "explicit" when user said "by <dimension>", "split by", "break down by", "per <dimension>".
  - "default" when breakdown was not mentioned (breakdown=null).
- Never output column or event names that are not in the lists above.

DEPTH — set "deep" when the question genuinely needs multiple analytical angles
to give a complete answer. Set "quick" for everything else.

  Use "deep" for:
  • Root-cause / investigative questions: "why did X drop/spike/change", "what caused",
    "what's driving", "explain the decline/growth", "root cause"
  • Anomaly investigations: metric changed and user wants to understand all dimensions
  • Explicit multi-angle requests: "give me a full analysis of X", "deep dive",
    "investigate", "what's happening with X"
  • Comparison questions spanning several dimensions: "compare X vs Y across segments"

  Keep "quick" for:
  • Single-metric lookups: "show me DAU", "how many users", "trend of X"
  • Follow-up refinements: "filter to <value>", "break down by <dimension>", "same but weekly"
  • Questions already answered by a rich type: funnel, retention, journey, behavioral_cohort
  • Forecast requests
  • Any question where one focused SQL query gives the complete answer

WORKED EXAMPLES — study slot choices here before filling your own:

Q: "D0 activation"
→ analysis_type=metric, metric_id=<activation metric>, activation_window_days=0,
   time_granularity=day, time_source=default
   [D0 → win=0 signals multi-window; compiler shows all catalog D-windows as cohort trend]

Q: "24hr conversion rate"
→ analysis_type=metric, metric_id=<activation/conversion metric>, activation_window_days=1,
   time_source=default
   [ALWAYS convert hours to whole days: 24hr=1, 48hr=2, sub-day=1]

Q: "show me activation rate by month over last 6 months"
→ analysis_type=metric, metric_id=<activation metric>, activation_window_days=null,
   time_granularity=month, time_range_days=180, time_source=explicit
   [user said "by month" and "6 months" → time_granularity=month is explicit, not default]

Q: "D7 retention"
→ analysis_type=retention, metric_id=<retention metric>, retention_window_days=7,
   time_source=default
   [D7 → retention_window_days=7; never use activation_window_days for retention]

Q: "show me D7 and D30 activation"
→ analysis_type=metric, metric_id=<activation metric>, activation_window_days=null,
   time_source=default
   [multiple windows requested → null lets the compiler emit all catalog default_windows]

Q: "why did activation drop last week"
→ analysis_type=diagnose, metric_id=<activation metric>,
   time_source=explicit, depth=deep
   [investigative root-cause question → diagnose + deep, not segment or metric]

Q: "funnel from signup to first purchase"
→ analysis_type=funnel, funnel_steps=[<signup event>, <purchase event>],
   time_source=default
   [step order matters: earlier event listed first]

Q: "MOM activation by cohort"
→ analysis_type=metric, metric_id=<activation metric>, activation_window_days=null,
   time_granularity=month, time_range_days=180, time_source=explicit
   [MOM = time_granularity=month; time_range_days≥90 to get multiple cohort months]
"""


def _format_event_section(
    all_event_names: list[str],
    top_events: list[dict],
) -> str:
    """
    Build the event listing block for the orchestrator prompt.

    Top-matched events (from BM25 retrieval) get semantic descriptions so the
    LLM can resolve synonyms (e.g. "engagement" → session_start). All event
    names are listed as a hard constraint so the LLM never invents names.
    """
    lines: list[str] = []

    if top_events:
        lines.append("Top-matched events for this question (with context):")
        for ev in top_events:
            name    = ev["id"]
            display = ev.get("display_name", "")
            desc    = ev.get("description", "")
            journey = ev.get("journey", "")
            stage   = ev.get("stage", "")
            tags    = ev.get("tags", [])

            # Display name only if meaningfully different from raw_name
            display_str = (
                f" ({display})"
                if display and display.lower() != name.replace("_", " ").lower()
                else ""
            )
            desc_str = f": {desc}" if desc else ""
            meta_parts = list(filter(None, [journey, stage] + tags[:2]))
            meta_str = f" [{', '.join(meta_parts)}]" if meta_parts else ""

            lines.append(f"  - {name}{display_str}{desc_str}{meta_str}")
        lines.append("")

    lines.append("All valid event name strings (hard constraint — use ONLY these exact values):")
    lines.append("  " + ", ".join(all_event_names))

    return "\n".join(lines)


def _format_filter_cols(filter_cols: dict[str, list]) -> str:
    lines = []
    for col, vals in sorted(filter_cols.items()):
        vals_str = ", ".join(repr(v) for v in sorted(str(v) for v in vals))
        lines.append(f"  {col}: [{vals_str}]")
    return "\n".join(lines)


def _format_dim_value_hints(dim_value_hints: dict[str, list]) -> str:
    lines = []
    for col, vals in sorted(dim_value_hints.items()):
        vals_str = ", ".join(repr(v) for v in sorted(str(v) for v in vals))
        lines.append(f"  {col}: [{vals_str}]")
    return "\n".join(lines)


def _format_ce_column_conditions(ce_column_conditions: dict[str, list]) -> str:
    """
    Format custom-event column conditions (IS NOT NULL / IS NULL) for the prompt.
    Shows the LLM the sentinel value to use when a qualifier matches a CE concept.
    """
    if not ce_column_conditions:
        return "(none)"
    lines = []
    for col in sorted(ce_column_conditions):
        seen: set[str] = set()
        for op, ce_label, ce_desc in ce_column_conditions[col]:
            key = (col, op)
            if key in seen:
                continue
            seen.add(key)
            sentinel = "__IS_NOT_NULL__" if op == "IS NOT NULL" else "__IS_NULL__"
            desc_part = f" ({ce_desc})" if ce_desc else ""
            lines.append(
                f"  User says \"{ce_label}\"-like phrase → "
                f"filters[{col}] = \"{sentinel}\"  [{op} in SQL]{desc_part}"
            )
    return "\n".join(lines)


def _format_corrections_block(corrections: list[dict]) -> str:
    """
    Format past negative feedback entries for injection into the system prompt.
    Each entry was marked wrong by a user and (optionally) includes a note.
    Only entries with a non-empty note are injected — they carry actionable signal.
    """
    entries = [c for c in (corrections or []) if (c.get("note") or "").strip()]
    if not entries:
        return ""
    lines = [
        "CORRECTION MEMORY — Users have flagged these past responses as incorrect.",
        "Apply these learnings when answering the current question:",
    ]
    for c in entries:
        parts = []
        if c.get("event"):
            parts.append(f"event={c['event']}")
        if c.get("metric_id"):
            parts.append(f"metric={c['metric_id']}")
        if c.get("analysis_type"):
            parts.append(f"type={c['analysis_type']}")
        tag = f"[{', '.join(parts)}] " if parts else ""
        date_str = (c.get("created_at") or "")[:10]
        lines.append(f"  • {tag}\"{c['note'].strip()}\"" + (f"  [{date_str}]" if date_str else ""))
    lines.append("")
    return "\n".join(lines) + "\n"


def _format_history(history: list[dict]) -> str:
    """
    Format the last N conversation turns for injection into the prompt.
    Each entry: {"question": str, "qo": dict}
    """
    if not history:
        return ""
    lines = ["PREVIOUS QUERIES IN THIS SESSION (resolve references like 'same metric', 'add that filter', 'it'):"]
    for i, turn in enumerate(history, 1):
        q   = turn.get("question", "")
        qo  = turn.get("qo", {})
        mem = turn.get("memory", {}) or {}
        # Build summary from canonical to_dict() when available, else fall back
        # to raw dict so all QueryObject fields (including threshold, event_b,
        # metric_value_col, etc.) are always visible to the orchestrator on follow-ups.
        _qo_obj = turn.get("qo_obj")
        if _qo_obj is not None and hasattr(_qo_obj, "to_dict"):
            _d = _qo_obj.to_dict()
        else:
            _d = qo  # legacy: raw dict from older history entries
        _ce = turn.get("custom_event_name") or ""
        _mn = turn.get("metric_name") or ""
        summary = (
            f"type={_d.get('analysis_type')} | event={_d.get('event')} | "
            f"metric_id={_d.get('metric_id')} | custom_event={_ce} | metric_name={_mn} | "
            f"event_b={_d.get('event_b')} | "
            f"metric_variant={_d.get('metric_variant')} | "
            f"metric_value_col={_d.get('metric_value_col')} | "
            f"metric_status_col={_d.get('metric_status_col')} | "
            f"metric_status_target={_d.get('metric_status_target')} | "
            f"threshold={_d.get('threshold')} | "
            f"breakdown={_d.get('breakdown')} | filters={_d.get('filters')} | "
            f"date_from={_d.get('date_from')} | date_to={_d.get('date_to')} | "
            f"time_range_days={_d.get('time_range_days')} | granularity={_d.get('time_granularity')} | "
            f"time_source={_d.get('time_source')}"
        )
        mem_summary = (mem.get("summary") or "").strip()
        verdict = (mem.get("hypothesis_verdict") or "").strip()
        mem_suffix = ""
        if mem_summary:
            mem_suffix += f" | memory={mem_summary[:180]}"
        if verdict:
            mem_suffix += f" | hypothesis_verdict={verdict}"
        lines.append(f"  {i}. Q: \"{q}\" → {summary}{mem_suffix}")
    lines.append("")  # blank line before the reply instruction
    return "\n".join(lines) + "\n"


# ── Main orchestrator call ────────────────────────────────────────────────────

def _format_hypothesis_context(hypothesis_doc) -> str:
    """
    Inject hypothesis context into the orchestrator so the QueryObject
    is directed at testing the most likely hypothesis, not just answering
    the question literally.
    """
    if not hypothesis_doc or not hypothesis_doc.hypotheses:
        return ""
    lines = [
        "ANALYST CONTEXT (from hypothesis agent — use to inform analysis_type and event choice):",
        f"  Investigation focus: {hypothesis_doc.investigation_context}",
    ]
    for h in hypothesis_doc.hypotheses[:2]:  # top 2 hypotheses only
        lines.append(f"  H{h.priority} [{h.category}]: check {h.expected_signal}")
    if hypothesis_doc.analysis_type_hint:
        lines.append(f"  SUGGESTED analysis_type: {hypothesis_doc.analysis_type_hint} — prefer this unless your step-by-step reasoning clearly supports a different type")
    lines.append("")
    return "\n".join(lines)


# ── Qualified pre-built metric rescue (catalog + sampled values only) ─────────

_ORCH_METRIC_GENERIC_TOKENS = frozenset({
    "rate", "ratio", "percent", "percentage", "pct", "users",
    "count", "daily", "weekly", "monthly", "annual", "average", "avg",
    "total", "metric", "metrics", "index", "score",
})

_ORCH_INFORMAL_METRIC_WORDS = frozenset({
    "numbers", "number", "stats", "statistics", "data", "figures", "values",
    "value", "results", "result", "report", "reporting", "share", "please",
    "show", "what", "give", "tell", "how", "many", "much", "get", "need",
    "want", "see", "view", "latest", "current",
})


def _catalog_suggested_metrics(catalog: dict) -> list[dict]:
    out: list[dict] = []
    for tname, tdata in catalog.items():
        if tname.startswith("__") or not isinstance(tdata, dict):
            continue
        for m in tdata.get("suggested_metrics") or []:
            if isinstance(m, dict) and (m.get("id") or "").strip():
                out.append(m)
    return out


def _metric_id_tokens(metric_id: str) -> list[str]:
    parts = re.split(r"[_\s\-]+", str(metric_id or "").lower())
    return [p for p in parts if p and p not in _ORCH_METRIC_GENERIC_TOKENS and len(p) >= 2]


def _score_metric_question_fit(question_tokens: set[str], metric: dict) -> int:
    """Prefer metrics whose id tokens all appear in the question."""
    id_parts = _metric_id_tokens(str(metric.get("id") or "").strip())
    if not id_parts:
        return 0
    matched = [p for p in id_parts if p in question_tokens]
    if not matched:
        return 0
    return len(matched) * 10 - (len(id_parts) - len(matched)) * 5


def _is_retention_metric(metric: dict) -> bool:
    mid = str(metric.get("id") or "").lower()
    mtype = str(metric.get("type") or "").lower()
    bt = str((metric.get("builder_definition") or {}).get("builder_type") or "").lower()
    return "retention" in mid or mtype == "retention" or "retention" in bt


_RETENTION_METRIC_PRIORITY = ("d7_retention", "d30_retention", "d1_retention")


def _pick_retention_metric(catalog: dict, question: str) -> Optional[dict]:
    """Best catalog retention metric for the user's question (default D7)."""
    from core.pipeline.activation_window import parse_retention_window_days_from_prompt

    candidates = [m for m in _catalog_suggested_metrics(catalog) if _is_retention_metric(m)]
    if not candidates:
        return None
    by_id = {m.get("id"): m for m in candidates}

    win = parse_retention_window_days_from_prompt(question)
    if win is not None:
        for mid, m in by_id.items():
            if mid and (mid.startswith(f"d{win}_") or f"d{win}" in mid):
                return m

    for pref in _RETENTION_METRIC_PRIORITY:
        if pref in by_id:
            return by_id[pref]
    return candidates[0]


def _hydrate_retention_from_metric(qo: QueryObject, metric: dict) -> None:
    """Populate event / event_b / retention_window_days / status filters from catalog metric."""
    bd = metric.get("builder_definition") or {}
    primary = str(bd.get("primary_event") or "").strip()
    if not primary:
        sql = metric.get("sql_hint") or metric.get("sql") or ""
        found = re.findall(r"event_name[`\"']?\s*=\s*'([^']+)'", sql, re.IGNORECASE)
        primary = found[0] if found else ""
    if primary:
        qo.event = primary
        qo.event_b = primary
    win = bd.get("retention_days") or bd.get("retention_window_days")
    if win:
        qo.retention_window_days = int(win)
    cur = dict(qo.filters or {})
    for f in bd.get("filters_structured") or []:
        if not isinstance(f, dict):
            continue
        col = str(f.get("field") or "").strip()
        val = str(f.get("value") or "").strip()
        if col and val and str(f.get("op") or "=").strip() == "=":
            cur.setdefault(col, val)
    qo.filters = cur or None


def _resolve_qualified_prebuilt_metric(
    qo: QueryObject,
    question: str,
    catalog: dict,
    sampled_values: dict[str, dict[str, list]],
) -> None:
    """
    When the LLM returns clarify/out_of_scope for a decomposed phrase like
    ``[qualifier] activation numbers`` or ``[qualifier] retention numbers``,
    recover using catalog metrics + dimension hints only (no hardcoded domain).
    """
    if qo.analysis_type not in ("clarify", "out_of_scope") or qo.metric_id:
        return

    qual_filters = extract_dimension_filters_from_question(
        question, sampled_values, catalog,
    )
    q_lower = question.lower()

    if re.search(r"\bretention\b", q_lower):
        retention_metric = _pick_retention_metric(catalog, question)
        if retention_metric:
            qo.analysis_type = "retention"
            qo.metric_id = str(retention_metric.get("id") or "").strip() or None
            _hydrate_retention_from_metric(qo, retention_metric)
            merged = dict(qo.filters or {})
            merged.update(qual_filters)
            qo.filters = merged or None
            from core.pipeline.activation_window import parse_retention_window_days_from_prompt
            win = parse_retention_window_days_from_prompt(question)
            if win is not None:
                qo.retention_window_days = win
            qo.clarify_message = None
            qo.metric_variant = None
            setattr(qo, "_qualified_metric_rescued", True)
            return

    q_tokens = set(re.findall(r"\b[a-z][a-z0-9]*\b", q_lower))
    q_tokens -= _ORCH_INFORMAL_METRIC_WORDS

    scored: list[tuple[int, dict]] = []
    for m in _catalog_suggested_metrics(catalog):
        if _is_retention_metric(m):
            continue
        s = _score_metric_question_fit(q_tokens, m)
        if s > 0:
            scored.append((s, m))
    if not scored:
        return

    scored.sort(key=lambda x: (-x[0], len(x[1].get("id", "")), x[1].get("id", "")))
    if len(scored) > 1 and scored[1][0] >= scored[0][0]:
        return

    best = scored[0][1]
    qo.analysis_type = "metric"
    qo.metric_id = str(best.get("id") or "").strip() or None
    merged = dict(qo.filters or {})
    merged.update(qual_filters)
    qo.filters = merged or None
    qo.clarify_message = None
    qo.metric_variant = None
    if qo.metric_id:
        apply_activation_window_from_prompt(qo, question, catalog=catalog)
    setattr(qo, "_qualified_metric_rescued", True)


def _rescue_metric_id_for_named_concepts(
    qo: QueryObject,
    question: str,
    catalog: dict,
) -> None:
    """
    Deterministic post-LLM rescue: when metric_id=null but the prompt contains a
    concept keyword (aarrr/category/domain_category from catalog), set metric_id.

    Catalog-driven — no hardcoded domain knowledge, works for any industry.
    Handles any phrasing: "14 day activation number", "share activation stats", etc.
    """
    if qo.metric_id:
        return
    if getattr(qo, "analysis_type", "") in ("retention", "clarify", "out_of_scope"):
        return  # other rescues handle these

    q_lower = question.lower()

    # Build keyword → [metric] index from catalog concept fields
    kw_to_metrics: dict[str, list[dict]] = {}
    for m in _catalog_suggested_metrics(catalog):
        if _is_retention_metric(m):
            continue
        keywords: set[str] = set()
        for field in ("aarrr", "category"):
            v = str(m.get(field) or "").lower().strip()
            if len(v) >= 4:
                keywords.add(v)
        bd = m.get("builder_definition") or {}
        v = str(bd.get("domain_category") or "").lower().strip()
        if len(v) >= 4:
            keywords.add(v)
        for kw in keywords:
            kw_to_metrics.setdefault(kw, []).append(m)

    q_tokens = set(re.findall(r"\b[a-z][a-z0-9]*\b", q_lower)) - _ORCH_INFORMAL_METRIC_WORDS
    for kw, candidates in kw_to_metrics.items():
        if not re.search(rf"\b{re.escape(kw)}\b", q_lower):
            continue
        scored = sorted(
            ((max(_score_metric_question_fit(q_tokens, m), 1), m) for m in candidates),
            key=lambda x: (-x[0], len(x[1].get("id", ""))),
        )
        best = scored[0][1]
        qo.metric_id = str(best.get("id") or "").strip() or None
        if qo.metric_id:
            qo.analysis_type = "metric"
            apply_activation_window_from_prompt(qo, question, catalog=catalog)
            setattr(qo, "_concept_keyword_rescued", True)
        return


@track(name="orchestrator", tags=["pipeline"], capture_input=False, capture_output=False)
def orchestrate(
    question: str,
    catalog: dict,
    sampled_values: dict[str, dict[str, list]],
    openai_api_key: Optional[str] = None,
    history: Optional[list[dict]] = None,
    hypothesis_doc=None,
    corrections: Optional[list[dict]] = None,
    eval_provider: Optional[str] = None,
    temperature: float = 0.0,
) -> QueryObject:
    """
    Parse a natural language question into a QueryObject.
    Never writes SQL — only fills typed slots from catalog vocabulary.

    history:        last 3 turns as [{"question": str, "qo": dict}, ...]
                    enables follow-up questions that refine filters or dimensions
    hypothesis_doc: HypothesisDoc from hypothesis_agent.generate_hypotheses
                    steers analysis_type and event toward testing hypotheses
    eval_provider:  Override the LLM provider for this call (used by eval benchmark).
    """
    client = make_llm_client(openai_api_key, provider=eval_provider)

    vocab = build_vocab(catalog, sampled_values)

    # ── Semantic retrieval: rank events/metrics by BM25 relevance to question ─
    # Injects descriptions for top-matched events so the LLM can resolve synonyms
    # (e.g. "engagement" → session_start). Falls back gracefully on empty catalog.
    try:
        sem_index  = get_or_build_index(catalog)
        sem_result = sem_index.retrieve(question)
        event_names_block = _format_event_section(
            all_event_names=sem_result.all_event_names,
            top_events=sem_result.top_events,
        )
        # Use semantically ranked metrics only when the catalog has metric descriptions
        # (avoids replacing a richer metrics list with a truncated ranked one)
        metrics_list = vocab["metrics"]
    except Exception:
        # Semantic index unavailable — fall back to original flat listing
        event_names_block = "\n".join(f"  - {e}" for e in vocab["event_names"])
        metrics_list = vocab["metrics"]

    _types_descriptions, _type_specific_rules = _build_dynamic_types(question)

    # Gate funnels injection — only inject saved funnels when the query is funnel-related
    _FUNNEL_KW = {"funnel", "step", "conversion", "drop-off", "dropoff"}
    _include_funnels = bool(_FUNNEL_KW & set(question.lower().split()))
    _funnels_payload = vocab["funnels"] if _include_funnels else []

    system = _SYSTEM.format(
        hypothesis_block=_format_hypothesis_context(hypothesis_doc),
        today=date.today().isoformat(),
        glossary_json=json.dumps(vocab.get("glossary") or [], indent=2),
        conventions_json=json.dumps(vocab.get("conventions") or [], indent=2),
        types_descriptions=_types_descriptions,
        type_specific_rules=_type_specific_rules,
        metrics_json=json.dumps(metrics_list, indent=2),
        funnels_json=json.dumps(_funnels_payload, indent=2),
        event_names=event_names_block,
        filter_cols=_format_filter_cols(vocab["filter_cols"]),
        dimension_cols=", ".join(vocab["dimension_cols"]),
        dim_value_hints=_format_dim_value_hints(vocab["dim_value_hints"]),
        ce_column_conditions_block=_format_ce_column_conditions(vocab.get("ce_column_conditions") or {}),
        corrections_block=_format_corrections_block(corrections or []),
        history_block=_format_history(history or []),
    )

    _messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": question},
    ]
    # Use provider-specific strong model (eval_provider may differ from LLM_PROVIDER).
    _model = resolve_model("strong", provider=eval_provider)
    # Retry up to 2 times when the provider returns None content (e.g. Gemini thinking mode).
    data: dict = {}
    for _attempt in range(3):
        resp = call_llm(
            client,
            call_site="orchestrator",
            model=_model,
            temperature=temperature,
            messages=_messages,
        )
        raw = resp.choices[0].message.content
        if raw is None:
            continue  # Thinking model returned empty text — retry
        raw = raw.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = json.loads(raw)
            break
        except Exception:
            # Thinking models (Gemini 2.5-flash) sometimes output reasoning as freeform
            # text before/after the JSON block. Use raw_decode to parse the first valid
            # JSON object starting from the first '{'.
            start = raw.find("{")
            if start >= 0:
                try:
                    data, _ = json.JSONDecoder().raw_decode(raw, start)
                    break
                except Exception:
                    pass
            continue
    if not data:
        return QueryObject(analysis_type="clarify",
                           clarify_message="I couldn't parse that question. Could you rephrase it?")

    _reasoning = data.pop("_reasoning", None)  # scratchpad — never leaks into QueryObject slots
    qo = QueryObject.from_dict(data)
    qo._llm_reasoning = _reasoning or ""  # preserved for debug panel only
    qo.time_source = "explicit" if _question_has_time(question) else "default"

    _resolve_qualified_prebuilt_metric(qo, question, catalog, sampled_values)
    _rescue_metric_id_for_named_concepts(qo, question, catalog)

    apply_activation_window_from_prompt(qo, question, catalog=catalog)

    # ── Deterministic status_rate correction ──────────────────────────────────
    # The LLM sometimes gets the rate/filter distinction wrong in two ways:
    #   A) Sets metric_status_col/target correctly but leaves metric_variant=null
    #   B) Puts the status col in filters{} instead of in metric_status_col/target
    # Both are fixed here when "rate", "ratio", "percent", "how often" is in question.
    _RATE_WORDS = {"rate", "ratio", "percent", "percentage", "how often"}
    _STATUS_SUFFIXES = ("_status", "_state", "_result", "_outcome", "_flag")
    _OUTCOME_VALS = frozenset({"success", "failed", "failure", "completed", "complete",
                               "passed", "pass", "error", "approved", "rejected"})
    _q_words = set(question.lower().split())
    if _RATE_WORDS & _q_words and qo.metric_variant != "status_rate" and not qo.metric_id:
        # Case A: metric_status_col already set, just variant is wrong
        if qo.metric_status_col:
            qo.metric_variant = "status_rate"
            status_key = qo.metric_status_col.strip().lower()
            qo.filters = {k: v for k, v in (qo.filters or {}).items()
                          if k.lower() != status_key}
        else:
            # Case B: LLM put the status in filters — detect by key suffix or value
            for fk, fv in list((qo.filters or {}).items()):
                fk_l = fk.strip().lower()
                fv_l = str(fv).strip().lower()
                if fk_l.endswith(_STATUS_SUFFIXES) or fv_l in _OUTCOME_VALS:
                    qo.metric_variant = "status_rate"
                    qo.metric_status_col = fk
                    qo.metric_status_target = fv
                    qo.filters = {k: v for k, v in qo.filters.items() if k != fk}
                    break

    # Case C: status_rate set but col/target missing — infer from catalog sampled values.
    # E.g. "transaction success rate" → event=transaction_reconciled has transaction_status col.
    if qo.metric_variant == "status_rate" and qo.event and not qo.metric_status_col:
        ev_sampled = sampled_values.get("events", {}) if sampled_values else {}
        for col, vals in ev_sampled.items():
            if not col.endswith(_STATUS_SUFFIXES):
                continue
            vals_lower = [str(v).lower() for v in (vals or [])]
            # Find outcome value that best matches question words
            best_target = None
            for v in vals:
                if str(v).lower() in _OUTCOME_VALS & _q_words:
                    best_target = v
                    break
            if best_target is None:
                # Default positive outcome: SUCCESS > success > COMPLETED > APPROVED
                for preferred in ("SUCCESS", "success", "COMPLETED", "completed", "APPROVED"):
                    if preferred.lower() in vals_lower:
                        best_target = preferred if preferred in vals else next(
                            (v for v in vals if str(v).lower() == preferred.lower()), None
                        )
                        break
            if best_target:
                qo.metric_status_col = col
                qo.metric_status_target = best_target
                break

    # ── Programmatic time-window inheritance ──────────────────────────────────
    # When the question has no explicit time signal and the QO has no absolute
    # range yet, copy from history (newest → oldest):
    #   1) first turn with fixed calendar dates, else
    #   2) first turn with a non-default rolling window (week/month bucket,
    #      time_range_days ≠ 30, or time_source explicit/inherited).
    if history and not _question_has_time(question):
        if not qo.date_from and not qo.date_to:
            inherited_fixed = False
            for turn in reversed(history):
                h = turn.get("qo", {})
                if h.get("date_from") or h.get("date_to"):
                    qo.date_from = h.get("date_from")
                    qo.date_to = h.get("date_to")
                    qo.time_range_days = h.get("time_range_days") or qo.time_range_days
                    qo.time_granularity = h.get("time_granularity") or qo.time_granularity
                    qo.time_source = "inherited"
                    inherited_fixed = True
                    break

            if not inherited_fixed:
                for turn in reversed(history):
                    h = turn.get("qo", {})
                    if h.get("date_from") or h.get("date_to"):
                        continue
                    tg = (h.get("time_granularity") or "day").lower()
                    try:
                        trd = int(h.get("time_range_days") or 30)
                    except (TypeError, ValueError):
                        trd = 30
                    tss = (h.get("time_source") or "default").lower()
                    if not (
                        tss in ("explicit", "inherited")
                        or tg in ("week", "month")
                        or trd != 30
                    ):
                        continue
                    qo.time_range_days = trd
                    if tg in ("day", "week", "month"):
                        qo.time_granularity = tg
                    qo.time_source = "inherited"
                    break

    apply_retention_window_from_prompt(qo, question, catalog=catalog)

    return qo
