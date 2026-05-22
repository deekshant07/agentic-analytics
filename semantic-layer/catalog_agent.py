"""
catalog_agent.py — Step 2

For regular tables: 1 LLM call (columns + metrics).
For event tables:   2 LLM calls:
  - Call A: table metadata + columns + suggested metrics
  - Call B: all events with per-event properties

Splitting prevents output token truncation when there are many events
with rich property sets (e.g. 27 events × 90 sparse columns).
"""
from __future__ import annotations

import json
import re
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Make sure the semantic-layer directory is on sys.path so metrics_matcher
# can be imported regardless of where the script is launched from.
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from metrics_matcher import diagnose_metrics as _diagnose_metrics

load_dotenv()

# ── LLM Client setup (delegates to core/llm.py for provider resolution) ──────
# Falls back to legacy OpenAI/Gemini direct clients if core llm is unavailable.

def get_llm_client():
    try:
        from core.llm import make_llm_client as _make_core_client, LLM_MEDIUM, _active_provider
        provider_name = os.environ.get("LLM_PROVIDER", "groq")
        print(f"Using {provider_name} / {LLM_MEDIUM} (via core.llm)")
        return _make_core_client(), "core"
    except Exception:
        pass

    # Legacy fallback — only reached if core.llm is unavailable
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            from openai import OpenAI
            print("Using OpenAI (gpt-4o-mini) [legacy fallback]")
            return OpenAI(api_key=openai_key), "openai"
        except ImportError:
            pass

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            print("Using Gemini (gemini-1.5-flash) [legacy fallback]")
            return genai, "gemini"
        except ImportError:
            pass

    raise ValueError(
        "No LLM provider available. Set LLM_PROVIDER + provider API key in .env."
    )


def call_llm(client, provider: str, system_prompt: str, user_prompt: str) -> str:
    if provider == "core":
        from core.llm import call_llm as _core_call_llm, LLM_MEDIUM
        resp = _core_call_llm(
            client,
            call_site="catalog_agent",
            model=LLM_MEDIUM,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content

    if provider == "openai":
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt}
            ]
        )
        return response.choices[0].message.content

    elif provider == "gemini":
        import google.generativeai as genai
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=system_prompt,
            generation_config={"temperature": 0}
        )
        response = model.generate_content(user_prompt)
        return response.text

    raise ValueError(f"Unknown provider: {provider}")


def parse_json(raw_text: str) -> dict:
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


SQL_KEYWORDS = {
    "and", "as", "asc", "between", "by", "case", "count", "current_date",
    "date", "date_trunc", "desc", "distinct", "else", "end", "false", "filter", "from",
    "group", "having", "in", "interval", "is", "join", "left", "like", "limit",
    "max", "min", "not", "null", "nullif", "on", "or", "order", "round",
    "select", "sum", "then", "true", "when", "where",
}

USER_SIGNALS = {"user_id", "userid", "account_id", "customer_id",
                "member_id", "uid", "visitor_id"}
TIME_SIGNALS = {"event_time", "timestamp", "created_at", "occurred_at",
                "event_ts", "ts", "time"}

GLOSSARY_TAXONOMY = {
    "acquisition": {"acquisition", "signup", "new user", "install", "onboarding start"},
    "activation": {"activation", "activated", "onboarding", "kyc", "first transaction"},
    "engagement": {"engagement", "stickiness", "dau", "wau", "mau", "session"},
    "retention": {"retention", "churn", "d1", "d7", "d30", "returning"},
    "revenue": {"revenue", "arpu", "ltv", "gmv", "payment", "transaction value"},
    "risk_compliance": {"risk", "fraud", "compliance", "kyc quality", "chargeback"},
}


def _find_exact_signal_col(columns: list[dict], signals: set[str]) -> str:
    for col in columns:
        if col["name"].lower() in signals:
            return col["name"]
    return ""


def _guess_primary_event_table(raw_schema: dict) -> tuple[str, dict]:
    candidates = [
        (table_name, table_info)
        for table_name, table_info in raw_schema.items()
        if table_info.get("is_event_table") and table_info.get("event_name_col")
    ]
    if not candidates:
        return "", {}
    return max(candidates, key=lambda item: item[1].get("row_count", 0))


def _normalise_duckdb_sql(sql: str, time_col: str) -> str:
    sql = re.sub(
        r"DATE_SUB\s*\(\s*CURRENT_DATE\s*,\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
        lambda m: f"CURRENT_DATE - INTERVAL '{m.group(1)} days'",
        sql,
        flags=re.IGNORECASE,
    )
    if time_col:
        sql = re.sub(r"\bdate\b(?!\s*\()", f'DATE({time_col})', sql, flags=re.IGNORECASE)
    return sql


def _extract_identifiers(sql: str) -> set[str]:
    scrubbed = re.sub(r"'[^']*'", " ", sql)
    return {tok.lower() for tok in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", scrubbed)}


def _unknown_identifiers(sql: str, allowed_columns: set[str]) -> set[str]:
    identifiers = _extract_identifiers(sql)
    return {
        tok for tok in identifiers
        if tok not in SQL_KEYWORDS and tok not in allowed_columns
    }


def _extract_event_name_claims(sql: str, event_col: str) -> set[str]:
    claims = set()
    pattern = rf"{re.escape(event_col)}\s*(?:=|IN)\s*(\((?:[^)]*)\)|'[^']*')"
    for match in re.finditer(
        pattern,
        sql,
        flags=re.IGNORECASE,
    ):
        claims |= set(re.findall(r"'([^']+)'", match.group(1)))
    return claims


def _sanitize_custom_event_sql(sql: str, allowed_columns: set[str],
                               valid_event_names: set[str], time_col: str,
                               event_col: str) -> str:
    sql = _normalise_duckdb_sql(sql, time_col)
    claims = _extract_event_name_claims(sql, event_col)
    if claims - valid_event_names:
        return ""
    if _unknown_identifiers(sql, allowed_columns):
        return ""
    return sql


def _sanitize_glossary_sql(sql: str, allowed_columns: set[str],
                           valid_event_names: set[str], time_col: str,
                           event_col: str) -> str:
    sql = _normalise_duckdb_sql(sql, time_col)
    claims = _extract_event_name_claims(sql, event_col)
    if claims - valid_event_names:
        return ""
    if _unknown_identifiers(sql, allowed_columns):
        return ""
    return sql


def _classify_glossary_category(term: str, description: str) -> str:
    text = f"{term} {description}".lower()
    for cat, hints in GLOSSARY_TAXONOMY.items():
        if any(h in text for h in hints):
            return cat
    return "engagement"


def _is_event_like_term(term: str, valid_event_names: set[str]) -> bool:
    t = (term or "").strip().lower()
    if not t:
        return False
    if t in {e.lower() for e in valid_event_names}:
        return True
    # event-like shape: snake_case verbs with explicit action tokens
    if "_" in t and any(k in t for k in ("open", "click", "completed", "failed", "started", "viewed")):
        return True
    return False


SYSTEM_PROMPT = """You are a data catalog agent. Your job is to analyse a
database table and produce human-readable documentation for an analytics agent.

You must infer meaning from the ACTUAL DATA — sample rows and sample values.
Do not rely on assumptions. Look at what is really stored.

Output ONLY valid JSON. No markdown fences. No explanation. No other text."""


# ── Call A: table metadata + columns + metrics ────────────────────────────────

NUMERIC_TYPES = {"INTEGER", "BIGINT", "DOUBLE", "FLOAT", "DECIMAL",
                 "NUMERIC", "REAL", "SMALLINT", "TINYINT", "HUGEINT"}
BOOLEAN_TYPES = {"BOOLEAN", "BOOL"}


# Column name signals for tiered ranking — matched on word tokens (split by _)
# so "notification_count" does NOT match "count" as a standalone business metric
# Tier 1: standalone tokens that directly represent a measurable business quantity
_TIER1_SIGNALS = {"amount", "value", "price", "cost", "revenue", "balance",
                  "points", "earned", "quantity", "spent"}
# Tier 2: user lifecycle / behaviour tokens
_TIER2_SIGNALS = {"first", "new", "referral", "conversion", "repeat",
                  "verified", "linked", "enabled"}
# Technical tokens — deprioritise regardless of other signals
_TECHNICAL_SIGNALS = {"latency", "timeout", "retry", "attempt", "hash",
                      "checksum", "fingerprint", "trace", "ms", "sec", "hrs"}

def _build_property_metrics_context(table_info: dict) -> dict:
    """
    Summarise event-level properties by type so the LLM can suggest
    property-level metrics (aggregations on numeric props, funnel gates on booleans).

    Only surfaces the most business-meaningful properties (max 6 numeric + 6 boolean)
    so the LLM isn't overwhelmed and skips them in favour of funnel metrics.
    """
    col_meta     = {c["name"]: c for c in table_info.get("columns", [])}
    event_props  = table_info.get("event_properties", {})   # event -> [col_names]

    # Invert: col -> list of events it appears on
    col_to_events: dict[str, list] = {}
    for evt, props in event_props.items():
        for col_name in props:
            col_to_events.setdefault(col_name, []).append(evt)

    def _tier(col_name: str) -> int:
        tokens = set(col_name.lower().split("_"))
        if tokens & _TECHNICAL_SIGNALS:
            return 3   # deprioritise
        if tokens & _TIER1_SIGNALS:
            return 0   # highest priority — direct business quantity
        if tokens & _TIER2_SIGNALS:
            return 1   # lifecycle / behaviour signal
        return 2       # everything else

    numeric_props = []
    boolean_props = []

    for col_name, events in col_to_events.items():
        col      = col_meta.get(col_name, {})
        col_type = col.get("type", "").upper().split("(")[0].strip()
        sample   = col.get("sample_values", [])

        if col_type in NUMERIC_TYPES:
            numeric_props.append({
                "column":        col_name,
                "events":        events,
                "sample_values": sample[:5],
                "_tier":         _tier(col_name),
            })
        elif col_type in BOOLEAN_TYPES:
            boolean_props.append({
                "column": col_name,
                "events": events,
                "_tier":  _tier(col_name),
            })

    def _rank(props):
        return sorted(props, key=lambda p: p["_tier"])

    # Keep top 6 of each — enough variety without overwhelming the LLM
    def _clean(props):
        return [{k: v for k, v in p.items() if not k.startswith("_")} for p in props]

    return {
        "numeric": _clean(_rank(numeric_props)[:6]),
        "boolean": _clean(_rank(boolean_props)[:6]),
    }


def build_table_prompt(table_name: str, table_info: dict, company_hint: str,
                        matched_metrics: list | None = None) -> str:
    cols = []
    for col in table_info["columns"]:
        entry = {
            "name":        col["name"],
            "type":        col["type"],
            "cardinality": col.get("cardinality", "?"),
            "null_pct":    col.get("null_pct", 0),
        }
        if col.get("sample_values"):
            entry["sample_values"] = col["sample_values"][:20]
        cols.append(entry)

    matched_metrics = matched_metrics or []

    packet = {
        "table_name":     table_name,
        "row_count":      table_info["row_count"],
        "unique_users":   table_info.get("unique_users", 0),
        "date_range":     table_info.get("date_range", {}),
        "is_event_table": table_info["is_event_table"],
        "event_counts":   table_info.get("event_counts", {}),
        "event_semantics": table_info.get("event_semantics", {}),
        "flow_candidates": table_info.get("flow_candidates", []),
        "columns":        cols,
        "sample_rows":    table_info["sample_rows"][:3],
    }

    # ── Metrics instruction — library-matched vs free-form ────────────────────
    if matched_metrics:
        # Format the library metrics so the LLM can write a description for each
        lib_items = []
        for m in matched_metrics:
            lib_items.append({
                "id":               m["id"],
                "name":             m["name"],
                "category":         m["category"],
                "aarrr":            m["aarrr"],
                "type":             m["type"],
                "description_hint": m["description_hint"],
                "sql":              m["sql"],
            })

        metrics_instruction = f"""
The following {len(matched_metrics)} metrics have been matched to this table from our standard
metrics library. The SQL is already correct — do NOT change it.

Your ONLY job for suggested_metrics is to write a 1–2 sentence "description" for each metric
that explains:
  1. What this metric measures in the context of THIS specific product/company
  2. Why it matters and what a bad value would signal

Return EXACTLY {len(matched_metrics)} metrics in "suggested_metrics" — one per library entry,
in the same order. For each entry:
  - "name"     → copy verbatim from the library entry
  - "id"       → copy verbatim from the library entry
  - "category" → copy verbatim
  - "aarrr"    → copy verbatim
  - "type"     → copy verbatim
  - "sql_hint" → copy the "sql" value verbatim — do NOT modify
  - "description" → YOUR contribution: 1–2 sentences, textual, no numbers

Pre-matched metrics from library:
{json.dumps(lib_items, indent=2)}"""
    else:
        # Fallback: LLM discovers metrics from raw signals
        property_metrics_ctx = _build_property_metrics_context(table_info) \
            if table_info.get("is_event_table") else {}
        packet["product_signals"]          = table_info.get("product_signals", {})
        packet["property_metrics_context"] = property_metrics_ctx

        metrics_instruction = """
No pre-matched metrics found. Suggest 5–8 metrics. Rules:

If product_signals is populated:
  - auto_funnels: for each pair, create a conversion rate metric. Description textual (no numbers).
    IMPORTANT: only use pairs from auto_funnels — do NOT invent funnels by comparing adoption rates
  - feature_adoption: INDEPENDENT per-event reach — do NOT compare two events to infer a funnel
  - retention: D1/D7/D30 if present — describe why it matters, not the actual number
  - flow_candidates: prefer metrics built from a single coherent flow (entry_events -> success_events)
    instead of mixing unrelated verification / onboarding / transaction events

If product_signals is empty, infer from event_counts and column shapes:
  - Funnel patterns in event names (initiated→completed, started→success)
  - DAU/WAU/MAU if time + user columns exist
  - Revenue metrics if amount/value columns exist

Property-level metrics — MANDATORY if property_metrics_context is non-empty:
  Include at least 2 metrics from property_metrics_context FIRST (before funnel/retention).
  - numeric: SUM/AVG per user/event scoped to the events in that property's "events" field
  - boolean: funnel gate or segment split (WHERE col = true)

Always:
- sql_hint must use ONLY event names visible in event_counts — never invent event names
- Description is textual — no numbers. Numbers belong in sql_hint."""

    return f"""Analyse this database table and generate catalog documentation.

Company context: {company_hint or "unknown — infer from the data"}

Table data:
{json.dumps(packet, indent=2, default=str)}

Return a JSON object with EXACTLY this structure:

{{
  "table_name":         "{table_name}",
  "table_display_name": "Human Readable Table Name",
  "table_description":  "One sentence: what is stored and its business purpose",
  "table_type":         "event_log" or "dimension" or "fact" or "lookup",

  "columns": [
    {{
      "raw_name":          "exact column name",
      "display_name":      "Human Readable Column Name",
      "description":       "What this column contains. When it is null.",
      "is_pii":            false,
      "analysis_priority": "high" or "medium" or "low" or "skip",
      "value_meanings":    {{}}
    }}
  ],

  "suggested_metrics": [
    {{
      "name":        "Metric Name",
      "description": "What this measures and why it matters",
      "sql_hint":    "SELECT ..."
    }}
  ]
}}

Rules for analysis_priority:
- "high"   → key business dimension, good for grouping/filtering
- "medium" → sometimes useful (amounts, durations, counts)
- "low"    → technical metadata, rarely useful analytically
- "skip"   → never analytically useful (trace IDs, raw UUIDs, session IDs)

Rules for is_pii:
- true for: names, phone numbers, emails, national IDs, precise GPS
- false for: aggregated/anonymised values, IDs that don't identify a person alone

Rules for value_meanings:
- Populate only when sample_values exist AND values have clear business meaning
- Leave as {{}} if values are self-explanatory or too numerous

Rules for display_name:
- Expand abbreviations using the actual data as context
- Do NOT use a static dictionary — infer from the data
{metrics_instruction}

If company context is "unknown", infer the industry from columns and sample data."""


# ── Call B: events + per-event properties ─────────────────────────────────────

def build_events_prompt(table_name: str, table_info: dict, company_hint: str) -> str:
    col_meta = {c["name"]: c for c in table_info["columns"]}

    # Build per-event property detail: only event-specific columns (already
    # filtered by scanner to sparse columns with null_pct > 5%)
    event_properties_detail = {}
    for evt, prop_names in table_info.get("event_properties", {}).items():
        event_properties_detail[evt] = [
            {
                "name":          p,
                "type":          col_meta[p]["type"],
                "sample_values": col_meta[p].get("sample_values", [])[:8],
            }
            for p in prop_names if p in col_meta
        ]

    packet = {
        "table_name":       table_name,
        "event_name_col":   table_info.get("event_name_col"),
        "event_names":      table_info.get("event_names", []),
        "event_semantics":  table_info.get("event_semantics", {}),
        "flow_candidates":  table_info.get("flow_candidates", []),
        "event_properties": event_properties_detail,
        "event_groups":     table_info.get("event_groups", {}),
    }

    event_list = table_info.get("event_names", [])

    return f"""You are documenting the events in a product analytics event log table.
Company context: {company_hint or "unknown — infer from the data"}

Event data:
{json.dumps(packet, indent=2, default=str)}

Return a JSON object with EXACTLY this structure — an array of ALL {len(event_list)} events:

{{
  "events": [
    {{
      "raw_name":      "exact event name from event_names list",
      "display_name":  "Human Readable Event Name",
      "description":   "When this fires and what it means for the business.",
      "analysis_tags": one or more of ["funnel", "retention", "segmentation", "rca", "cohort"],
      "groups":        {{"event_category": "onboarding", "feature_area": "kyc"}},
      "properties": [
        {{
          "raw_name":      "exact column name from event_properties",
          "display_name":  "Human Readable Property Name",
          "description":   "What this captures for THIS specific event.",
          "value_meanings": {{}}
        }}
      ]
    }}
  ]
}}

CRITICAL rules:
- You MUST include ALL {len(event_list)} events: {json.dumps(event_list)}
- Do NOT skip any event, even if it has no special properties (use empty properties array)
- For "groups": copy the values directly from event_groups for that event — do not invent values
- Properties come ONLY from the event_properties map for that event — do not invent columns
- Use event_semantics / flow_candidates to keep intermediate steps separate from terminal success events
- Write description from the perspective of the specific event context
  (e.g. failure_reason for vkyc_failed = "why the vKYC call failed", not generic)
- Populate value_meanings only when sample_values exist and have clear business meaning"""


# ── Call C: business context (glossary, exclusions, custom events) ─────────────

def build_business_context_prompt(raw_schema: dict, company_hint: str) -> str:
    primary_table_name, primary_table_info = _guess_primary_event_table(raw_schema)
    primary_columns = primary_table_info.get("columns", []) if primary_table_info else []
    event_col = primary_table_info.get("event_name_col", "event_name") if primary_table_info else "event_name"
    user_col = _find_exact_signal_col(primary_columns, USER_SIGNALS) or "user_id"
    time_col = _find_exact_signal_col(primary_columns, TIME_SIGNALS) or "timestamp"

    # Summarise what we know: all event names, all column names, row counts
    summary = {}
    for table_name, table_info in raw_schema.items():
        summary[table_name] = {
            "row_count":    table_info.get("row_count", 0),
            "is_event_table": table_info.get("is_event_table", False),
            "event_names":  table_info.get("event_names", []),
            "event_semantics": table_info.get("event_semantics", {}),
            "flow_candidates": table_info.get("flow_candidates", []),
            "columns":      [
                {
                    "name":          c["name"],
                    "type":          c["type"],
                    "sample_values": c.get("sample_values", [])[:10],
                }
                for c in table_info.get("columns", [])
            ],
            "sample_rows": table_info.get("sample_rows", [])[:3],
        }

    return f"""You are a senior analytics engineer analysing a product database.
Company context: {company_hint or "unknown — infer from the data"}

Database schema summary:
{json.dumps(summary, indent=2, default=str)}

Based on this data, generate the business context block that will be injected into every
analytics query and LLM prompt for this product.

Return a JSON object with EXACTLY this structure:

{{
  "industry":   "fintech neobank" (or healthcare, edtech, ecommerce, etc — infer from data),
  "company":    "Company Name or Unknown",

  "custom_events": [
    {{
      "name":        "snake_case_name",
      "sql":         "{event_col} IN ('...')",
      "description": "What this named segment means for the business"
    }}
  ],

  "exclusions": {{
    "always_filter": [
      "user_id NOT LIKE 'test_%'"
    ],
    "glossary": [
        {{
          "term":        "DAU",
          "description": "Number of unique users who opened the app on a given day",
          "sql":         "COUNT(DISTINCT {user_col}) FILTER (WHERE {event_col} = 'app_open')"
        }}
      ],
    "conventions": [
      "Default time window: last 7 days unless user specifies otherwise"
    ]
  }}
}}

Rules for custom_events:
- Define 3–6 meaningful user lifecycle segments relevant to THIS industry
- The "sql" field is a WHERE clause fragment. One strict rule:
  {event_col} can ONLY be compared to values from the event_names list above.
  Column names are for additional filters — never as event name values.

  INVALID: {event_col} IN ('<column_name>')
           ↑ wrong — a column name is not a valid event name

  VALID:   {event_col} IN ('<event_from_list>') AND <status_column> = 'SUCCESS'
           ↑ correct — event name from the list, column used as an additional filter

  Always add column filters when they meaningfully narrow the definition
  (e.g. status = 'SUCCESS', amount > 0, is_verified = true).
  Use sample_values from the schema to pick the right filter values.
- Examples for fintech: active_user, activated_user, paying_user, churned_user
- Examples for healthcare: engaged_patient, onboarded_provider, active_subscriber
- Examples for edtech: active_learner, course_completer, paying_student
- **Lineage / follow-ups:** When a custom event implies extra equality filters (e.g. status = SUCCESS)
  that are not always copied into the orchestrator ``filters`` slot, either:
  (a) add a structured ``builder_definition`` with ``groups[].filters`` using ``op: "="`` for those
  fields, and/or (b) set ``rollup_filters`` on that custom event (same shape as orchestrator filters,
  string values). The runtime uses these for roll-forward merge on same-event follow-ups.
  Optional top-level sibling of ``custom_events``: ``"lineage": {{"rollforward_keys": ["transaction_status"],
  "rollup_analysis_types": ["metric", "segment"], "skip_metric_variants": ["status_rate", "event_count"]}}``
  to tune which analysis types participate and which keys may be copied from the prior turn's filters.

Rules for always_filter:
- Patterns to exclude internal/test traffic (look at user_id / email patterns in sample data)
- Only add filters that make sense for this dataset — do not invent

Rules for glossary:
- Glossary is for BUSINESS CONCEPTS — composite definitions that combine multiple events or require a formula.
- NEVER put individual event names as glossary terms. Events are already documented in the catalog.
- A glossary term answers "how does the business define X?" not "what does event Y do?"
- Good glossary terms: DAU, MAU, Activated User, Churned User, Conversion Rate, LTV, D7 Retention
- Bad glossary terms: app_open, kyc_completed, payment_success (these are events, not concepts)
- Each glossary entry MUST have both:
    "description": plain English — what does this term mean to a business stakeholder?
    "sql": a DuckDB-safe SQL expression or FILTER clause fragment using real schema columns
- 4–8 terms. Relevant to THIS industry — infer the right concepts from the data.

Rules for conventions:
- DuckDB syntax hints
- Currency/unit conventions inferred from data (look at amount columns and sample values)
- Default time window
- Date grouping pattern

Output only valid JSON. No markdown. No explanation."""


# ── Source-of-truth conflict detection ───────────────────────────────────────

def _extract_event_names_from_sql(sql: str, valid_events: set) -> frozenset:
    """Return the set of known event names referenced in a SQL snippet."""
    if not sql:
        return frozenset()
    quoted = re.findall(r"'([^']+)'", sql)
    return frozenset(q for q in quoted if q in valid_events)


def detect_metric_conflicts(catalog: dict) -> list[dict]:
    """
    Scan the catalog for metric/glossary/custom-event definitions that reference
    the same events but may measure them differently.

    Returns a list of conflict dicts, each with:
      type      — "duplicate_metric_name" | "overlapping_event_set" |
                  "glossary_metric_mismatch" | "custom_event_metric_overlap"
      severity  — "error" | "warning"
      items     — human-readable list of the conflicting entries
      reason    — one-sentence explanation of why this is a problem
    """
    from collections import defaultdict

    # Collect all known event names across all tables
    valid_events: set = {
        ename
        for tname, tdata in catalog.items()
        if not tname.startswith("__") and isinstance(tdata, dict)
        for ename in tdata.get("event_semantics", {}).keys()
    }

    # Build flat list of all metrics with their primary event fingerprint
    all_metrics: list[dict] = []
    for tname, tdata in catalog.items():
        if tname.startswith("__") or not isinstance(tdata, dict):
            continue
        for m in tdata.get("suggested_metrics", []):
            if not isinstance(m, dict):
                continue
            sql = m.get("sql_hint", "") or m.get("sql", "")
            all_metrics.append({
                "table":   tname,
                "id":      m.get("id", ""),
                "name":    m.get("name", ""),
                "source":  m.get("source", ""),
                "status":  m.get("status", ""),
                "sql":     sql,
                "events":  _extract_event_names_from_sql(sql, valid_events),
            })

    # Glossary terms with SQL
    biz = catalog.get("__business_context__", {}) or {}
    excl = biz.get("exclusions", {}) or {}
    raw_glossary = biz.get("glossary", []) or excl.get("glossary", []) or []
    glossary_items: list[dict] = []
    for item in raw_glossary:
        if not isinstance(item, dict):
            continue
        sql = item.get("sql", "")
        glossary_items.append({
            "term":   item.get("term", ""),
            "sql":    sql,
            "events": _extract_event_names_from_sql(sql, valid_events),
        })

    # Custom events
    custom_events = biz.get("custom_events", []) or []

    conflicts: list[dict] = []

    # ── Check 1: multiple metrics referencing the same event set ─────────────
    by_events: dict = defaultdict(list)
    for m in all_metrics:
        if m["events"]:
            by_events[m["events"]].append(m)

    for events_key, group in by_events.items():
        if len(group) < 2:
            continue
        names = [g["name"] for g in group]
        unique_names = set(names)
        if len(unique_names) == 1:
            # Same name, same events but potentially different SQL → duplicate definition
            unique_sqls = {g["sql"] for g in group if g["sql"]}
            if len(unique_sqls) > 1:
                conflicts.append({
                    "type":    "duplicate_metric_name",
                    "severity": "error",
                    "items":   [f"{g['name']} (table: {g['table']}, source: {g['source']})" for g in group],
                    "reason":  (
                        f"Metric '{names[0]}' has {len(group)} definitions with different SQL. "
                        f"Queries will return inconsistent values depending on which definition is used."
                    ),
                })
        else:
            # Different names, same event(s) — may measure the same concept differently
            conflicts.append({
                "type":    "overlapping_event_set",
                "severity": "warning",
                "items":   [f"{g['name']} (id: {g['id']}, source: {g['source']})" for g in group],
                "shared_events": sorted(events_key),
                "reason":  (
                    f"Metrics [{', '.join(names)}] all reference event(s) "
                    f"{sorted(events_key)} but have different names. "
                    f"Verify each measures a distinct concept to prevent user confusion."
                ),
            })

    # ── Check 2: glossary term matches a metric name with different events ────
    metric_by_name_lower = {m["name"].lower(): m for m in all_metrics}
    for gt in glossary_items:
        term_lower = gt["term"].lower()
        if term_lower not in metric_by_name_lower:
            continue
        m = metric_by_name_lower[term_lower]
        if gt["events"] and m["events"] and gt["events"] != m["events"]:
            conflicts.append({
                "type":    "glossary_metric_mismatch",
                "severity": "warning",
                "items":   [f"Glossary: {gt['term']}", f"Metric: {m['name']} (table: {m['table']})"],
                "reason":  (
                    f"Glossary term '{gt['term']}' and metric '{m['name']}' share a name "
                    f"but reference different events "
                    f"(glossary: {sorted(gt['events'])}, metric: {sorted(m['events'])}). "
                    f"Align them so natural-language answers are consistent."
                ),
            })

    # ── Check 3: custom event name similar to a metric, overlapping events ───
    for ce in custom_events:
        if not isinstance(ce, dict):
            continue
        ce_name   = ce.get("name", "").lower()
        ce_events = _extract_event_names_from_sql(ce.get("sql", ""), valid_events)
        if not ce_events:
            continue
        for m in all_metrics:
            if not m["events"]:
                continue
            # Flag when names are similar AND event sets partially overlap
            names_similar = (
                ce_name in m["name"].lower() or m["name"].lower() in ce_name
            )
            events_overlap = bool(ce_events & m["events"])
            events_differ  = ce_events != m["events"]
            if names_similar and events_overlap and events_differ:
                conflicts.append({
                    "type":    "custom_event_metric_overlap",
                    "severity": "warning",
                    "items":   [f"Custom event: {ce.get('name', '')}", f"Metric: {m['name']}"],
                    "reason":  (
                        f"Custom event '{ce.get('name')}' and metric '{m['name']}' "
                        f"have similar names but reference overlapping yet distinct event sets "
                        f"(custom: {sorted(ce_events)}, metric: {sorted(m['events'])}). "
                        f"Ensure the intended population is the same."
                    ),
                })

    return conflicts


# ── Main ──────────────────────────────────────────────────────────────────────

def run_catalog_agent(raw_schema: dict, company_hint: str = "") -> dict:

    client, provider = get_llm_client()
    catalog = {}
    tables = list(raw_schema.items())

    print(f"\nRunning Catalog Agent on {len(tables)} table(s)...")

    for i, (table_name, table_info) in enumerate(tables):
        is_event_table = table_info.get("is_event_table") and table_info.get("event_names")

        try:
            # ── Pre-match metrics from library (zero LLM, zero cost) ───────
            matched_metrics = []
            metric_diagnostics = []
            if is_event_table:
                metric_diagnostics = _diagnose_metrics(table_info, table_name,
                                                      industry=company_hint or None)
                matched_metrics = [
                    metric for metric in metric_diagnostics
                    if metric.get("status") == "approved"
                ]
                # Strip internal debug keys before passing to LLM
                matched_metrics = [
                    {k: v for k, v in m.items() if not k.startswith("_")}
                    for m in matched_metrics
                ]
                if matched_metrics:
                    print(f"  [{i+1}/{len(tables)}] {table_name} — "
                          f"{len(matched_metrics)} metrics matched from library")

            # ── Call A: columns + metrics ──────────────────────────────────
            print(f"  [{i+1}/{len(tables)}] {table_name} — columns...", end=" ", flush=True)
            table_prompt = build_table_prompt(table_name, table_info, company_hint,
                                              matched_metrics=matched_metrics)
            raw_text = call_llm(client, provider, SYSTEM_PROMPT, table_prompt)
            result = parse_json(raw_text)
            result["event_semantics"] = table_info.get("event_semantics", {})
            result["flow_candidates"] = table_info.get("flow_candidates", [])
            result["capabilities"] = table_info.get("capabilities", {})
            result["suppressed_metrics"] = [
                {
                    "id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "category": m.get("category", ""),
                    "aarrr": m.get("aarrr", ""),
                    "type": m.get("type", ""),
                    "sql_hint": m.get("sql", ""),
                    "validation_reasons": m.get("validation_reasons", []),
                }
                for m in metric_diagnostics
                if m.get("status") == "suppressed"
            ]
            print(f"✓  {len(result.get('columns', []))} columns", end="")

            # ── Merge library metadata back into suggested_metrics ─────────
            # The LLM may drop id/category/aarrr/type. Re-attach from the
            # matched_metrics list so the catalog has the full enriched record.
            if matched_metrics:
                lib_by_name = {m["name"]: m for m in matched_metrics}
                enriched = []
                for m in result.get("suggested_metrics", []):
                    lib = lib_by_name.get(m.get("name", ""), {})
                    enriched.append({
                        "id":          lib.get("id", ""),
                        "name":        m.get("name", ""),
                        "category":    lib.get("category", ""),
                        "aarrr":       lib.get("aarrr", ""),
                        "type":        lib.get("type", ""),
                        "description": m.get("description", ""),
                        "sql_hint":    m.get("sql_hint", lib.get("sql", "")),
                        "status":      "approved",
                        "confidence":  "high",
                        "source":      "library_match",
                        "validation_reasons": [],
                    })
                result["suggested_metrics"] = enriched
            else:
                # LLM-suggested metrics are candidates by default.
                candidate = []
                for m in result.get("suggested_metrics", []):
                    candidate.append({
                        "id": m.get("id", ""),
                        "name": m.get("name", ""),
                        "category": m.get("category", ""),
                        "aarrr": m.get("aarrr", m.get("category", "")),
                        "type": m.get("type", "ratio"),
                        "description": m.get("description", ""),
                        "sql_hint": m.get("sql_hint", ""),
                        "status": "candidate",
                        "confidence": "medium",
                        "source": "llm_suggested",
                        "validation_reasons": ["Suggested without library match; requires review."],
                    })
                result["suggested_metrics"] = candidate

            # ── Call B: events (only for event tables) ─────────────────────
            if is_event_table:
                print(f"\n  [{i+1}/{len(tables)}] {table_name} — events...", end=" ", flush=True)
                events_prompt = build_events_prompt(table_name, table_info, company_hint)
                raw_events = call_llm(client, provider, SYSTEM_PROMPT, events_prompt)
                events_result = parse_json(raw_events)
                result["events"] = events_result.get("events", [])
                print(f"✓  {len(result['events'])} events")
            else:
                result["events"] = []
                print()

            catalog[table_name] = result

        except json.JSONDecodeError as e:
            print(f"\n  ✗  JSON parse error — {e}")
            catalog[table_name] = {"error": f"JSON parse error: {e}"}
        except Exception as e:
            print(f"\n  ✗  {e}")
            catalog[table_name] = {"error": str(e)}

    # ── Call C: business context (runs once for the whole DB) ─────────────────
    print(f"\n  [C] Generating business context (glossary, exclusions, custom events)...", end=" ", flush=True)
    try:
        ctx_prompt = build_business_context_prompt(raw_schema, company_hint)
        raw_ctx = call_llm(client, provider, SYSTEM_PROMPT, ctx_prompt)
        biz_context = parse_json(raw_ctx)

        primary_table_name, primary_table_info = _guess_primary_event_table(raw_schema)
        primary_columns = primary_table_info.get("columns", []) if primary_table_info else []
        allowed_columns = {c["name"].lower() for c in primary_columns}
        event_col = primary_table_info.get("event_name_col", "event_name") if primary_table_info else "event_name"
        time_col = _find_exact_signal_col(primary_columns, TIME_SIGNALS)

        # ── Validate custom_events SQL — strip hallucinated event names ────────
        all_event_names = {
            name
            for tinfo in raw_schema.values()
            for name in tinfo.get("event_names", [])
        }
        validated_custom_events = []
        for ce in biz_context.get("custom_events", []):
            sql = ce.get("sql", "")
            cleaned_sql = _sanitize_custom_event_sql(
                sql, allowed_columns, all_event_names, time_col, event_col
            )
            if not cleaned_sql:
                print(f"\n    ⚠  custom_event '{ce['name']}' is not valid for table '{primary_table_name}' — skipped")
                continue
            ce["sql"] = cleaned_sql
            validated_custom_events.append(ce)
        biz_context["custom_events"] = validated_custom_events

        validated_glossary = []
        taxonomy_present = set()
        for item in biz_context.get("exclusions", {}).get("glossary", []):
            term = item.get("term", "")
            desc = item.get("description", item.get("definition", ""))
            if _is_event_like_term(term, all_event_names):
                print(f"\n    ⚠  glossary term '{term}' looks like an event name — dropped")
                continue
            cleaned_sql = _sanitize_glossary_sql(
                item.get("sql", ""), allowed_columns, all_event_names, time_col, event_col
            )
            if item.get("sql") and not cleaned_sql:
                print(f"\n    ⚠  glossary term '{item.get('term', '')}' has invalid SQL — dropped")
                continue
            if cleaned_sql:
                item["sql"] = cleaned_sql
            cat = item.get("domain_category") or _classify_glossary_category(term, desc)
            item["domain_category"] = cat
            item["review_status"] = item.get("review_status", "draft")
            taxonomy_present.add(cat)
            validated_glossary.append(item)
        # Ensure at least one activation/retention/revenue concept when possible.
        if validated_glossary:
            missing = [x for x in ("activation", "retention", "revenue") if x not in taxonomy_present]
            for x in missing:
                validated_glossary.append({
                    "term": x.replace("_", " ").title(),
                    "description": f"Standard {x} definition — please curate for this business.",
                    "sql": "",
                    "domain_category": x,
                    "review_status": "draft",
                })
        biz_context.setdefault("exclusions", {})["glossary"] = validated_glossary

        catalog["__business_context__"] = biz_context
        industry = biz_context.get("industry", "unknown")
        n_events = len(validated_custom_events)
        n_glossary = len(biz_context.get("exclusions", {}).get("glossary", []))
        print(f"✓  industry={industry}  {n_events} custom events  {n_glossary} glossary terms")
    except Exception as e:
        print(f"\n  ✗  Business context failed — {e}  (defaults will be used)")
        catalog["__business_context__"] = {}

    # ── Source-of-truth conflict detection ────────────────────────────────────
    # Runs after all tables + business context are populated so it can see
    # every metric, glossary term, and custom event in one pass.
    print(f"\n  [D] Checking for metric conflicts...", end=" ", flush=True)
    try:
        conflicts = detect_metric_conflicts(catalog)
        catalog["__conflicts__"] = conflicts
        if conflicts:
            n_errors   = sum(1 for c in conflicts if c.get("severity") == "error")
            n_warnings = sum(1 for c in conflicts if c.get("severity") == "warning")
            print(f"⚠  {len(conflicts)} conflict(s) found ({n_errors} errors, {n_warnings} warnings)")
            for c in conflicts:
                icon = "🔴" if c.get("severity") == "error" else "🟡"
                print(f"    {icon} [{c['type']}] {c['reason'][:120]}")
        else:
            print("✓  no conflicts")
    except Exception as e:
        print(f"✗  {e}")
        catalog["__conflicts__"] = []

    return catalog


if __name__ == "__main__":
    import sys

    schema_path  = sys.argv[1] if len(sys.argv) > 1 else "raw_schema.json"
    company_hint = sys.argv[2] if len(sys.argv) > 2 else ""

    if not Path(schema_path).exists():
        print(f"Error: {schema_path} not found. Run scanner.py first.")
        raise SystemExit(1)

    raw_schema = json.loads(Path(schema_path).read_text())
    catalog    = run_catalog_agent(raw_schema, company_hint)

    Path("catalog.json").write_text(json.dumps(catalog, indent=2))
    print("\n→ catalog.json written")

    for table_name, entry in catalog.items():
        if "error" in entry:
            print(f"  ✗ {table_name}: {entry['error'][:60]}")
        else:
            print(f"  ✓ {table_name}: {entry.get('table_description','')[:70]}")
