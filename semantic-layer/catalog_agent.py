"""
catalog_agent.py — Step 2

For regular tables: 1 LLM call (columns + metrics).
For event tables:   2 LLM calls:
  - Call A: table metadata + columns + suggested metrics
  - Call B: all events with per-event properties

Splitting prevents output token truncation when there are many events
with rich property sets (e.g. 27 events × 90 sparse columns).
"""

import json
import re
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── LLM Client setup ──────────────────────────────────────────────────────────

def get_llm_client():
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            print("Using OpenAI (gpt-4o-mini)")
            return client, "openai"
        except ImportError:
            print("openai package not installed. Run: pip install openai")
            raise

    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            print("Using Gemini (gemini-1.5-flash)")
            return genai, "gemini"
        except ImportError:
            print("google-generativeai package not installed. "
                  "Run: pip install google-generativeai")
            raise

    raise ValueError(
        "No API key found. Set OPENAI_API_KEY or GEMINI_API_KEY in your .env file"
    )


def call_llm(client, provider: str, system_prompt: str, user_prompt: str) -> str:
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


def build_table_prompt(table_name: str, table_info: dict, company_hint: str) -> str:
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

    property_metrics_ctx = _build_property_metrics_context(table_info) \
        if table_info.get("is_event_table") else {}

    packet = {
        "table_name":             table_name,
        "row_count":              table_info["row_count"],
        "unique_users":           table_info.get("unique_users", 0),
        "date_range":             table_info.get("date_range", {}),
        "is_event_table":         table_info["is_event_table"],
        "event_counts":           table_info.get("event_counts", {}),
        "product_signals":        table_info.get("product_signals", {}),
        "property_metrics_context": property_metrics_ctx,
        "columns":                cols,
        "sample_rows":            table_info["sample_rows"][:3],
    }

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
      "sql_hint":    "COUNT(DISTINCT user_id) WHERE ..."
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

Rules for suggested_metrics — suggest 5–8, be specific and data-driven:

If product_signals is populated, use it — these are PRE-COMPUTED facts, not guesses:
  - auto_funnels: real funnel steps with actual conversion_pct. For each, create a conversion rate metric.
    Description should explain WHAT the metric measures and WHY it matters — no numbers in description.
    Put the sql_hint as: COUNT(DISTINCT user_id WHERE event='<to>') / NULLIF(COUNT(DISTINCT user_id WHERE event='<from>'),0)
    IMPORTANT: only use pairs that appear in auto_funnels — do NOT invent funnels by comparing adoption rates
  - feature_adoption: each event's adoption_pct is INDEPENDENT — it is NOT relative to any other event.
    Do NOT compare two events' adoption rates to infer a funnel. Use it only to describe feature reach.
    Description should explain what it means if this number is high or low for the business.
  - retention: real D1/D7/D30 numbers if present. Description should explain why retention matters here.

If product_signals is empty, infer from event_counts and column shapes:
  - Look for funnel patterns in event names (initiated→completed, started→success, opened→submitted)
  - Use count ratios to estimate conversion (high count event → lower count event = likely funnel)
  - Suggest DAU/WAU/MAU if a time column and user column exist
  - Suggest revenue/transaction metrics if amount or value columns exist

Property-level metrics — MANDATORY if property_metrics_context is non-empty:
  You MUST include at least 2 metrics derived from property_metrics_context.
  These are listed FIRST before funnel/retention metrics so you don't run out of slots.

  - numeric properties: each is a measurement opportunity tied to specific events.
    For each, suggest one or more of: SUM per user, AVG per event, distribution (P50/P90).
    e.g. "jewels_earned" numeric on transaction events → "Avg Jewels Per Transaction", "Total Loyalty Points Per User"
    e.g. "amount" numeric → "Avg Transaction Value", "Revenue per Active User"
  - boolean properties: each is a funnel gate or segmentation signal.
    Suggest WHERE <col> = true / false conversion or segment-split metrics.
    e.g. "is_first_transaction" boolean → "First Transaction Rate (% of transactions that are a user's first)"
  - Always scope the sql_hint to the specific events listed in the property's "events" field

Always:
- sql_hint must use ONLY event names visible in event_counts — never invent event names
- Description is always textual — explain what the metric measures and why it matters, never put numbers in it
- Numbers (conversion_pct, adoption_pct, retention %) belong in sql_hint as comments, not description
- Each metric must be immediately actionable: an analyst should be able to run it today

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
- Write description from the perspective of the specific event context
  (e.g. failure_reason for vkyc_failed = "why the vKYC call failed", not generic)
- Populate value_meanings only when sample_values exist and have clear business meaning"""


# ── Call C: business context (glossary, exclusions, custom events) ─────────────

def build_business_context_prompt(raw_schema: dict, company_hint: str) -> str:
    # Summarise what we know: all event names, all column names, row counts
    summary = {}
    for table_name, table_info in raw_schema.items():
        summary[table_name] = {
            "row_count":    table_info.get("row_count", 0),
            "is_event_table": table_info.get("is_event_table", False),
            "event_names":  table_info.get("event_names", []),
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
      "sql":         "event_name IN ('...')",
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
        "sql":         "COUNT(DISTINCT user_id) WHERE event_name = 'app_open' GROUP BY DATE(timestamp)"
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
  event_name can ONLY be compared to values from the event_names list above.
  Column names are for additional filters — never as event name values.

  INVALID: event_name IN ('<column_name>')
           ↑ wrong — a column name is not a valid event name

  VALID:   event_name IN ('<event_from_list>') AND <status_column> = 'SUCCESS'
           ↑ correct — event name from the list, column used as an additional filter

  Always add column filters when they meaningfully narrow the definition
  (e.g. status = 'SUCCESS', amount > 0, is_verified = true).
  Use sample_values from the schema to pick the right filter values.
- Examples for fintech: active_user, activated_user, paying_user, churned_user
- Examples for healthcare: engaged_patient, onboarded_provider, active_subscriber
- Examples for edtech: active_learner, course_completer, paying_student

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
    "sql": the exact formula or condition an analyst would write
- 4–8 terms. Relevant to THIS industry — infer the right concepts from the data.

Rules for conventions:
- DuckDB syntax hints
- Currency/unit conventions inferred from data (look at amount columns and sample values)
- Default time window
- Date grouping pattern

Output only valid JSON. No markdown. No explanation."""


# ── Main ──────────────────────────────────────────────────────────────────────

def run_catalog_agent(raw_schema: dict, company_hint: str = "") -> dict:

    client, provider = get_llm_client()
    catalog = {}
    tables = list(raw_schema.items())

    print(f"\nRunning Catalog Agent on {len(tables)} table(s)...")

    for i, (table_name, table_info) in enumerate(tables):
        is_event_table = table_info.get("is_event_table") and table_info.get("event_names")

        try:
            # ── Call A: columns + metrics ──────────────────────────────────
            print(f"  [{i+1}/{len(tables)}] {table_name} — columns...", end=" ", flush=True)
            table_prompt = build_table_prompt(table_name, table_info, company_hint)
            raw_text = call_llm(client, provider, SYSTEM_PROMPT, table_prompt)
            result = parse_json(raw_text)
            print(f"✓  {len(result.get('columns', []))} columns", end="")

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

        # ── Validate custom_events SQL — strip hallucinated event names ────────
        all_event_names = {
            name
            for tinfo in raw_schema.values()
            for name in tinfo.get("event_names", [])
        }
        validated_custom_events = []
        for ce in biz_context.get("custom_events", []):
            sql = ce.get("sql", "")
            # Extract quoted strings used after event_name IN (...) or event_name = '...'
            import re
            claimed = set(re.findall(r"event_name\s*(?:=|IN)\s*\(?\s*'([^']+)'", sql))
            claimed |= set(re.findall(r"'([^']+)'\s*(?:,\s*'[^']*')*\s*\)", sql))
            bad = claimed - all_event_names
            if bad:
                print(f"\n    ⚠  custom_event '{ce['name']}' references non-existent event(s): {bad} — skipped")
                continue
            validated_custom_events.append(ce)
        biz_context["custom_events"] = validated_custom_events

        catalog["__business_context__"] = biz_context
        industry = biz_context.get("industry", "unknown")
        n_events = len(validated_custom_events)
        n_glossary = len(biz_context.get("exclusions", {}).get("glossary", []))
        print(f"✓  industry={industry}  {n_events} custom events  {n_glossary} glossary terms")
    except Exception as e:
        print(f"\n  ✗  Business context failed — {e}  (defaults will be used)")
        catalog["__business_context__"] = {}

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
