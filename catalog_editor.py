"""
catalog_editor.py — Visual editor for the analytics catalog.

Run with:
    streamlit run catalog_editor.py
"""
from __future__ import annotations

import json
import re
import sys
import streamlit as st
import altair as alt
import pandas as pd
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "semantic-layer"))

CATALOG_CANDIDATES = [
    ROOT / "catalog.json",
    ROOT / "semantic-layer" / "catalog.json",
]

ALL_TAGS   = ["funnel", "retention", "cohort", "rca", "segmentation"]
TAG_COLORS = {
    "funnel":       "#4f8ef7",
    "retention":    "#22c55e",
    "cohort":       "#a855f7",
    "rca":          "#f97316",
    "segmentation": "#06b6d4",
}
PRIORITY_COLORS = {
    "high":   ("#f59e0b", "white"),
    "medium": ("#3b82f6", "white"),
    "low":    ("#94a3b8", "white"),
    "skip":   ("#e2e8f0", "#64748b"),
}

USER_SIGNALS = {"user_id", "userid", "account_id", "customer_id",
                "member_id", "uid", "visitor_id"}
TIME_SIGNALS = {"event_time", "timestamp", "created_at", "occurred_at",
                "event_ts", "ts", "time"}
EVENT_SIGNALS = {"event_name", "event_type", "event", "action", "activity",
                 "event_key", "tracking_event"}
METRIC_BUCKET_ORDER = [
    "Acquisition", "Activation", "Engagement",
    "Retention", "Revenue", "Referral", "Other",
]
METRIC_BUCKET_COLORS = {
    "Acquisition": "#3b82f6",
    "Activation": "#8b5cf6",
    "Engagement": "#06b6d4",
    "Retention": "#22c55e",
    "Revenue": "#f59e0b",
    "Referral": "#ec4899",
    "Other": "#64748b",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_catalog_path() -> Path | None:
    for p in CATALOG_CANDIDATES:
        if p.exists():
            return p
    return None


def find_db_path(catalog_path: Path) -> Path | None:
    for p in catalog_path.parent.glob("*.duckdb"):
        return p
    for p in catalog_path.parent.parent.glob("*.duckdb"):
        return p
    return None


@st.cache_data(show_spinner=False)
def load_catalog(path: str) -> dict:
    return json.loads(Path(path).read_text())


def save_and_regenerate(catalog: dict, path: Path):
    # Write session-state business context back into catalog before saving
    biz = catalog.setdefault("__business_context__", {})
    biz["custom_events"] = st.session_state.get("custom_events", [])
    biz["exclusions"]    = st.session_state.get("exclusions", {})
    biz["industry"]      = st.session_state.get("biz_industry", biz.get("industry", ""))
    biz["company"]       = st.session_state.get("biz_company",  biz.get("company", ""))
    path.write_text(json.dumps(catalog, indent=2))
    from semantic_writer import generate_semantic_files
    generate_semantic_files(catalog, output_dir=str(path.parent))
    st.cache_data.clear()


def tag_badge(tag: str) -> str:
    color = TAG_COLORS.get(tag, "#64748b")
    return (
        f'<span style="background:{color};color:white;padding:2px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600;margin-right:4px">{tag}</span>'
    )


def priority_badge(p: str) -> str:
    bg, tc = PRIORITY_COLORS.get(p, ("#e2e8f0", "#64748b"))
    return (
        f'<span style="background:{bg};color:{tc};padding:1px 7px;'
        f'border-radius:8px;font-size:11px;font-weight:600">{p}</span>'
    )


def source_badge(edited: bool) -> str:
    if edited:
        return '<span title="Human edited" style="background:#fef3c7;color:#92400e;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid #fcd34d">✏️ edited</span>'
    return '<span title="LLM generated" style="background:#f0f9ff;color:#0369a1;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid #bae6fd">🤖 auto</span>'


def _find_signal_column(columns: list[dict], signals: set[str]) -> str:
    for col in columns:
        name = col.get("raw_name", "").lower()
        if name in signals:
            return col.get("raw_name", "")
    for col in columns:
        name = col.get("raw_name", "").lower()
        if set(name.split("_")) & signals:
            return col.get("raw_name", "")
    return ""


def resolve_table_signals(table_data: dict) -> dict:
    columns = table_data.get("columns", [])
    return {
        "event_col": _find_signal_column(columns, EVENT_SIGNALS),
        "time_col": _find_signal_column(columns, TIME_SIGNALS),
        "user_col": _find_signal_column(columns, USER_SIGNALS),
    }

def _funnel_preview_sql(table_name: str, user_col: str, event_col: str, steps: list[str]) -> str:
    """
    Build a simple deterministic conversion SQL for a saved funnel:
    first step users -> last step users conversion.
    """
    if not table_name or not user_col or not event_col or len(steps) < 2:
        return ""
    first_evt = steps[0].replace("'", "''")
    last_evt = steps[-1].replace("'", "''")
    step_vals = ["'" + s.replace("'", "''") + "'" for s in steps]
    in_clause = ", ".join(step_vals)
    return (
        f'SELECT\n'
        f'  COUNT(DISTINCT CASE WHEN {event_col} = \'{first_evt}\' THEN {user_col} END) AS entered,\n'
        f'  COUNT(DISTINCT CASE WHEN {event_col} = \'{last_evt}\' THEN {user_col} END) AS completed,\n'
        f'  ROUND(100.0 * COUNT(DISTINCT CASE WHEN {event_col} = \'{last_evt}\' THEN {user_col} END)\n'
        f'        / NULLIF(COUNT(DISTINCT CASE WHEN {event_col} = \'{first_evt}\' THEN {user_col} END), 0), 1) AS conversion_pct\n'
        f'FROM "{table_name}"\n'
        f'WHERE {event_col} IN ({in_clause})\n'
    )


def metric_bucket(metric: dict) -> str:
    for key in ("aarrr", "category"):
        value = (metric.get(key) or "").strip()
        if not value:
            continue
        title = value.title()
        if title in METRIC_BUCKET_ORDER:
            return title
    return "Other"


def bucket_metrics(metrics: list[dict]) -> dict[str, list[tuple[int, dict]]]:
    grouped = {bucket: [] for bucket in METRIC_BUCKET_ORDER}
    for idx, metric in enumerate(metrics):
        grouped[metric_bucket(metric)].append((idx, metric))
    return {bucket: items for bucket, items in grouped.items() if items}


def metric_bucket_badge(bucket: str, count: int) -> str:
    color = METRIC_BUCKET_COLORS.get(bucket, METRIC_BUCKET_COLORS["Other"])
    return (
        f'<span style="display:inline-flex;align-items:center;gap:8px;'
        f'background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.08);'
        f'border-radius:999px;padding:7px 12px;margin:0 8px 8px 0">'
        f'<span style="width:10px;height:10px;border-radius:999px;background:{color};display:inline-block"></span>'
        f'<span style="font-size:12px;color:#e2e8f0;font-weight:700">{bucket}</span>'
        f'<span style="font-size:11px;color:#94a3b8">{count}</span>'
        f'</span>'
    )


def metric_meta_badges(metric: dict) -> str:
    chips = []
    for key in ("aarrr", "category", "type"):
        value = (metric.get(key) or "").strip()
        if not value:
            continue
        chips.append(
            f'<span style="background:rgba(255,255,255,0.05);color:#cbd5e1;'
            f'border:1px solid rgba(255,255,255,0.08);padding:2px 8px;border-radius:999px;'
            f'font-size:11px;font-weight:600">{value}</span>'
        )
    return "".join(chips)


def _column_index(table_data: dict) -> dict[str, dict]:
    return {col.get("raw_name", ""): col for col in table_data.get("columns", [])}


def _event_options(table_data: dict) -> list[str]:
    """Raw event names, ordered by display_name alphabetically."""
    events = [ev for ev in table_data.get("events", []) if ev.get("raw_name")]
    return [ev["raw_name"] for ev in sorted(events, key=lambda e: e.get("display_name", e["raw_name"]))]


def _event_display_map(table_data: dict) -> dict[str, str]:
    """raw_name → display_name for events."""
    return {
        ev["raw_name"]: ev.get("display_name", ev["raw_name"])
        for ev in table_data.get("events", [])
        if ev.get("raw_name")
    }


def _field_options(table_data: dict) -> list[str]:
    """Column raw names, sorted by analysis_priority (high first), skipping internal-only columns."""
    priority_order = {"high": 0, "medium": 1, "low": 2, "skip": 3}
    cols = [
        col for col in table_data.get("columns", [])
        if col.get("raw_name") and col.get("analysis_priority", "medium") != "skip"
    ]
    cols.sort(key=lambda c: priority_order.get(c.get("analysis_priority", "medium"), 1))
    return [col["raw_name"] for col in cols]


def _field_display_map(table_data: dict) -> dict[str, str]:
    """raw_name → display_name for columns."""
    return {
        col["raw_name"]: col.get("display_name", col["raw_name"])
        for col in table_data.get("columns", [])
        if col.get("raw_name")
    }


def _numeric_field_options(table_data: dict) -> list[str]:
    """
    Catalog columns have no 'type' field (LLM strips it). Fall back to:
    1. raw_schema.json cross-reference if available
    2. Name-based heuristic (amount, value, count, total, price, sum, fee, revenue)
    """
    # Try raw_schema cross-reference
    raw_schema_path = ROOT / "raw_schema.json"
    if not raw_schema_path.exists():
        raw_schema_path = ROOT / "semantic-layer" / "raw_schema.json"

    table_name = table_data.get("table_name", "")
    raw_col_types: dict[str, str] = {}
    if raw_schema_path.exists() and table_name:
        try:
            raw_schema = json.loads(raw_schema_path.read_text())
            raw_cols = raw_schema.get(table_name, {}).get("columns", [])
            numeric_markers = ("INT", "BIGINT", "DECIMAL", "DOUBLE", "FLOAT", "NUMERIC", "REAL", "SMALLINT")
            raw_col_types = {
                c["name"]: c.get("type", "")
                for c in raw_cols
                if any(m in (c.get("type", "") or "").upper() for m in numeric_markers)
            }
        except Exception:
            pass

    if raw_col_types:
        return sorted(
            col["raw_name"] for col in table_data.get("columns", [])
            if col.get("raw_name") in raw_col_types
        )

    # Heuristic fallback
    numeric_hints = ("amount", "value", "count", "total", "price", "sum",
                     "fee", "revenue", "balance", "qty", "quantity", "score")
    return sorted(
        col["raw_name"] for col in table_data.get("columns", [])
        if col.get("raw_name") and any(h in col["raw_name"].lower() for h in numeric_hints)
    )


@st.cache_data(show_spinner=False)
def _raw_schema_sample_values() -> dict[str, dict[str, list]]:
    """Load raw_schema.json once and return {table: {col: [sample_values]}}."""
    for path in [ROOT / "raw_schema.json", ROOT / "semantic-layer" / "raw_schema.json"]:
        if path.exists():
            raw = json.loads(path.read_text())
            return {
                tname: {col["name"]: col.get("sample_values", [])
                        for col in tinfo.get("columns", [])}
                for tname, tinfo in raw.items()
            }
    return {}


def _field_value_options(table_data: dict, field_name: str) -> list[str]:
    """
    Returns known values for a column, tried in order:
    1. value_meanings keys from catalog  (e.g. platform → android/ios/web)
    2. sample_values from raw_schema.json (actual DB samples)
    """
    if not field_name:
        return []
    col = _column_index(table_data).get(field_name, {})
    # 1 — catalog value_meanings
    values = list((col.get("value_meanings", {}) or {}).keys())
    # 2 — raw_schema sample_values
    if not values:
        table_name = table_data.get("table_name", "")
        values = _raw_schema_sample_values().get(table_name, {}).get(field_name, [])
    return [str(v) for v in values[:20]]


def _parse_sql_for_builder(sql: str) -> dict:
    """
    Reverse-engineer a sql_hint string back into builder fields.
    Returns: {builder_type, primary_event, secondary_event, value_field}
    Best-effort; missing fields stay as empty string.
    """
    result = {"builder_type": "Unique users on event",
              "primary_event": "", "secondary_event": "", "value_field": ""}
    if not sql:
        return result

    # ── 1a. Active user churn: WITH prev / curr CTEs ─────────────────────────
    if (re.search(r'\bWITH\b', sql, re.IGNORECASE)
            and re.search(r'\bprev\b', sql, re.IGNORECASE)
            and re.search(r'\bcurr\b', sql, re.IGNORECASE)
            and re.search(r'churn', sql, re.IGNORECASE)):
        result["builder_type"] = "Active user churn"
        interval_m = re.search(r"INTERVAL\s+'(\d+)\s+days?'", sql, re.IGNORECASE)
        result["retention_days"] = int(interval_m.group(1)) if interval_m else 30
        ev_m = re.search(
            r'(?:event_name|event_key|event_type)\s*=\s*[\'"]([^\'"]+)[\'"]',
            sql, re.IGNORECASE,
        )
        if ev_m:
            result["primary_event"] = ev_m.group(1)
        return result

    # ── 1b. Retention: WITH cohort / first_seen CTEs ──────────────────────────
    if re.search(r'\bWITH\b.*(cohort|first_seen)\b', sql, re.IGNORECASE | re.DOTALL):
        result["builder_type"] = "Retention (N-day)"
        interval_m = re.search(r"INTERVAL\s+'(\d+)\s+days?'", sql, re.IGNORECASE)
        result["retention_days"] = int(interval_m.group(1)) if interval_m else 7
        ev_m = re.search(
            r'(?:event_name|event_key|event_type)\s*=\s*[\'"]([^\'"]+)[\'"]',
            sql, re.IGNORECASE,
        )
        if ev_m:
            result["primary_event"] = ev_m.group(1)
        return result

    # ── 2. Complex WITH CTE (not retention / churn) → Formula (SQL) ──────────
    if re.search(r'\bWITH\b\s+\w+\s+AS\s*\(', sql, re.IGNORECASE):
        result["builder_type"] = "Formula (SQL)"
        return result

    # ── 3. CASE WHEN ratio patterns ───────────────────────────────────────────
    # Find all COUNT(DISTINCT CASE WHEN event = 'X' occurrences
    case_events = re.findall(
        r'COUNT\s*\(\s*DISTINCT\s+CASE\s+WHEN\s+\S+\s*=\s*[\'"]([^\'"]+)[\'"]',
        sql, re.IGNORECASE,
    )
    has_nullif = bool(re.search(r'\bNULLIF\b', sql, re.IGNORECASE))

    if len(case_events) >= 2 and has_nullif:
        # event A / event B  →  Conversion funnel
        # In the SQL: numerator = case_events[0] (end/completed), denominator = case_events[1] (start)
        result["builder_type"]   = "Conversion funnel"
        result["secondary_event"] = case_events[0]   # numerator = end event
        result["primary_event"]   = case_events[1]   # denominator = start event
        return result

    if len(case_events) == 1 and has_nullif:
        # event A / all users  →  % of users
        result["builder_type"]  = "% of users"
        result["primary_event"] = case_events[0]
        return result

    # ── 4. Events per user: COUNT(*) / COUNT(DISTINCT user) on event ──────────
    if (re.search(r'\bCOUNT\s*\(\s*\*\s*\)', sql, re.IGNORECASE)
            and re.search(r'COUNT\s*\(\s*DISTINCT', sql, re.IGNORECASE)
            and has_nullif):
        result["builder_type"] = "Events per user"
        ev_m = re.search(
            r'(?:event_name|event_key|event_type)\s*=\s*[\'"]([^\'"]+)[\'"]',
            sql, re.IGNORECASE,
        )
        if ev_m:
            result["primary_event"] = ev_m.group(1)
        return result

    # ── 5. SUM / AVG / COUNT DISTINCT / COUNT(*) ─────────────────────────────
    sum_m  = re.search(r'\bSUM\s*\(\s*([^\)]+)\)', sql, re.IGNORECASE)
    avg_m  = re.search(r'\bAVG\s*\(\s*([^\)]+)\)', sql, re.IGNORECASE)
    dist_m = re.search(r'COUNT\s*\(\s*DISTINCT', sql, re.IGNORECASE)

    if sum_m:
        result["builder_type"] = "Sum property"
        result["value_field"]  = sum_m.group(1).strip().strip('"').strip("'")
    elif avg_m:
        result["builder_type"] = "Average property"
        result["value_field"]  = avg_m.group(1).strip().strip('"').strip("'")
    elif dist_m:
        result["builder_type"] = "Unique users on event"
    else:
        result["builder_type"] = "Event count"

    # ── 6. Extract event name(s) ──────────────────────────────────────────────
    single = re.findall(
        r'(?:event_name|event_key|event_type|event|action)\s*=\s*[\'"]([^\'"]+)[\'"]',
        sql, re.IGNORECASE,
    )
    if len(single) == 1:
        result["primary_event"] = single[0]
    elif len(single) >= 2:
        result["builder_type"]    = "Conversion funnel"
        result["primary_event"]   = single[0]
        result["secondary_event"] = single[1]

    if not result["primary_event"]:
        in_m = re.search(
            r'(?:event_name|event_key|event_type|event)\s+IN\s*\(([^\)]+)\)',
            sql, re.IGNORECASE,
        )
        if in_m:
            vals = re.findall(r"['\"]([^'\"]+)['\"]", in_m.group(1))
            if vals:
                result["primary_event"] = vals[0]
                if len(vals) >= 2:
                    result["secondary_event"] = vals[1]
                    result["builder_type"]    = "Conversion funnel"

    return result


def _parse_custom_event_where(sql: str, event_col: str | None = None) -> list[dict]:
    """
    Best-effort parser for custom-event WHERE clauses shaped like:
      ("event_name"='a' AND "platform"='ios') OR ("event_name"='b')
    Returns [{event: str, filters:[{field, op, value}]}].
    """
    if not sql:
        return []
    norm = " ".join(str(sql).replace("\n", " ").split())
    groups_raw = re.findall(r"\(([^()]+)\)", norm)
    if not groups_raw:
        groups_raw = re.split(r"\s+OR\s+", norm, flags=re.IGNORECASE)
    parsed: list[dict] = []
    for graw in groups_raw:
        parts = re.split(r"\s+AND\s+", graw.strip(), flags=re.IGNORECASE)
        event_name = ""
        filters = []
        for part in parts:
            cond = part.strip().strip("()")
            # Handle event IN (...) syntax first.
            in_m = re.match(
                r'^"?(?P<field>[a-zA-Z0-9_\.]+)"?\s+IN\s*\((?P<vals>.+)\)$',
                cond,
                flags=re.IGNORECASE,
            )
            if in_m:
                field = in_m.group("field")
                vals_raw = [v.strip() for v in (in_m.group("vals") or "").split(",") if v.strip()]
                vals = []
                for rv in vals_raw:
                    if (rv.startswith("'") and rv.endswith("'")) or (rv.startswith('"') and rv.endswith('"')):
                        vals.append(rv[1:-1].replace("''", "'"))
                    else:
                        vals.append(rv.strip())
                is_event_field = (event_col and field == event_col) or (field.lower() in EVENT_SIGNALS)
                if is_event_field and vals:
                    event_name = vals[0]
                    continue
                if field != (event_col or "") and len(vals) == 1:
                    filters.append({"field": field, "op": "=", "value": vals[0]})
                continue

            m = re.match(
                r'^"?(?P<field>[a-zA-Z0-9_\.]+)"?\s*(?P<op>=|!=|>=|<=|>|<|IS NULL|IS NOT NULL)\s*(?P<value>.*)$',
                cond,
                flags=re.IGNORECASE,
            )
            if not m:
                continue
            field = m.group("field")
            op = m.group("op").upper()
            raw_val = (m.group("value") or "").strip()
            if raw_val.startswith("'") and raw_val.endswith("'"):
                value = raw_val[1:-1].replace("''", "'")
            elif raw_val.startswith('"') and raw_val.endswith('"'):
                value = raw_val[1:-1]
            else:
                value = raw_val
            is_event_field = (event_col and field == event_col) or (field.lower() in EVENT_SIGNALS)
            if is_event_field and op == "=" and value:
                event_name = value
                continue
            if field != (event_col or ""):
                filters.append({
                    "field": field,
                    "op": op,
                    "value": "" if op in {"IS NULL", "IS NOT NULL"} else value,
                })
        if event_name:
            parsed.append({"event": event_name, "filters": filters})
    return parsed


def _custom_event_extra_clause(custom_event: dict, table_data: dict, primary_event: str) -> str:
    """
    Build a strict, structural clause from a custom event by keeping ONLY non-event
    filter atoms. This avoids duplicate event predicates in metric SQL.
    """
    if not custom_event:
        return ""

    groups = []
    bdef = custom_event.get("builder_definition", {}) if isinstance(custom_event, dict) else {}
    if isinstance(bdef, dict):
        groups = bdef.get("groups", []) or []
    if not groups:
        groups = _parse_custom_event_where(custom_event.get("sql", ""))
    if not groups:
        return ""

    # Prefer group(s) matching the currently selected primary event.
    target_groups = [g for g in groups if (g.get("event", "") == primary_event)] if primary_event else list(groups)
    if not target_groups:
        target_groups = list(groups)

    group_clauses = []
    for g in target_groups:
        atoms = []
        for f in g.get("filters", []) or []:
            field = f.get("field", "")
            op = f.get("op", "")
            value = f.get("value", "")
            clause = _build_filter_clause(table_data, field, op, value)
            if clause:
                atoms.append(clause)
        if atoms:
            group_clauses.append("(" + " AND ".join(atoms) + ")")

    if not group_clauses:
        return ""
    if len(group_clauses) == 1:
        return group_clauses[0]
    return "(" + " OR ".join(group_clauses) + ")"


def _sql_literal(value: str, table_data: dict, field_name: str) -> str:
    col = _column_index(table_data).get(field_name, {})
    ctype = (col.get("type", "") or "").upper()
    if value.lower() in {"true", "false"}:
        return value.lower()
    if any(marker in ctype for marker in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "NUMERIC", "REAL")):
        return value
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _build_filter_clause(table_data: dict, field_name: str, op: str, value: str) -> str:
    if not field_name or not op:
        return ""
    if op in {"IS NULL", "IS NOT NULL"}:
        return f'"{field_name}" {op}'
    if value == "":
        return ""
    return f'"{field_name}" {op} {_sql_literal(value, table_data, field_name)}'


def _combine_clauses(clauses: list[str]) -> str:
    cleaned = [clause for clause in clauses if clause]
    if not cleaned:
        return ""
    return " AND ".join(cleaned)


def _table_label(table_name: str, table_data: dict) -> str:
    return table_data.get("table_display_name", table_name)


def _metric_preview_sql(table_name: str, table_data: dict, metric_type: str,
                        primary_event: str, secondary_event: str,
                        value_field: str, filter_1: str, filter_2: str,
                        retention_days: int = 7,
                        return_filter: str = "",
                        denom_filter: str = "",
                        numerator_clause_override: str = "",
                        denominator_clause_override: str = "",
                        cohort_clause_override: str = "",
                        return_clause_override: str = "") -> str:
    signals = resolve_table_signals(table_data)
    event_col = signals["event_col"] or "event_name"
    user_col  = signals["user_col"]  or "user_id"
    time_col  = signals["time_col"]  or "timestamp"

    base_clauses = []
    if primary_event:
        base_clauses.append(f'"{event_col}" = \'{primary_event}\'')
    if filter_1:
        base_clauses.append(filter_1)
    if filter_2:
        base_clauses.append(filter_2)
    where_sql    = _combine_clauses(base_clauses)
    where_clause = f" WHERE {where_sql}" if where_sql else ""

    if metric_type == "Unique users on event":
        return (
            f"SELECT DATE({time_col}) AS date, COUNT(DISTINCT {user_col}) AS value "
            f'FROM "{table_name}"{where_clause} GROUP BY 1 ORDER BY 1'
        )
    if metric_type == "Event count":
        return (
            f"SELECT DATE({time_col}) AS date, COUNT(*) AS value "
            f'FROM "{table_name}"{where_clause} GROUP BY 1 ORDER BY 1'
        )
    if metric_type == "Sum property" and value_field:
        return (
            f"SELECT DATE({time_col}) AS date, SUM({value_field}) AS value "
            f'FROM "{table_name}"{where_clause} GROUP BY 1 ORDER BY 1'
        )
    if metric_type == "Average property" and value_field:
        return (
            f"SELECT DATE({time_col}) AS date, AVG({value_field}) AS value "
            f'FROM "{table_name}"{where_clause} GROUP BY 1 ORDER BY 1'
        )
    if metric_type == "Events per user" and primary_event:
        return (
            f"SELECT ROUND(\n"
            f"  COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT {user_col}), 0), 2\n"
            f') AS events_per_user\n'
            f'FROM "{table_name}"{where_clause}'
        )
    if metric_type == "% of users" and primary_event:
        # Numerator CASE: primary_event + its filters
        if numerator_clause_override:
            num_case = numerator_clause_override
        else:
            num_clauses = [f'"{event_col}" = \'{primary_event}\'']
            if filter_1: num_clauses.append(filter_1)
            if filter_2: num_clauses.append(filter_2)
            num_case = " AND ".join(num_clauses)
        # Global WHERE: union of all active event types so the scan is tight
        if secondary_event:
            # denominator = users who did secondary_event (cohort-based %)
            if denominator_clause_override:
                den_case = denominator_clause_override
            else:
                den_clauses = [f'"{event_col}" = \'{secondary_event}\'']
                if denom_filter: den_clauses.append(denom_filter)
                den_case = " AND ".join(den_clauses)
            # WHERE: only rows that match numerator OR denominator
            if numerator_clause_override or denominator_clause_override:
                extra_where = ""
            else:
                all_events = [f'"{event_col}" IN (\'{primary_event}\', \'{secondary_event}\')']
                combined_extra = _combine_clauses(all_events)
                extra_where = f" WHERE {combined_extra}" if combined_extra else ""
            return (
                f"-- % of [{secondary_event}] users who also did [{primary_event}]\n"
                f"SELECT ROUND(\n"
                f"  COUNT(DISTINCT CASE WHEN {num_case} THEN {user_col} END) * 100.0\n"
                f"  / NULLIF(\n"
                f"      COUNT(DISTINCT CASE WHEN {den_case} THEN {user_col} END), 0),\n"
                f"  1\n"
                f') AS pct\nFROM "{table_name}"{extra_where}'
            )
        else:
            extra_filter = _combine_clauses([filter_1, filter_2])
            extra_where  = f" WHERE {extra_filter}" if extra_filter else ""
            return (
                f"SELECT ROUND(\n"
                f"  COUNT(DISTINCT CASE WHEN {num_case} THEN {user_col} END) * 100.0\n"
                f"  / NULLIF(COUNT(DISTINCT {user_col}), 0), 1\n"
                f') AS pct\nFROM "{table_name}"{extra_where}'
            )
    if metric_type == "Conversion funnel" and primary_event and secondary_event:
        funnel_extra = _combine_clauses([filter_1, filter_2])
        funnel_where = f" WHERE {funnel_extra}" if funnel_extra else ""
        return (
            f"SELECT ROUND(\n"
            f"  COUNT(DISTINCT CASE WHEN \"{event_col}\" = '{secondary_event}'"
            f" THEN {user_col} END) * 100.0\n"
            f"  / NULLIF(\n"
            f"      COUNT(DISTINCT CASE WHEN \"{event_col}\" = '{primary_event}'"
            f" THEN {user_col} END), 0),\n"
            f"  1\n"
            f') AS conversion_pct\nFROM "{table_name}"{funnel_where}'
        )
    if metric_type == "Retention (N-day)" and primary_event:
        ret_event      = secondary_event or primary_event
        if cohort_clause_override:
            cohort_filter = cohort_clause_override
        else:
            cohort_filter = _combine_clauses([f'"{event_col}" = \'{primary_event}\'', filter_1, filter_2])
        cohort_where   = f" WHERE {cohort_filter}" if cohort_filter else ""
        # Return event WHERE: event filter + optional extra filter
        if return_clause_override:
            ret_where_body = return_clause_override
        else:
            ret_clauses = [f'"{event_col}" = \'{ret_event}\'']
            if return_filter:
                ret_clauses.append(return_filter)
            ret_where_body = _combine_clauses(ret_clauses)
        n              = retention_days
        label          = f"d{n}_retention_pct"
        return (
            f'WITH cohort AS (\n'
            f'  SELECT {user_col}, MIN(DATE({time_col})) AS first_date\n'
            f'  FROM "{table_name}"{cohort_where}\n'
            f'  GROUP BY 1\n'
            f'),\n'
            f'returned AS (\n'
            f'  SELECT DISTINCT c.{user_col}\n'
            f'  FROM cohort c\n'
            f'  JOIN "{table_name}" e ON e.{user_col} = c.{user_col}\n'
            f'  WHERE {ret_where_body}\n'
            f"  AND DATE(e.{time_col}) = c.first_date + INTERVAL '{n} days'\n"
            f')\n'
            f'SELECT\n'
            f'  COUNT(DISTINCT cohort.{user_col})   AS cohort_size,\n'
            f'  COUNT(DISTINCT returned.{user_col}) AS retained,\n'
            f'  ROUND(\n'
            f'    COUNT(DISTINCT returned.{user_col}) * 100.0\n'
            f'    / NULLIF(COUNT(DISTINCT cohort.{user_col}), 0), 1\n'
            f'  ) AS {label}\n'
            f'FROM cohort\n'
            f'LEFT JOIN returned ON returned.{user_col} = cohort.{user_col}'
        )
    if metric_type == "Active user churn":
        # N = look-back window (days). Prev period = 2N to N+1 days ago, curr = last N days.
        n = retention_days or 30
        evt_filter = f'"{event_col}" = \'{primary_event}\'' if primary_event else ""
        extra = _combine_clauses([evt_filter, filter_1, filter_2])
        and_extra = f"\n  AND {extra}" if extra else ""
        return (
            f'-- Churn Rate: users active in prev {n}-day period who are absent in last {n} days\n'
            f'WITH prev AS (\n'
            f'  SELECT DISTINCT {user_col}\n'
            f'  FROM "{table_name}"\n'
            f'  WHERE DATE({time_col})\n'
            f'    BETWEEN CURRENT_DATE - INTERVAL \'{n * 2} days\'\n'
            f'        AND CURRENT_DATE - INTERVAL \'{n + 1} days\'{and_extra}\n'
            f'),\n'
            f'curr AS (\n'
            f'  SELECT DISTINCT {user_col}\n'
            f'  FROM "{table_name}"\n'
            f"  WHERE DATE({time_col}) >= CURRENT_DATE - INTERVAL '{n} days'{and_extra}\n"
            f')\n'
            f'SELECT\n'
            f'  COUNT(DISTINCT prev.{user_col})  AS prev_period_users,\n'
            f'  COUNT(DISTINCT curr.{user_col})  AS curr_period_users,\n'
            f'  ROUND(\n'
            f'    (1 - COUNT(DISTINCT curr.{user_col}) * 1.0\n'
            f'       / NULLIF(COUNT(DISTINCT prev.{user_col}), 0)) * 100, 1\n'
            f'  ) AS churn_rate_pct\n'
            f'FROM prev\n'
            f'LEFT JOIN curr ON curr.{user_col} = prev.{user_col}'
        )
    return ""


def _definition_preview_sql(table_name: str, table_data: dict, definition_type: str,
                            primary_event: str, secondary_event: str,
                            filter_1: str, filter_2: str) -> str:
    signals = resolve_table_signals(table_data)
    event_col = signals["event_col"] or "event_name"
    user_col = signals["user_col"] or "user_id"

    event_clause = f'"{event_col}" = \'{primary_event}\'' if primary_event else ""
    combined = _combine_clauses([event_clause, filter_1, filter_2])

    if definition_type == "User segment" and combined:
        return f"COUNT(DISTINCT {user_col}) FILTER (WHERE {combined})"
    if definition_type == "Event count" and combined:
        return f"COUNT(*) FILTER (WHERE {combined})"
    if definition_type == "Conversion rate" and primary_event and secondary_event:
        return (
            f"ROUND("
            f"COUNT(DISTINCT CASE WHEN \"{event_col}\" = '{secondary_event}' THEN {user_col} END) * 100.0 / "
            f"NULLIF(COUNT(DISTINCT CASE WHEN \"{event_col}\" = '{primary_event}' THEN {user_col} END), 0), 1"
            f")"
        )
    return ""


def is_event_edited(table_name: str, raw_name: str, current: dict) -> bool:
    """Compare current event to the version on disk to detect human edits."""
    orig = st.session_state.get("original_catalog", {})
    orig_events = {e["raw_name"]: e for e in orig.get(table_name, {}).get("events", [])}
    if raw_name not in orig_events:
        return False
    orig_ev = orig_events[raw_name]
    for field in ("display_name", "description", "analysis_tags"):
        if current.get(field) != orig_ev.get(field):
            return True
    # Check property descriptions
    orig_props = {p["raw_name"]: p for p in orig_ev.get("properties", [])}
    for prop in current.get("properties", []):
        op = orig_props.get(prop["raw_name"], {})
        if prop.get("description") != op.get("description"):
            return True
    return False


# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Catalog Editor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    :root {
        --bg: #eef4fb;
        --bg-elevated: #ffffff;
        --bg-panel: #ffffff;
        --line: rgba(15, 23, 42, 0.12);
        --line-strong: rgba(59, 130, 246, 0.22);
        --text: #0f172a;
        --muted: #475569;
        --chip: rgba(15, 23, 42, 0.05);
        --accent: #8b5cf6;
        --accent-2: #06b6d4;
        --warn: #f59e0b;
    }
    .stApp {
        background:
            radial-gradient(circle at top right, rgba(59, 130, 246, 0.08), transparent 28%),
            radial-gradient(circle at top left, rgba(139, 92, 246, 0.06), transparent 24%),
            linear-gradient(180deg, #f9fbff 0%, var(--bg) 32%, #e7eef7 100%);
    }
    [data-testid="stSidebar"] {
        background:
            linear-gradient(180deg, rgba(255,255,255,0.94), rgba(248,250,252,0.98)),
            #f8fbff;
        border-right: 1px solid var(--line);
    }
    [data-testid="stSidebar"] * {
        color: var(--text);
    }
    .block-container { padding-top: 1.25rem; max-width: 1320px; }
    h1, h2, h3, h4, p, label, span, div {
        color: inherit;
    }
    .stMarkdown, .stCaption, .stText, .stMetricLabel {
        color: var(--text);
    }
    .stExpander summary, .stExpander summary * {
        color: #0f172a !important;
    }
    .stExpander details div[role="button"] p,
    .stExpander details div[role="button"] span,
    .stExpander details div[data-testid="stExpanderDetails"] p,
    .stExpander details div[data-testid="stExpanderDetails"] label,
    .stExpander details div[data-testid="stExpanderDetails"] div,
    .stExpander details div[data-testid="stExpanderDetails"] span {
        color: #0f172a !important;
    }
    .stTextInput input::placeholder, .stTextArea textarea::placeholder {
        color: #64748b !important;
        opacity: 1 !important;
    }
    .stTextArea textarea, .stTextInput input {
        caret-color: #0f172a !important;
    }
    .event-title { font-size: 15px; font-weight: 700; color: var(--text); }
    .raw-name {
        font-family: monospace; font-size: 12px;
        background: rgba(15, 23, 42, 0.06); color: #0f172a;
        padding: 2px 8px; border-radius: 6px;
    }
    .section-label {
        font-size: 11px; font-weight: 700; color: var(--muted);
        text-transform: uppercase; letter-spacing: 0.08em;
        margin: 16px 0 6px;
    }
    .table-chip {
        font-size: 11px; background: rgba(15, 23, 42, 0.06); color: #0f172a;
        padding: 2px 8px; border-radius: 6px; font-family: monospace;
    }
    .stat-card {
        background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(247,250,252,1));
        border: 1px solid var(--line);
        border-radius: 14px; padding: 16px; text-align: center;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.05);
    }
    .stat-num { font-size: 28px; font-weight: 800; color: var(--text); }
    .stat-label { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .hero-shell {
        border: 1px solid var(--line-strong);
        background:
            linear-gradient(135deg, rgba(139,92,246,0.10), rgba(96,165,250,0.08)),
            rgba(255,255,255,0.98);
        border-radius: 22px;
        padding: 22px 24px;
        margin-bottom: 18px;
        box-shadow: 0 18px 42px rgba(15, 23, 42, 0.08);
    }
    .hero-top {
        display:flex;
        justify-content:space-between;
        gap:16px;
        align-items:flex-start;
        flex-wrap:wrap;
    }
    .hero-kicker {
        color:#6d28d9;
        font-size:12px;
        letter-spacing:0.08em;
        text-transform:uppercase;
        font-weight:800;
        margin-bottom:10px;
    }
    .hero-title {
        font-size:30px;
        line-height:1.1;
        font-weight:900;
        color:var(--text);
        margin-bottom:8px;
    }
    .hero-sub {
        color:var(--muted);
        font-size:14px;
        max-width:760px;
    }
    .hero-pills {
        margin-top:16px;
        display:flex;
        gap:10px;
        flex-wrap:wrap;
    }
    .hero-pill {
        background: rgba(15, 23, 42, 0.05);
        border: 1px solid var(--line);
        color: #0f172a;
        padding: 9px 12px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
    }
    .panel {
        background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(247,250,252,1));
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 18px 18px 10px;
        margin-bottom: 18px;
        box-shadow: 0 12px 30px rgba(15, 23, 42, 0.06);
    }
    .panel-title {
        font-size: 13px;
        font-weight: 800;
        color: #0f172a;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 8px;
    }
    .metric-section {
        background: #f8fbff;
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 14px 14px 2px;
        margin-bottom: 14px;
    }
    .metric-header {
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:12px;
        margin-bottom:10px;
    }
    .metric-title {
        color: var(--text);
        font-size: 18px;
        font-weight: 800;
    }
    .metric-count {
        color: var(--muted);
        font-size: 12px;
        font-weight: 700;
    }
    .stTextInput input, .stTextArea textarea, .stSelectbox [data-baseweb="select"], .stMultiSelect [data-baseweb="select"] {
        background-color: #ffffff !important;
        color: #0f172a !important;
        opacity: 1 !important;
        -webkit-text-fill-color: #0f172a !important;
    }
    div[data-baseweb="select"] > div {
        background-color: #ffffff !important;
        border-color: var(--line) !important;
    }
    div[data-baseweb="select"] span,
    div[data-baseweb="select"] input,
    div[data-baseweb="select"] * {
        color: #0f172a !important;
        opacity: 1 !important;
        -webkit-text-fill-color: #0f172a !important;
    }
    .stCode pre {
        background: #0f172a !important;
        border: 1px solid #334155 !important;
    }
    .stCode code {
        color: #e2e8f0 !important;
    }
    .stTextInput input, .stTextArea textarea {
        border: 1px solid var(--line) !important;
        border-radius: 12px !important;
    }
    .stTextInput label, .stTextArea label, .stSelectbox label, .stMultiSelect label, .stCheckbox label {
        color: #0f172a !important;
        font-weight: 600 !important;
    }
    .stButton > button, .stDownloadButton > button {
        border-radius: 12px !important;
        border: 1px solid var(--line) !important;
        background: #ffffff !important;
        color: #0f172a !important;
    }
    .stExpander {
        border: 1px solid var(--line) !important;
        border-radius: 14px !important;
        background: #ffffff !important;
    }
    .stCaption, [data-testid="stCaptionContainer"], small {
        color: #475569 !important;
    }
    [data-testid="stMetricLabel"], [data-testid="stMetricValue"] {
        color: #0f172a !important;
    }
    .stDataFrame, [data-testid="stMetric"] {
        background: transparent;
    }
</style>
""", unsafe_allow_html=True)


# ── Load catalog ───────────────────────────────────────────────────────────────

catalog_path = find_catalog_path()
if not catalog_path:
    st.error("catalog.json not found. Run the pipeline first:\n```\ncd semantic-layer && python run.py ../jupiter.duckdb\n```")
    st.stop()

catalog = load_catalog(str(catalog_path))

# Session state
if "catalog" not in st.session_state:
    st.session_state.catalog = json.loads(json.dumps(catalog))

# Keep a frozen copy of what's on disk for edit detection
if "original_catalog" not in st.session_state:
    st.session_state.original_catalog = json.loads(json.dumps(catalog))

_biz = catalog.get("__business_context__", {})

if "biz_industry" not in st.session_state:
    st.session_state.biz_industry = _biz.get("industry", "")
if "biz_company" not in st.session_state:
    st.session_state.biz_company = _biz.get("company", "")

if "custom_events" not in st.session_state:
    st.session_state.custom_events = _biz.get("custom_events", [])

if "exclusions" not in st.session_state:
    _default_excl = {
        "always_filter": [],
        "glossary": [],
        "conventions": [
            "Default time window: last 7 days unless user specifies otherwise",
            "Always use DATE(event_time) when grouping by day",
            "DuckDB syntax: use INTERVAL '7 days' not DATEADD",
        ],
    }
    st.session_state.exclusions = _biz.get("exclusions", _default_excl)


# ── Sidebar ────────────────────────────────────────────────────────────────────

valid_tables = {k: v for k, v in st.session_state.catalog.items()
                if not k.startswith("__") and "error" not in v}

total_events = sum(len(t.get("events", [])) for t in valid_tables.values())
total_metrics = sum(len(t.get("suggested_metrics", [])) for t in valid_tables.values())
edited_count = sum(
    1 for tname, tdata in valid_tables.items()
    for ev in tdata.get("events", [])
    if is_event_edited(tname, ev["raw_name"], ev)
)

with st.sidebar:
    st.markdown("## 📊 Catalog Editor")
    st.caption(f"`{catalog_path.relative_to(ROOT)}`")
    st.divider()

    table_items = []
    for tname, tdata in valid_tables.items():
        ttype   = tdata.get("table_type", "dimension")
        icon    = "⚡" if ttype == "event_log" else "👤"
        display = tdata.get("table_display_name", tname)
        table_items.append(f"{icon} {display}")

    nav_options = table_items + ["─────────────", "📊 Data Stats", "🏢 Business Context"]

    page = st.radio("Navigate", nav_options, label_visibility="collapsed")

    st.divider()

    c1, c2 = st.columns(2)
    c1.metric("Events", total_events)
    c2.metric("Edited", edited_count)

    st.divider()
    if st.button("💾 Save & Regenerate MD", use_container_width=True, type="primary"):
        save_and_regenerate(st.session_state.catalog, catalog_path)
        st.session_state.original_catalog = json.loads(json.dumps(st.session_state.catalog))
        st.success("Saved!")


# ── Resolve selected table ─────────────────────────────────────────────────────

selected_table_name = None
selected_table_data = None

for tname, tdata in valid_tables.items():
    display = tdata.get("table_display_name", tname)
    ttype   = tdata.get("table_type", "dimension")
    icon    = "⚡" if ttype == "event_log" else "👤"
    if page == f"{icon} {display}":
        selected_table_name = tname
        selected_table_data = tdata
        break

shell_title = "Analytics Catalog Console"
shell_subtitle = "Review events, curate business definitions, and shape a semantic layer that feels closer to an actual product analytics platform."
if selected_table_name and selected_table_data:
    shell_title = selected_table_data.get("table_display_name", selected_table_name)
    shell_subtitle = selected_table_data.get("table_description", "")
elif page == "📊 Data Stats":
    shell_title = "Analytics Workspace"
    shell_subtitle = "Usage overview, event distributions, and documentation coverage in one place."
elif page == "🏢 Business Context":
    shell_title = "Business Context Studio"
    shell_subtitle = "Organize lifecycle definitions, KPI buckets, exclusions, and conventions the way analytics teams expect."

st.markdown(
    f"""
    <div class="hero-shell">
        <div class="hero-top">
            <div>
                <div class="hero-kicker">Product Analytics Platform</div>
                <div class="hero-title">{shell_title}</div>
                <div class="hero-sub">{shell_subtitle}</div>
                <div class="hero-pills">
                    <span class="hero-pill">{total_events} events cataloged</span>
                    <span class="hero-pill">{total_metrics} metrics tracked</span>
                    <span class="hero-pill">{edited_count} human edits</span>
                    <span class="hero-pill">{len(valid_tables)} tables in workspace</span>
                </div>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# TABLE VIEW
# ─────────────────────────────────────────────────────────────────────────────

if selected_table_name and selected_table_data:
    ttype        = selected_table_data.get("table_type", "dimension")
    display_name = selected_table_data.get("table_display_name", selected_table_name)

    type_color = "#4f8ef7" if ttype == "event_log" else "#22c55e"
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">'
        f'<span style="font-size:22px;font-weight:800;color:#0f172a">{display_name}</span>'
        f'<span style="background:{type_color};color:white;padding:2px 10px;border-radius:8px;font-size:12px;font-weight:600">{ttype}</span>'
        f'<span class="table-chip">{selected_table_name}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.caption(selected_table_data.get("table_description", ""))
    st.divider()

    # ── EVENT LOG ──────────────────────────────────────────────────────────────
    if ttype == "event_log":
        events = selected_table_data.get("events", [])
        flow_candidates = selected_table_data.get("flow_candidates", [])

        if flow_candidates:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown("#### Flow Candidates")
            st.caption("Deterministic process groups inferred from event semantics. Use these as a sanity check for activation / completion metrics.")
            for flow in flow_candidates[:8]:
                flow_title = f"{flow.get('journey', 'general').replace('_', ' ').title()} / {flow.get('object', 'general').replace('_', ' ').title()}"
                start_events = ", ".join(flow.get("entry_events", [])) or "none"
                success_events = ", ".join(flow.get("success_events", [])) or "none"
                failure_events = ", ".join(flow.get("failure_events", [])) or "none"
                st.markdown(
                    f'<div style="background:#f8fbff;border:1px solid rgba(15,23,42,0.12);'
                    f'border-radius:12px;padding:12px 14px;margin-bottom:10px">'
                    f'<div style="font-size:14px;font-weight:800;color:#0f172a">{flow_title}</div>'
                    f'<div style="font-size:12px;color:#475569;margin-top:4px">'
                    f'start: <code>{start_events}</code><br>'
                    f'success: <code>{success_events}</code><br>'
                    f'failure: <code>{failure_events}</code></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            st.markdown('</div>', unsafe_allow_html=True)

        all_group_keys = sorted({k for ev in events for k in ev.get("groups", {}).keys()})

        filter_cols = st.columns([3] + [2] * len(all_group_keys) + [2])
        with filter_cols[0]:
            search = st.text_input("🔍 Search", placeholder="vkyc, transaction...", label_visibility="collapsed")
        group_filters = {}
        for i, gkey in enumerate(all_group_keys):
            values = sorted({ev.get("groups", {}).get(gkey) for ev in events if ev.get("groups", {}).get(gkey)})
            with filter_cols[i + 1]:
                group_filters[gkey] = st.selectbox(
                    gkey.replace("_", " ").title(), ["All"] + values, key=f"gf_{gkey}"
                )
        with filter_cols[-1]:
            tag_filter = st.multiselect("Tags", ALL_TAGS, placeholder="All tags", label_visibility="collapsed")

        show_edited_only = st.checkbox("Show edited only")

        def matches(ev):
            if show_edited_only and not is_event_edited(selected_table_name, ev["raw_name"], ev):
                return False
            if search and search.lower() not in ev["raw_name"].lower() \
                    and search.lower() not in ev.get("display_name", "").lower():
                return False
            for gkey, gval in group_filters.items():
                if gval != "All" and ev.get("groups", {}).get(gkey) != gval:
                    return False
            if tag_filter and not any(t in ev.get("analysis_tags", []) for t in tag_filter):
                return False
            return True

        filtered = [ev for ev in events if matches(ev)]

        def render_event(ev):
            raw    = ev["raw_name"]
            tags   = ev.get("analysis_tags", [])
            props  = ev.get("properties", [])
            grps   = ev.get("groups", {})
            edited = is_event_edited(selected_table_name, raw, ev)

            tags_html = "".join(tag_badge(t) for t in tags) if tags else \
                '<span style="color:#475569;font-size:11px">no tags</span>'
            grp_html = "".join(
                f'<span style="background:rgba(15,23,42,0.06);color:#0f172a;padding:1px 7px;'
                f'border-radius:6px;font-size:11px;margin-right:4px">{v}</span>'
                for v in grps.values()
            )
            sbadge = source_badge(edited)
            border = "#fbbf24" if edited else "#e2e8f0"

            st.markdown(
                f'<div style="border-left:3px solid {border};padding-left:10px;'
                f'display:flex;align-items:center;gap:8px;margin-bottom:2px;flex-wrap:wrap">'
                f'<span class="event-title">{ev.get("display_name", raw)}</span>'
                f'<span class="raw-name">{raw}</span>'
                f'{grp_html}{tags_html}{sbadge}</div>',
                unsafe_allow_html=True,
            )

            label = f"Edit · {len(props)} properties" if props else "Edit"
            with st.expander(label, expanded=False):
                c1, c2 = st.columns([3, 1])
                with c1:
                    ev["display_name"] = st.text_input(
                        "Display name", value=ev.get("display_name", raw),
                        key=f"dn_{selected_table_name}_{raw}"
                    )
                    ev["description"] = st.text_area(
                        "Description", value=ev.get("description", ""),
                        height=80, key=f"desc_{selected_table_name}_{raw}"
                    )
                with c2:
                    ev["analysis_tags"] = st.multiselect(
                        "Tags", ALL_TAGS,
                        default=[t for t in tags if t in ALL_TAGS],
                        key=f"tags_{selected_table_name}_{raw}"
                    )
                    if grps:
                        st.markdown('<div class="section-label" style="margin-top:8px">Groups</div>',
                                    unsafe_allow_html=True)
                        for gkey, gval in grps.items():
                            ev["groups"][gkey] = st.text_input(
                                gkey, value=gval, key=f"grp_{selected_table_name}_{raw}_{gkey}"
                            )

                if props:
                    st.markdown('<div class="section-label">Event Properties</div>', unsafe_allow_html=True)
                    for pi, prop in enumerate(props):
                        pc1, pc2 = st.columns([2, 3])
                        with pc1:
                            st.markdown(
                                f'<div style="font-size:13px;padding:6px 0">'
                                f'<code>{prop["raw_name"]}</code><br>'
                                f'<span style="color:#334155;font-size:12px">'
                                f'{prop.get("display_name","")}</span></div>',
                                unsafe_allow_html=True,
                            )
                        with pc2:
                            prop["description"] = st.text_input(
                                "desc", value=prop.get("description", ""),
                                label_visibility="collapsed",
                                key=f"pdesc_{selected_table_name}_{raw}_{pi}"
                            )
            st.markdown("---")

        st.caption(f"Showing **{len(filtered)}** of {len(events)} events")
        st.divider()

        if all_group_keys and group_filters.get(all_group_keys[0], "All") == "All":
            primary_key = all_group_keys[0]
            group_values = sorted({ev.get("groups", {}).get(primary_key, "Other") for ev in filtered})
            for gval in group_values:
                group_events = [ev for ev in filtered
                                if ev.get("groups", {}).get(primary_key, "Other") == gval]
                edited_in_group = sum(1 for ev in group_events
                                      if is_event_edited(selected_table_name, ev["raw_name"], ev))
                edit_note = f" · {edited_in_group} edited" if edited_in_group else ""
                st.markdown(
                    f'<div class="section-label">{gval.replace("_"," ").title()} &nbsp;'
                    f'<span style="font-weight:400;color:#475569">'
                    f'{len(group_events)} events{edit_note}</span></div>',
                    unsafe_allow_html=True,
                )
                for ev in group_events:
                    render_event(ev)
        else:
            for ev in filtered:
                render_event(ev)

    # ── DIMENSION TABLE ────────────────────────────────────────────────────────
    else:
        columns  = selected_table_data.get("columns", [])
        pri_order = {"high": 0, "medium": 1, "low": 2, "skip": 3}
        sorted_cols = sorted(columns, key=lambda c: pri_order.get(c.get("analysis_priority", "medium"), 1))

        high_cols = [c for c in columns if c.get("analysis_priority") == "high"]
        pii_cols  = [c for c in columns if c.get("is_pii")]
        s1, s2, s3 = st.columns(3)
        s1.metric("Total columns", len(columns))
        s2.metric("★ Key dimensions", len(high_cols))
        s3.metric("PII columns", len(pii_cols))
        st.divider()

        for col in sorted_cols:
            raw      = col["raw_name"]
            pri      = col.get("analysis_priority", "medium")
            p_html   = priority_badge(pri)
            pii_html = '<span style="background:#ef4444;color:white;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600;margin-left:4px">PII</span>' \
                       if col.get("is_pii") else ""

            st.markdown(
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:2px">'
                f'<strong style="font-size:14px">{col.get("display_name", raw)}</strong>'
                f'<code style="font-size:12px;color:#334155">{raw}</code>'
                f'{p_html}{pii_html}</div>',
                unsafe_allow_html=True,
            )

            with st.expander("Edit", expanded=False):
                c1, c2, c3 = st.columns([4, 1, 1])
                with c1:
                    col["description"] = st.text_area(
                        "Description", value=col.get("description", ""),
                        height=70, key=f"cdesc_{selected_table_name}_{raw}"
                    )
                with c2:
                    col["analysis_priority"] = st.selectbox(
                        "Priority", ["high", "medium", "low", "skip"],
                        index=["high", "medium", "low", "skip"].index(pri),
                        key=f"cpri_{selected_table_name}_{raw}"
                    )
                with c3:
                    col["is_pii"] = st.checkbox("PII", value=col.get("is_pii", False),
                                                key=f"cpii_{selected_table_name}_{raw}")
                vm = col.get("value_meanings", {})
                if vm:
                    st.markdown('<div class="section-label">Value Meanings</div>', unsafe_allow_html=True)
                    for k, v in vm.items():
                        st.markdown(f"- `{k}` → {v}")

            st.markdown("---")


# ─────────────────────────────────────────────────────────────────────────────
# DATA STATS
# ─────────────────────────────────────────────────────────────────────────────

elif page == "📊 Data Stats":
    import duckdb

    db_path = find_db_path(catalog_path)
    if not db_path:
        st.error("No .duckdb file found near catalog.json")
        st.stop()

    @st.cache_data(show_spinner="Querying database...")
    def query(sql: str) -> pd.DataFrame:
        conn = duckdb.connect(str(db_path), read_only=True)
        df = conn.execute(sql).df()
        conn.close()
        return df

    st.markdown("## Data Stats")
    st.caption(f"Source: `{db_path.name}`  ·  Catalog generated by LLM, human edits tracked below")

    # ── Table selector ─────────────────────────────────────────────────────────
    event_tables = [k for k, v in valid_tables.items() if v.get("table_type") == "event_log"]
    if not event_tables:
        st.info("No event tables found.")
        st.stop()

    sel_table = st.selectbox("Table", event_tables,
                             format_func=lambda k: valid_tables[k].get("table_display_name", k))
    tinfo = valid_tables[sel_table]
    signals = resolve_table_signals(tinfo)
    event_name_col = signals["event_col"]
    time_col = signals["time_col"]
    user_col = signals["user_col"]

    if not all((event_name_col, time_col, user_col)):
        st.warning(
            "Could not confidently detect the event, time, and user columns for this table. "
            "Stats are disabled for this dataset."
        )
        st.stop()

    st.divider()

    # ── Overview cards ─────────────────────────────────────────────────────────
    try:
        overview = query(f"""
            SELECT
                COUNT(*)                        AS total_events,
                COUNT(DISTINCT "{user_col}")    AS unique_users,
                COUNT(DISTINCT "{event_name_col}") AS unique_event_types,
                MIN(DATE("{time_col}"))         AS first_date,
                MAX(DATE("{time_col}"))         AS last_date
            FROM "{sel_table}"
        """)
        row = overview.iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="stat-card"><div class="stat-num">{int(row.total_events):,}</div><div class="stat-label">Total Events</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="stat-card"><div class="stat-num">{int(row.unique_users):,}</div><div class="stat-label">Unique Users</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="stat-card"><div class="stat-num">{int(row.unique_event_types)}</div><div class="stat-label">Event Types</div></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="stat-card"><div class="stat-num" style="font-size:16px">{row.first_date}<br>→ {row.last_date}</div><div class="stat-label">Date Range</div></div>', unsafe_allow_html=True)
    except Exception as e:
        st.warning(f"Could not load overview: {e}")

    st.divider()

    col_left, col_right = st.columns(2)

    # ── Event distribution ─────────────────────────────────────────────────────
    with col_left:
        st.markdown("#### Event Distribution")
        try:
            df_dist = query(f"""
                SELECT "{event_name_col}" AS event_name, COUNT(*) AS count
                FROM "{sel_table}"
                GROUP BY 1 ORDER BY 2 DESC
            """)
            # Enrich with display names from catalog
            name_map = {e["raw_name"]: e.get("display_name", e["raw_name"])
                        for e in tinfo.get("events", [])}
            df_dist["display"] = df_dist["event_name"].map(lambda x: name_map.get(x, x))

            chart = alt.Chart(df_dist).mark_bar(color="#4f8ef7").encode(
                x=alt.X("count:Q", title="Event count"),
                y=alt.Y("display:N", sort="-x", title=None),
                tooltip=["display:N", "count:Q"]
            ).properties(height=max(250, len(df_dist) * 22))
            st.altair_chart(chart, use_container_width=True)
        except Exception as e:
            st.warning(str(e))

    # ── Events over time ───────────────────────────────────────────────────────
    with col_right:
        st.markdown("#### Events Over Time")
        try:
            df_time = query(f"""
                SELECT DATE("{time_col}") AS date, COUNT(*) AS events
                FROM "{sel_table}"
                GROUP BY 1 ORDER BY 1
            """)
            chart_t = alt.Chart(df_time).mark_area(
                color="#4f8ef7", opacity=0.3, line={"color": "#4f8ef7"}
            ).encode(
                x=alt.X("date:T", title="Date"),
                y=alt.Y("events:Q", title="Events"),
                tooltip=["date:T", "events:Q"]
            ).properties(height=300)
            st.altair_chart(chart_t, use_container_width=True)
        except Exception as e:
            st.warning(str(e))

    st.divider()

    # ── Category breakdown ─────────────────────────────────────────────────────
    # Auto-detect group columns from catalog
    group_keys = sorted({k for ev in tinfo.get("events", []) for k in ev.get("groups", {}).keys()})
    if group_keys:
        st.markdown("#### Category Breakdown")
        gcols = st.columns(len(group_keys))
        for i, gkey in enumerate(group_keys):
            with gcols[i]:
                try:
                    df_g = query(f"""
                        SELECT "{gkey}", COUNT(*) AS count
                        FROM "{sel_table}"
                        WHERE "{gkey}" IS NOT NULL
                        GROUP BY 1 ORDER BY 2 DESC
                    """)
                    chart_g = alt.Chart(df_g).mark_arc(innerRadius=40).encode(
                        theta=alt.Theta("count:Q"),
                        color=alt.Color(f"{gkey}:N", legend=alt.Legend(title=gkey.replace("_"," ").title())),
                        tooltip=[f"{gkey}:N", "count:Q"]
                    ).properties(height=200, title=gkey.replace("_", " ").title())
                    st.altair_chart(chart_g, use_container_width=True)
                except Exception as e:
                    st.warning(str(e))

        st.divider()

    # ── What the Catalog Agent produced ───────────────────────────────────────
    st.markdown("#### What the Catalog Agent Documented")
    st.caption("Auto-generated descriptions and properties — with human edits highlighted.")

    events = tinfo.get("events", [])
    edited_events   = [e for e in events if is_event_edited(sel_table, e["raw_name"], e)]
    auto_events     = [e for e in events if not is_event_edited(sel_table, e["raw_name"], e)]

    m1, m2 = st.columns(2)
    m1.metric("🤖 Auto-generated", len(auto_events))
    m2.metric("✏️ Human edited",   len(edited_events))

    st.markdown("")
    for ev in events:
        edited = is_event_edited(sel_table, ev["raw_name"], ev)
        bg     = "#fffbeb" if edited else "#f8fafc"
        border = "#fbbf24" if edited else "#e2e8f0"
        badge  = source_badge(edited)
        props  = ev.get("properties", [])
        props_html = "".join(
            f'<span style="font-family:monospace;font-size:11px;background:#f1f5f9;'
            f'color:#10131a;padding:1px 5px;border-radius:4px;margin-right:3px">{p["raw_name"]}</span>'
            for p in props
        ) if props else '<span style="color:#475569;font-size:11px">no properties</span>'

        st.markdown(
            f'<div style="background:{bg};border:1px solid {border};border-radius:8px;'
            f'padding:10px 14px;margin-bottom:8px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
            f'<strong>{ev.get("display_name", ev["raw_name"])}</strong>'
            f'<span class="raw-name">{ev["raw_name"]}</span>{badge}</div>'
            f'<div style="font-size:13px;color:#0f172a;margin-bottom:6px">{ev.get("description","")}</div>'
            f'<div>{props_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Raw sample rows ────────────────────────────────────────────────────────
    st.markdown("#### Raw Sample Rows")
    evt_filter = st.selectbox("Filter by event", ["(all)"] + sorted(df_dist["event_name"].tolist() if "df_dist" in dir() else []))
    try:
        where = f'WHERE "{event_name_col}" = \'{evt_filter}\'' if evt_filter != "(all)" else ""
        df_raw = query(f"SELECT * FROM \"{sel_table}\" {where} LIMIT 20")
        st.dataframe(df_raw, use_container_width=True, height=300)
    except Exception as e:
        st.warning(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS CONTEXT  (merged: custom events + metrics + glossary + rules)
# ─────────────────────────────────────────────────────────────────────────────

elif page == "🏢 Business Context":
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown("## Business Context")
    st.caption("Everything the analytics agent needs to understand your business — auto-detected by LLM, human-editable here.")

    # ── Industry / Company ────────────────────────────────────────────────────
    c1, c2 = st.columns(2)
    with c1:
        st.session_state.biz_industry = st.text_input(
            "Industry", value=st.session_state.biz_industry,
            help="Inferred from your data. Edit if wrong."
        )
    with c2:
        st.session_state.biz_company = st.text_input(
            "Company name", value=st.session_state.biz_company,
            help="Used as a header in catalog.md."
        )

    excl = st.session_state.exclusions

    st.divider()
    st.markdown('</div>', unsafe_allow_html=True)

    # ── Custom Events ─────────────────────────────────────────────────────────
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown("### ⚡ Custom Events")
    st.caption("Named user lifecycle segments — each maps to a SQL `WHERE` clause. The analytics agent uses these names without expanding the SQL.")

    ces = st.session_state.custom_events
    if not ces:
        st.info("No custom events yet. Re-run the pipeline to auto-generate, or add one below.")

    def _start_custom_event_edit(ci: int, ce: dict, ev_groups: list[dict]):
        st.session_state.ce_edit_idx = ci
        st.session_state["ce_name"] = ce.get("name", "")
        st.session_state["ce_desc"] = ce.get("description", "")
        bdef = ce.get("builder_definition", {}) if isinstance(ce, dict) else {}
        if bdef.get("table"):
            st.session_state["ce_table_sel"] = bdef.get("table")
        if ev_groups:
            hydrated = []
            for g in ev_groups:
                gf = g.get("filters", []) if isinstance(g, dict) else []
                hydrated.append({
                    "event": g.get("event", ""),
                    "filter_count": max(1, len(gf)),
                    "filters": gf,
                })
            st.session_state.ce_groups = hydrated or [{"event": "", "filter_count": 1, "filters": []}]
        else:
            parsed = _parse_custom_event_where(ce.get("sql", ""))
            if parsed:
                st.session_state.ce_groups = [
                    {
                        "event": g.get("event", ""),
                        "filter_count": max(1, len(g.get("filters", []))),
                        "filters": g.get("filters", []),
                    }
                    for g in parsed
                ]
            else:
                fallback = _parse_sql_for_builder(ce.get("sql", ""))
                fallback_event = fallback.get("primary_event", "") if isinstance(fallback, dict) else ""
                st.session_state.ce_groups = [{
                    "event": fallback_event,
                    "filter_count": 1,
                    "filters": [],
                }]
        for gi, g in enumerate(st.session_state.ce_groups):
            st.session_state[f"ceg{gi}_event"] = g.get("event", "")
            for fi, frow in enumerate(g.get("filters", [])):
                base = f"ceg{gi}_f{fi}"
                st.session_state[f"{base}_field"] = frow.get("field", "")
                st.session_state[f"{base}_op"] = frow.get("op", "=")
                if frow.get("value", "") != "":
                    st.session_state[f"{base}_val"] = "Custom..."
                    st.session_state[f"{base}_custom"] = frow.get("value", "")

    _ce_active_idx = st.session_state.get("ce_edit_idx")
    for ci, ce in enumerate(ces):
        is_auto  = ce.get("_source") != "human"
        badge    = source_badge(not is_auto)
        bdef_ce  = ce.get("builder_definition", {})
        has_bld  = bool(bdef_ce.get("groups"))
        ev_groups = bdef_ce.get("groups", [])
        ev_summary = " OR ".join(
            f'⚡ {g.get("event","?")}' for g in ev_groups if g.get("event")
        ) or ce.get("sql", "")[:60]

        st.markdown(
            f'<div style="background:white;border:1px solid rgba(15,23,42,0.1);'
            f'border-left:3px solid #4f8ef7;border-radius:8px;padding:10px 14px;margin-bottom:6px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
            f'<strong style="font-family:monospace;font-size:14px">{ce.get("name","")}</strong>{badge}</div>'
            + (f'<div style="font-size:12px;color:#475569;margin-bottom:3px">{ce.get("description","")}</div>'
               if ce.get("description") else "")
            + f'<div style="font-size:11px;color:#6366f1;font-family:monospace">{ev_summary}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        is_editing = st.session_state.get("ce_edit_idx") == ci
        exp_cols = st.columns([1, 1, 1, 4])
        with exp_cols[0]:
            if st.button("✏️ Editing…" if is_editing else "✏️ Edit",
                         key=f"ce_edit_{ci}",
                         type="primary" if is_editing else "secondary",
                         use_container_width=True,
                         help="Edit in guided builder"):
                if is_editing:
                    st.session_state.ce_edit_idx = None
                else:
                    _start_custom_event_edit(ci, ce, ev_groups)
                st.rerun()
        with exp_cols[1]:
            if st.button("🗑 Remove", key=f"cedel_{ci}", use_container_width=True):
                ces.pop(ci)
                if st.session_state.get("ce_edit_idx") == ci:
                    st.session_state.ce_edit_idx = None
                st.rerun()

        st.markdown("---")
        if _ce_active_idx == ci:
            break

    # ── Edit-mode state ───────────────────────────────────────────────────────
    if "ce_edit_idx" not in st.session_state:
        st.session_state.ce_edit_idx = None
    _ce_edit_idx    = st.session_state.ce_edit_idx
    _ce_edit_active = _ce_edit_idx is not None and _ce_edit_idx < len(ces)
    _ce_being_edited = ces[_ce_edit_idx] if _ce_edit_active else {}

    if _ce_edit_active:
        st.markdown(
            f"<div style='background:#eef2ff;border-left:4px solid #6366f1;"
            f"border-radius:0 8px 8px 0;padding:10px 16px;margin-bottom:12px'>"
            f"<strong style='color:#4338ca'>Editing segment:</strong> "
            f"<code>{_ce_being_edited.get('name','')}</code></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown("#### Guided Custom Event Builder")
        st.caption("Each row is one event + AND filters. Rows are combined with OR — exactly like Amplitude's custom events.")

    # ── Source table picker ───────────────────────────────────────────────────
    ce_event_tables = [k for k, v in valid_tables.items() if v.get("table_type") == "event_log"]
    if not ce_event_tables:
        st.info("No event tables found. Custom event builder requires an event log table.")
    else:
        ce_table_sel = st.selectbox(
            "Source table",
            ce_event_tables,
            format_func=lambda k: valid_tables[k].get("table_display_name", k),
            key="ce_table_sel",
        )
        ce_table_data   = {**valid_tables[ce_table_sel], "table_name": ce_table_sel}
        ce_events       = _event_options(ce_table_data)
        ce_event_labels = _event_display_map(ce_table_data)
        ce_field_labels = _field_display_map(ce_table_data)
        ce_signals      = resolve_table_signals(ce_table_data)
        ce_event_col    = ce_signals["event_col"] or "event_name"

        # ── Shared filterable columns (categorical + high priority) ───────────
        TIME_DERIVED_CE = {"date", "hour", "day_of_week", "week_num", "month",
                           "month_name", "value_date"}
        ALL_SKIP_CE = (USER_SIGNALS | TIME_SIGNALS | EVENT_SIGNALS
                       | {"event_id", "session_id"} | TIME_DERIVED_CE)
        _base_filterable = [
            col["raw_name"] for col in ce_table_data.get("columns", [])
            if col.get("raw_name") and col["raw_name"] not in ALL_SKIP_CE
            and bool(col.get("value_meanings"))
        ]
        _seen_base = set(ALL_SKIP_CE) | set(_base_filterable)
        _base_filterable += [
            col["raw_name"] for col in ce_table_data.get("columns", [])
            if col.get("raw_name") and col["raw_name"] not in _seen_base
            and col.get("analysis_priority") == "high"
        ]

        def _ce_filterable_for(event_name: str) -> list[str]:
            """Event-specific properties first, then shared filterable cols."""
            ev_props = []
            for ev in ce_table_data.get("events", []):
                if ev.get("raw_name") == event_name:
                    ev_props = [p["raw_name"] for p in ev.get("properties", [])
                                if p.get("raw_name") and p["raw_name"] not in _seen_base]
                    break
            return ev_props + _base_filterable

        def _render_filter_row(gidx: int, fidx: int, event_name: str):
            """Render one AND-filter row for group gidx, filter fidx. Returns (field, op, value)."""
            filterable = _ce_filterable_for(event_name)
            sk_f = f"ceg{gidx}_f{fidx}"
            c1, c2, c3, c4 = st.columns([3, 2, 3, 1])
            with c1:
                field = st.selectbox(
                    "Field", [""] + filterable,
                    format_func=lambda x: ce_field_labels.get(x, x) if x else "— add filter —",
                    key=f"{sk_f}_field", label_visibility="collapsed",
                )
            with c2:
                op = st.selectbox(
                    "Op", ["=", "!=", ">", ">=", "<", "<=", "IS NULL", "IS NOT NULL"],
                    key=f"{sk_f}_op", label_visibility="collapsed",
                )
            with c3:
                if op in {"IS NULL", "IS NOT NULL"} or not field:
                    st.markdown(
                        "<div style='padding:8px 0;color:#94a3b8;font-size:12px'>—</div>",
                        unsafe_allow_html=True,
                    )
                    value = ""
                else:
                    candidates = _field_value_options(ce_table_data, field)
                    raw_val = st.selectbox(
                        "Value", [""] + candidates + ["Custom..."],
                        key=f"{sk_f}_val", label_visibility="collapsed",
                    )
                    value = (
                        st.text_input("Custom", key=f"{sk_f}_custom", label_visibility="collapsed")
                        if raw_val == "Custom..." else raw_val
                    )
            with c4:
                remove = st.button("✕", key=f"{sk_f}_rm", help="Remove filter")
            return field, op, value, remove

        # ── Session state: list of event groups ───────────────────────────────
        # Each group: {"event": str, "filter_count": int}
        if "ce_groups" not in st.session_state:
            st.session_state.ce_groups = [{"event": "", "filter_count": 1}]

        groups = st.session_state.ce_groups

        # Collect rendered values to build SQL
        group_clauses = []   # one SQL clause per group
        group_defs = []      # structured group definition for builder replay

        for gi, group in enumerate(groups):
            # OR separator between groups
            if gi > 0:
                st.markdown(
                    "<div style='text-align:center;margin:6px 0'>"
                    "<span style='background:#e0e7ff;color:#4338ca;padding:3px 14px;"
                    "border-radius:999px;font-size:12px;font-weight:700'>OR</span></div>",
                    unsafe_allow_html=True,
                )

            with st.container():
                st.markdown(
                    f"<div style='background:#f8faff;border:1px solid #e0e7ff;"
                    f"border-left:3px solid #6366f1;border-radius:8px;padding:12px 14px;margin-bottom:2px'>",
                    unsafe_allow_html=True,
                )

                row_top = st.columns([4, 1])
                with row_top[0]:
                    chosen_event = st.selectbox(
                        "Event",
                        [""] + ce_events,
                        format_func=lambda x: ce_event_labels.get(x, x) if x else "— select event —",
                        key=f"ceg{gi}_event",
                        label_visibility="collapsed",
                    )
                    groups[gi]["event"] = chosen_event
                with row_top[1]:
                    if len(groups) > 1 and st.button("Remove", key=f"ceg{gi}_del"):
                        groups.pop(gi)
                        st.rerun()

                # AND filter rows for this group
                st.markdown(
                    "<div style='font-size:11px;color:#6366f1;font-weight:700;"
                    "text-transform:uppercase;letter-spacing:0.06em;margin:8px 0 4px'>"
                    "AND filters</div>",
                    unsafe_allow_html=True,
                )

                filter_clauses = []
                filter_defs = []
                filters_to_remove = []
                for fi in range(groups[gi].get("filter_count", 1)):
                    field, op, value, remove = _render_filter_row(gi, fi, chosen_event)
                    if remove:
                        filters_to_remove.append(fi)
                    clause = _build_filter_clause(ce_table_data, field, op, value)
                    if clause:
                        filter_clauses.append(clause)
                        filter_defs.append({"field": field, "op": op, "value": value})

                if filters_to_remove:
                    groups[gi]["filter_count"] = max(1, groups[gi]["filter_count"] - len(filters_to_remove))
                    st.rerun()

                add_filter_col, _ = st.columns([1, 4])
                with add_filter_col:
                    if st.button("+ AND filter", key=f"ceg{gi}_addfilter"):
                        groups[gi]["filter_count"] = groups[gi].get("filter_count", 1) + 1
                        st.rerun()

                st.markdown("</div>", unsafe_allow_html=True)

                # Build clause for this group
                if chosen_event:
                    parts = [f'"{ce_event_col}" = \'{chosen_event}\''] + filter_clauses
                    group_clauses.append("(" + " AND ".join(parts) + ")")
                    group_defs.append({"event": chosen_event, "filters": filter_defs})

        # Add event group button
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        if st.button("＋ Add OR event", key="ce_add_group"):
            groups.append({"event": "", "filter_count": 1})
            st.rerun()

        # ── Generated SQL ─────────────────────────────────────────────────────
        ce_generated_sql = "\nOR ".join(group_clauses)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        st.markdown("**Generated WHERE clause**")
        st.code(ce_generated_sql or "-- select at least one event to generate SQL", language="sql")

        # ── Name + description + Save ─────────────────────────────────────────
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        ce_n1, ce_n2, ce_n3, ce_n4 = st.columns([2, 2, 1, 1])
        with ce_n1:
            default_name = _ce_being_edited.get("name", "") if _ce_edit_active else ""
            new_ce_name = st.text_input(
                "Segment name", value=default_name, placeholder="paying_user", key="ce_name",
                help="Snake_case identifier used in the analytics agent.",
            )
        with ce_n2:
            default_desc = _ce_being_edited.get("description", "") if _ce_edit_active else ""
            new_ce_desc = st.text_input(
                "Description", value=default_desc, placeholder="User who completed a payment", key="ce_desc",
            )
        with ce_n3:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            save_label = "💾 Update" if _ce_edit_active else "➕ Add"
            if st.button(save_label, type="primary", key="ce_add", use_container_width=True):
                if new_ce_name and ce_generated_sql:
                    bdef_new = {
                        "table": ce_table_sel,
                        "groups": group_defs,
                    }
                    if _ce_edit_active:
                        ces[_ce_edit_idx]["name"]               = new_ce_name
                        ces[_ce_edit_idx]["description"]        = new_ce_desc
                        ces[_ce_edit_idx]["sql"]                = ce_generated_sql
                        ces[_ce_edit_idx]["builder_definition"] = bdef_new
                        st.success(f"Updated '{new_ce_name}'")
                    else:
                        ces.append({
                            "name": new_ce_name,
                            "description": new_ce_desc,
                            "sql": ce_generated_sql,
                            "_source": "human",
                            "builder_definition": bdef_new,
                        })
                        st.success(f"Added '{new_ce_name}'")
                    # Reset builder state
                    st.session_state.ce_groups   = [{"event": "", "filter_count": 1}]
                    st.session_state.ce_edit_idx = None
                    st.session_state.pop("ce_name", None)
                    st.session_state.pop("ce_desc", None)
                    st.rerun()
                else:
                    st.warning("Segment name and at least one event are required.")
        with ce_n4:
            if _ce_edit_active:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                if st.button("✕ Cancel", key="ce_cancel", use_container_width=True):
                    st.session_state.ce_edit_idx = None
                    st.session_state.ce_groups   = [{"event": "", "filter_count": 1}]
                    st.session_state.pop("ce_name", None)
                    st.session_state.pop("ce_desc", None)
                    st.rerun()

        with st.expander("Manual SQL instead", expanded=False):
            mc1, mc2, mc3 = st.columns([2, 2, 2])
            with mc1:
                manual_name = st.text_input("Name", placeholder="paying_user", key="ce_manual_name")
            with mc2:
                manual_desc = st.text_input("Description", key="ce_manual_desc")
            with mc3:
                manual_sql = st.text_area("WHERE clause", height=68,
                                          placeholder="event_name = 'upi_payment_success'",
                                          key="ce_manual_sql")
            if st.button("Add manual", key="ce_manual_add"):
                if manual_name and manual_sql:
                    ces.append({"name": manual_name, "description": manual_desc,
                                "sql": manual_sql, "_source": "human"})
                    st.success(f"Added '{manual_name}'")
                    st.rerun()

    st.divider()
    st.markdown('</div>', unsafe_allow_html=True)

    # ── Table Metrics ─────────────────────────────────────────────────────────
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown("### 📐 Table Metrics")
    st.caption("KPI formulas tied to a specific table — bucketed into lifecycle stages like modern analytics tools.")

    table_sel = st.selectbox("Table", list(valid_tables.keys()),
                             format_func=lambda k: valid_tables[k].get("table_display_name", k))
    metrics = st.session_state.catalog[table_sel].setdefault("suggested_metrics", [])
    metric_table_data = {**valid_tables[table_sel], "table_name": table_sel}
    suppressed_metrics = metric_table_data.get("suppressed_metrics", [])
    metric_events = _event_options(metric_table_data)
    metric_event_labels = _event_display_map(metric_table_data)
    metric_fields = _field_options(metric_table_data)
    metric_field_labels = _field_display_map(metric_table_data)
    metric_numeric_fields = _numeric_field_options(metric_table_data)
    grouped_metrics = bucket_metrics(metrics)

    # ── Industry KPI checklist (curation accelerator) ────────────────────────
    _industry = (st.session_state.get("biz_industry", "") or "").lower()
    if "fintech" in _industry or "bank" in _industry or "neobank" in _industry:
        _industry_key = "fintech"
    elif "ecom" in _industry or "commerce" in _industry or "retail" in _industry:
        _industry_key = "ecommerce"
    elif "saas" in _industry or "b2b" in _industry:
        _industry_key = "saas"
    elif "health" in _industry or "care" in _industry or "med" in _industry:
        _industry_key = "healthcare"
    elif "edtech" in _industry or "education" in _industry or "learning" in _industry:
        _industry_key = "edtech"
    else:
        _industry_key = "universal"

    KPI_CHECKLIST = {
        "universal": ["dau", "wau", "mau", "activation_rate", "d7_retention"],
        "fintech": ["kyc_completion_rate", "payment_success_rate", "first_transaction_rate", "d7_retention", "arpu"],
        "ecommerce": ["checkout_completion_rate", "aov", "d7_retention", "mau"],
        "saas": ["trial_to_paid_conversion", "feature_activation_rate", "d30_retention", "mau"],
        "healthcare": ["appointment_completion_rate", "followup_rate", "d7_retention"],
        "edtech": ["course_completion_rate", "weekly_learning_users", "d7_retention"],
    }

    required_ids = KPI_CHECKLIST.get(_industry_key, KPI_CHECKLIST["universal"])
    metric_id_status = {}
    for m in metrics:
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        metric_id_status[mid] = m.get("status", "approved") or "approved"
    missing_ids = [mid for mid in required_ids if mid not in metric_id_status]

    with st.expander(f"🧭 Industry KPI checklist · {_industry_key} ({len(required_ids)-len(missing_ids)}/{len(required_ids)})", expanded=False):
        rows = []
        for mid in required_ids:
            stt = metric_id_status.get(mid, "missing")
            icon = "✅" if stt == "approved" else "🟡" if stt == "candidate" else "⛔" if stt == "rejected" else "➕"
            rows.append({"metric_id": mid, "status": f"{icon} {stt}"})
        st.dataframe(rows, use_container_width=True, hide_index=True)
        if missing_ids:
            st.caption("Missing KPIs: " + ", ".join(missing_ids))
            if st.button("➕ Add missing KPIs as candidates", key=f"add_missing_kpi_{table_sel}"):
                for mid in missing_ids:
                    metrics.append({
                        "id": mid,
                        "name": mid.replace("_", " ").title(),
                        "description": "Placeholder KPI added from industry checklist. Please curate SQL and description.",
                        "sql_hint": "",
                        "aarrr": "Other",
                        "category": "Other",
                        "type": "ratio",
                        "status": "candidate",
                        "confidence": "low",
                        "source": "kpi_checklist_placeholder",
                        "validation_reasons": ["Missing required KPI for this industry; added as placeholder."],
                    })
                st.rerun()

    status_counts = {
        "all": len(metrics),
        "approved": sum(1 for m in metrics if m.get("status") in ("approved", "", None)),
        "candidate": sum(1 for m in metrics if m.get("status") == "candidate"),
        "rejected": sum(1 for m in metrics if m.get("status") == "rejected"),
    }
    sf1, sf2, sf3, sf4 = st.columns([2, 1, 1, 1])
    with sf1:
        metric_status_filter = st.selectbox(
            "Metric status",
            ["all", "approved", "candidate", "rejected"],
            format_func=lambda x: f"{x.title()} ({status_counts.get(x, 0)})",
            key=f"metric_status_filter_{table_sel}",
        )
    with sf2:
        if st.button("✅ Approve all candidates", key=f"approve_all_{table_sel}", use_container_width=True):
            for m in metrics:
                if m.get("status") == "candidate":
                    m["status"] = "approved"
                    m["confidence"] = "high"
                    m["validation_reasons"] = []
            st.rerun()
    with sf3:
        if st.button("⛔ Reject all candidates", key=f"reject_all_{table_sel}", use_container_width=True):
            for m in metrics:
                if m.get("status") == "candidate":
                    m["status"] = "rejected"
                    m["confidence"] = "low"
            st.rerun()
    with sf4:
        if st.button("🧹 Hide rejected", key=f"hide_rej_{table_sel}", use_container_width=True):
            st.session_state[f"metric_status_filter_{table_sel}"] = "approved"
            st.rerun()

    summary_html = "".join(
        metric_bucket_badge(bucket, len(items))
        for bucket, items in grouped_metrics.items()
    ) or metric_bucket_badge("Other", 0)
    st.markdown(summary_html, unsafe_allow_html=True)

    _aarrr_opts = [b for b in METRIC_BUCKET_ORDER if b != "Other"] + [""]
    _metric_types = ["Unique users on event", "Event count", "% of users",
                     "Events per user", "Sum property", "Average property",
                     "Conversion funnel", "Retention (N-day)", "Active user churn",
                     "Formula (SQL)"]
    _MTYPE_TO_SQL_TYPE = {
        "Unique users on event": "simple",
        "Event count": "simple",
        "% of users": "ratio",
        "Events per user": "ratio",
        "Sum property": "simple",
        "Average property": "simple",
        "Conversion funnel": "conversion",
        "Retention (N-day)": "retention",
        "Active user churn": "ratio",
        "Formula (SQL)": "ratio",
    }
    TIME_DERIVED = {"date", "hour", "day_of_week", "week_num", "month", "month_name", "value_date"}
    ALL_SKIP = (USER_SIGNALS | TIME_SIGNALS | EVENT_SIGNALS | {"event_id", "session_id"} | TIME_DERIVED)

    esk = f"mbe_{table_sel}"   # builder key prefix
    edit_key   = f"mbedit_{table_sel}_idx"
    loaded_key = f"mbedit_{table_sel}_loaded"

    # ── Pre-populate builder when a different metric is selected ──────────────
    edit_idx   = st.session_state.get(edit_key, None)   # None = new metric mode
    last_loaded = st.session_state.get(loaded_key, -2)

    if edit_idx is not None and edit_idx != last_loaded and edit_idx < len(metrics):
        m    = metrics[edit_idx]
        bdef = m.get("builder_definition", {})

        # If no builder_definition (auto-generated metric), parse sql_hint
        if not bdef:
            sql_src = m.get("sql_hint") or m.get("sql") or ""
            bdef = _parse_sql_for_builder(sql_src)

        stored_mtype = bdef.get("builder_type", "Unique users on event")
        bucket_v     = m.get("aarrr", "") if m.get("aarrr", "") in _aarrr_opts else ""

        # Validate that parsed events actually exist in this table's event list
        valid_events = set(_event_options(metric_table_data))
        pev = bdef.get("primary_event", "")
        sev = bdef.get("secondary_event", "")
        pev = pev if pev in valid_events else ""
        sev = sev if sev in valid_events else ""

        st.session_state[f"{esk}_name"]      = m.get("name", "")
        st.session_state[f"{esk}_desc"]      = m.get("description", "")
        st.session_state[f"{esk}_mtype"]     = stored_mtype if stored_mtype in _metric_types else "Unique users on event"
        st.session_state[f"{esk}_bucket"]    = bucket_v
        st.session_state[f"{esk}_primary"]   = pev
        st.session_state[f"{esk}_secondary"] = sev
        st.session_state[f"{esk}_vfield"]    = bdef.get("value_field", "")
        st.session_state[f"{esk}_retdays"]   = bdef.get("retention_days", 7)
        for fi, fdata in enumerate(bdef.get("filters_structured", [])[:2]):
            st.session_state[f"{esk}_f{fi+1}_field"] = fdata.get("field", "")
            st.session_state[f"{esk}_f{fi+1}_op"]    = fdata.get("op", "=")
        for fi, fdata in enumerate(bdef.get("denom_filters_structured", [])[:2]):
            st.session_state[f"{esk}_df{fi+1}_field"] = fdata.get("field", "")
            st.session_state[f"{esk}_df{fi+1}_op"]    = fdata.get("op", "=")
        st.session_state[loaded_key] = edit_idx

    # ── helpers for the inline builder ───────────────────────────────────────
    def _ep_for(raw: str) -> list[str]:
        for ev in metric_table_data.get("events", []):
            if ev.get("raw_name") == raw:
                return [p["raw_name"] for p in ev.get("properties", []) if p.get("raw_name")]
        return []

    def _filt_fields(event_name: str) -> list[str]:
        ep   = _ep_for(event_name)
        seen = set(ep) | ALL_SKIP
        cat  = [c["raw_name"] for c in metric_table_data.get("columns", [])
                if c.get("raw_name") and c["raw_name"] not in seen and c.get("value_meanings")]
        seen.update(cat)
        hi   = [c["raw_name"] for c in metric_table_data.get("columns", [])
                if c.get("raw_name") and c["raw_name"] not in seen
                and c.get("analysis_priority") == "high"]
        return ep + cat + hi

    # ── Type metadata (icon, color, description, formula hint) ──────────────
    _MTYPE_META = {
        "Unique users on event": {
            "icon": "👤", "color": "#3b82f6",
            "desc": "Count of distinct users who performed an event",
            "formula": "COUNT(DISTINCT user_id) WHERE event = …",
        },
        "Event count": {
            "icon": "🔢", "color": "#6366f1",
            "desc": "Total number of times an event occurred",
            "formula": "COUNT(*) WHERE event = …",
        },
        "% of users": {
            "icon": "📊", "color": "#8b5cf6",
            "desc": "% of all users who performed an event",
            "formula": "users who did event ÷ all users × 100",
        },
        "Events per user": {
            "icon": "🔄", "color": "#06b6d4",
            "desc": "Average times each active user triggered an event",
            "formula": "COUNT(events) ÷ COUNT(DISTINCT user_id)",
        },
        "Sum property": {
            "icon": "∑", "color": "#f59e0b",
            "desc": "Sum of a numeric column filtered to an event",
            "formula": "SUM(numeric_field) WHERE event = …",
        },
        "Average property": {
            "icon": "⌀", "color": "#f97316",
            "desc": "Average of a numeric column filtered to an event",
            "formula": "AVG(numeric_field) WHERE event = …",
        },
        "Conversion funnel": {
            "icon": "⏩", "color": "#22c55e",
            "desc": "% of users who reached event B after event A",
            "formula": "users who did end event ÷ users who did start event × 100",
        },
        "Retention (N-day)": {
            "icon": "🔁", "color": "#ec4899",
            "desc": "% of cohort who returned and did an event N days later",
            "formula": "returned users on day N ÷ cohort size × 100",
        },
        "Active user churn": {
            "icon": "📉", "color": "#ef4444",
            "desc": "% of previously active users who became inactive in the next period",
            "formula": "(1 − active in curr period ÷ active in prev period) × 100",
        },
        "Formula (SQL)": {
            "icon": "💻", "color": "#64748b",
            "desc": "Custom SQL — edit the query directly",
            "formula": "write any SQL",
        },
    }

    def _event_pill(label: str, raw: str, display_map: dict, empty_text: str = "— pick event —") -> str:
        name = display_map.get(raw, raw) if raw else empty_text
        color = "#6366f1" if raw else "#94a3b8"
        bg    = "#eef2ff" if raw else "#f8fafc"
        bdr   = "#c7d2fe" if raw else "#e2e8f0"
        return (
            f"<div style='background:{bg};border:1.5px solid {bdr};border-radius:8px;"
            f"padding:10px 14px;margin-bottom:4px'>"
            f"<div style='font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;"
            f"letter-spacing:.05em;margin-bottom:4px'>{label}</div>"
            f"<div style='font-size:15px;font-weight:700;color:{color}'>{name}</div>"
            f"</div>"
        )

    def _render_inline_builder(metric_idx_or_none):
        """Render the guided builder. metric_idx_or_none=None means new metric."""
        is_edit = (metric_idx_or_none is not None)

        if not metric_events:
            st.info("Guided builder requires an event table. No events found.")
            return

        # ═══════════════════════════════════════════════════════════════════
        # SECTION 1 — Metric identity
        # ═══════════════════════════════════════════════════════════════════
        st.markdown(
            "<div style='font-size:11px;font-weight:700;color:#64748b;"
            "text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px'>"
            "Metric identity</div>",
            unsafe_allow_html=True,
        )
        id_c1, id_c2 = st.columns([5, 2])
        with id_c1:
            builder_mname = st.text_input(
                "Metric name", placeholder="e.g. KYC Completion Rate",
                key=f"{esk}_name",
            )
            builder_mdesc = st.text_area(
                "Description",
                placeholder="What this measures and why it matters for the business",
                height=72, key=f"{esk}_desc",
            )
        with id_c2:
            builder_bucket = st.selectbox(
                "Lifecycle stage", _aarrr_opts,
                index=_aarrr_opts.index(st.session_state.get(f"{esk}_bucket", _aarrr_opts[1]))
                      if st.session_state.get(f"{esk}_bucket", "") in _aarrr_opts else 1,
                key=f"{esk}_bucket",
            )

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        # ═══════════════════════════════════════════════════════════════════
        # SECTION 2 — Measure type with description
        # ═══════════════════════════════════════════════════════════════════
        st.markdown(
            "<div style='font-size:11px;font-weight:700;color:#64748b;"
            "text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px'>"
            "Measure type — <span style='font-weight:400;text-transform:none;font-size:11px'>"
            "changing this updates the event picker and SQL below</span></div>",
            unsafe_allow_html=True,
        )
        builder_metric_type = st.selectbox(
            "Measure type", _metric_types, key=f"{esk}_mtype",
            label_visibility="collapsed",
        )
        meta = _MTYPE_META.get(builder_metric_type, {})
        st.markdown(
            f"<div style='background:{meta.get('color','#64748b')}12;"
            f"border-left:4px solid {meta.get('color','#64748b')};"
            f"border-radius:0 10px 10px 0;padding:10px 16px;margin-bottom:2px'>"
            f"<div style='display:flex;align-items:center;gap:8px'>"
            f"<span style='font-size:20px'>{meta.get('icon','')}</span>"
            f"<div>"
            f"<div style='font-size:14px;font-weight:700;color:#0f172a'>{builder_metric_type}</div>"
            f"<div style='font-size:12px;color:#475569;margin-top:2px'>{meta.get('desc','')}</div>"
            f"</div></div>"
            f"<div style='font-size:11px;color:#64748b;margin-top:6px;font-family:monospace;"
            f"background:rgba(0,0,0,0.04);padding:4px 8px;border-radius:4px;display:inline-block'>"
            f"{meta.get('formula','')}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # ═══════════════════════════════════════════════════════════════════
        # SECTION 3 — Event picker (layout changes per type)
        # ═══════════════════════════════════════════════════════════════════
        is_funnel    = builder_metric_type == "Conversion funnel"
        is_retention = builder_metric_type == "Retention (N-day)"
        is_churn     = builder_metric_type == "Active user churn"
        is_pct       = builder_metric_type == "% of users"
        is_epu       = builder_metric_type == "Events per user"
        is_formula   = builder_metric_type == "Formula (SQL)"
        needs_value  = builder_metric_type in {"Sum property", "Average property"}

        RETENTION_WINDOWS = [1, 3, 7, 14, 28, 30, 60, 90]
        retention_days = st.session_state.get(f"{esk}_retdays", 7)
        if retention_days not in RETENTION_WINDOWS:
            retention_days = 7

        secondary_event = ""
        value_field     = ""
        primary_event   = ""

        if not is_formula:
            st.markdown(
                "<div style='font-size:11px;font-weight:700;color:#64748b;"
                "text-transform:uppercase;letter-spacing:.08em;margin:14px 0 8px'>"
                "Events</div>",
                unsafe_allow_html=True,
            )

        if is_retention:
            rc1, rc2, rc3 = st.columns([5, 5, 2])
            with rc1:
                st.markdown("**Cohort event** — first qualifying action")
                primary_event = st.selectbox(
                    "Cohort event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— select event —",
                    key=f"{esk}_primary", label_visibility="collapsed",
                )
            with rc2:
                st.markdown("**Return event** — blank = same as cohort")
                secondary_event = st.selectbox(
                    "Return event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— same as cohort —",
                    key=f"{esk}_secondary", label_visibility="collapsed",
                )
            with rc3:
                st.markdown("**Window**")
                retention_days = st.selectbox(
                    "Window", RETENTION_WINDOWS,
                    index=RETENTION_WINDOWS.index(retention_days)
                          if retention_days in RETENTION_WINDOWS else RETENTION_WINDOWS.index(7),
                    format_func=lambda d: f"Day {d}",
                    key=f"{esk}_retdays", label_visibility="collapsed",
                )

        elif is_churn:
            ch1, ch2, ch3 = st.columns([5, 5, 2])
            with ch1:
                st.markdown("**Active event** — defines 'active' (optional, blank = any event)")
                primary_event = st.selectbox(
                    "Active event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— any event = active —",
                    key=f"{esk}_primary", label_visibility="collapsed",
                )
            with ch2:
                st.markdown(
                    "<div style='background:#fef2f2;border-left:3px solid #ef4444;"
                    "border-radius:0 6px 6px 0;padding:8px 12px;font-size:12px;color:#7f1d1d'>"
                    "<strong>Logic:</strong> users active in prev {N} days but absent in last {N} days</div>",
                    unsafe_allow_html=True,
                )
                secondary_event = ""
            with ch3:
                st.markdown("**Window**")
                retention_days = st.selectbox(
                    "Window", RETENTION_WINDOWS,
                    index=RETENTION_WINDOWS.index(retention_days)
                          if retention_days in RETENTION_WINDOWS else RETENTION_WINDOWS.index(30),
                    format_func=lambda d: f"{d}d",
                    key=f"{esk}_retdays", label_visibility="collapsed",
                )

        elif is_funnel:
            fc1, arr_c, fc2 = st.columns([5, 1, 5])
            with fc1:
                st.markdown("**Start event** — who entered the funnel")
                primary_event = st.selectbox(
                    "Start event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— select event —",
                    key=f"{esk}_primary", label_visibility="collapsed",
                )
            with arr_c:
                st.markdown(
                    "<div style='text-align:center;padding-top:32px;font-size:22px;color:#6366f1'>→</div>",
                    unsafe_allow_html=True,
                )
            with fc2:
                st.markdown("**End event** — who completed")
                secondary_event = st.selectbox(
                    "End event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— select event —",
                    key=f"{esk}_secondary", label_visibility="collapsed",
                )

        elif is_pct:
            pct_c1, pct_div, pct_c2 = st.columns([5, 1, 5])
            with pct_c1:
                st.markdown("**Numerator event** — users who did this")
                primary_event = st.selectbox(
                    "Numerator event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— select event —",
                    key=f"{esk}_primary", label_visibility="collapsed",
                )
            with pct_div:
                st.markdown(
                    "<div style='text-align:center;padding-top:30px;font-size:18px;color:#94a3b8'>÷</div>",
                    unsafe_allow_html=True,
                )
            with pct_c2:
                st.markdown(
                    "**Denominator event** — base cohort "
                    "<span style='font-weight:400;color:#64748b'>(blank = all users)</span>",
                    unsafe_allow_html=True,
                )
                secondary_event = st.selectbox(
                    "Denominator event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— all users —",
                    key=f"{esk}_secondary", label_visibility="collapsed",
                )

        elif is_epu:
            st.markdown("**Event** — count occurrences ÷ distinct users")
            primary_event = st.selectbox(
                "Event", [""] + metric_events,
                format_func=lambda x: metric_event_labels.get(x, x) if x else "— select event —",
                key=f"{esk}_primary", label_visibility="collapsed",
            )

        elif not is_formula:
            # Unique users / Event count / Sum / Average
            ec1, ec2 = st.columns([3, 2])
            with ec1:
                lbl = "Event" if not needs_value else "Event to filter on"
                st.markdown(f"**{lbl}**")
                primary_event = st.selectbox(
                    "Event", [""] + metric_events,
                    format_func=lambda x: metric_event_labels.get(x, x) if x else "— select event —",
                    key=f"{esk}_primary", label_visibility="collapsed",
                )
            with ec2:
                if needs_value:
                    st.markdown("**Numeric field**")
                    value_field = st.selectbox(
                        "Numeric field", [""] + metric_numeric_fields,
                        format_func=lambda x: metric_field_labels.get(x, x) if x else "— select field —",
                        key=f"{esk}_vfield", label_visibility="collapsed",
                    )
                else:
                    value_field = st.session_state.get(f"{esk}_vfield", "")

        # ── Live formula preview pill (non-formula types) ─────────────────
        if not is_formula and not is_retention and not is_churn:
            pev_name  = metric_event_labels.get(primary_event, primary_event) if primary_event else None
            sev_name  = metric_event_labels.get(secondary_event, secondary_event) if secondary_event else None
            vf_name   = metric_field_labels.get(value_field, value_field) if value_field else None
            color     = meta.get("color", "#6366f1")
            if pev_name and is_funnel and sev_name:
                pill = (f"<span style='color:{color};font-weight:700'>{pev_name}</span>"
                        f" &nbsp;→&nbsp; "
                        f"<span style='color:{color};font-weight:700'>{sev_name}</span>"
                        f" &nbsp;= conversion %")
            elif pev_name and is_pct and sev_name:
                pill = (f"users who did <span style='color:{color};font-weight:700'>{pev_name}</span>"
                        f" &nbsp;÷&nbsp; users who did "
                        f"<span style='color:#64748b;font-weight:700'>{sev_name}</span>"
                        f" &nbsp;× 100")
            elif pev_name and is_pct:
                pill = (f"users who did <span style='color:{color};font-weight:700'>{pev_name}</span>"
                        f" &nbsp;÷&nbsp; <em>all users</em> &nbsp;× 100")
            elif pev_name and is_epu:
                pill = (f"COUNT(<span style='color:{color};font-weight:700'>{pev_name}</span>)"
                        f" &nbsp;÷&nbsp; distinct users")
            elif pev_name and needs_value and vf_name:
                agg = "SUM" if "Sum" in builder_metric_type else "AVG"
                pill = (f"{agg}(<span style='color:{color};font-weight:700'>{vf_name}</span>)"
                        f" WHERE event = <span style='color:{color};font-weight:700'>{pev_name}</span>")
            elif pev_name:
                agg = "COUNT(DISTINCT user_id)" if "Unique" in builder_metric_type else "COUNT(*)"
                pill = (f"{agg} WHERE event = <span style='color:{color};font-weight:700'>{pev_name}</span>")
            else:
                pill = None

            if pill:
                st.markdown(
                    f"<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;"
                    f"padding:10px 14px;font-size:13px;color:#475569;margin-top:4px'>"
                    f"📐 {pill}</div>",
                    unsafe_allow_html=True,
                )

        # ═══════════════════════════════════════════════════════════════════
        # SECTION 4 — Filters
        # ═══════════════════════════════════════════════════════════════════
        f1_field = f1_op = f1_val = ""
        f2_field = f2_op = f2_val = ""
        df1_field = df1_op = df1_val = ""
        df2_field = df2_op = df2_val = ""
        mf1 = mf2 = ""
        denom_filter_sql = ""
        ret_filter_sql = ""
        numerator_clause_override = ""
        denominator_clause_override = ""
        cohort_clause_override = ""
        return_clause_override = ""

        def _filter_row(label_prefix: str, key_prefix: str, event_ctx: str):
            """Render one filter row. Returns (field, op, val)."""
            _ctx = _filt_fields(event_ctx)
            _fc1, _fc2, _fc3 = st.columns([4, 2, 3])
            with _fc1:
                _fld = st.selectbox(
                    f"{label_prefix} field", [""] + _ctx,
                    format_func=lambda x: metric_field_labels.get(x, x) if x else f"{label_prefix} — pick field",
                    key=f"{key_prefix}_field", label_visibility="collapsed",
                )
            with _fc2:
                _op = st.selectbox(
                    f"{label_prefix} op", ["=", "!=", ">", ">=", "<", "<=", "IS NULL", "IS NOT NULL"],
                    key=f"{key_prefix}_op", label_visibility="collapsed",
                )
            with _fc3:
                if _op in {"IS NULL", "IS NOT NULL"} or not _fld:
                    st.markdown(
                        "<div style='padding:8px 4px;color:#cbd5e1;font-size:13px'>—</div>",
                        unsafe_allow_html=True,
                    )
                    _val = ""
                else:
                    _cands = _field_value_options(metric_table_data, _fld)
                    _raw_v = st.selectbox(
                        f"{label_prefix} val", [""] + _cands + ["Custom..."],
                        key=f"{key_prefix}_val", label_visibility="collapsed",
                    )
                    _val = (st.text_input(f"{label_prefix} custom", key=f"{key_prefix}_custom",
                                         label_visibility="collapsed")
                            if _raw_v == "Custom..." else _raw_v)
            return _fld, _op, _val

        def _on_custom_event_change():
            """
            Streamlit-safe autofill hook for custom event selection.
            Runs as widget callback, so mutating other widget keys is allowed.
            """
            seg_key = f"{esk}_segment"
            selected_name = st.session_state.get(seg_key, "")
            if not selected_name:
                return
            ce_list = st.session_state.get("custom_events", [])
            selected_ce = next((ce for ce in ce_list if ce.get("name") == selected_name), None)
            if not selected_ce:
                return

            inferred_primary = ""
            inferred_secondary = ""
            bdef = selected_ce.get("builder_definition", {}) if isinstance(selected_ce, dict) else {}
            groups = bdef.get("groups", []) if isinstance(bdef, dict) else []
            if groups:
                inferred_primary = (groups[0] or {}).get("event", "") or ""
                if len(groups) > 1:
                    inferred_secondary = (groups[1] or {}).get("event", "") or ""
            if not inferred_primary:
                parsed_groups = _parse_custom_event_where(selected_ce.get("sql", ""))
                if parsed_groups:
                    inferred_primary = (parsed_groups[0] or {}).get("event", "") or ""
                    if len(parsed_groups) > 1:
                        inferred_secondary = (parsed_groups[1] or {}).get("event", "") or ""
            if not inferred_primary:
                fallback = _parse_sql_for_builder(selected_ce.get("sql", ""))
                inferred_primary = (fallback.get("primary_event", "") if isinstance(fallback, dict) else "") or ""
                inferred_secondary = (fallback.get("secondary_event", "") if isinstance(fallback, dict) else "") or ""

            # Detect multi-event custom event; preserve full OR logic for simple metric types.
            event_names = sorted({(g or {}).get("event", "") for g in groups if (g or {}).get("event", "")})
            multi_event_custom = len(event_names) > 1
            st.session_state[f"{esk}_segment_multi"] = multi_event_custom

            if inferred_primary and inferred_primary in metric_events:
                st.session_state[f"{esk}_primary"] = inferred_primary
            if inferred_secondary and inferred_secondary in metric_events:
                mtype_now = st.session_state.get(f"{esk}_mtype", "")
                if mtype_now in {"Conversion funnel", "% of users", "Retention (N-day)"}:
                    st.session_state[f"{esk}_secondary"] = inferred_secondary

        def _ce_primary_event(ce_obj: dict) -> str:
            if not ce_obj:
                return ""
            bdef = ce_obj.get("builder_definition", {}) if isinstance(ce_obj, dict) else {}
            groups = bdef.get("groups", []) if isinstance(bdef, dict) else []
            if groups and (groups[0] or {}).get("event", ""):
                return (groups[0] or {}).get("event", "") or ""
            parsed = _parse_custom_event_where(ce_obj.get("sql", ""))
            if parsed and (parsed[0] or {}).get("event", ""):
                return (parsed[0] or {}).get("event", "") or ""
            fallback = _parse_sql_for_builder(ce_obj.get("sql", ""))
            return (fallback.get("primary_event", "") if isinstance(fallback, dict) else "") or ""

        if not is_formula:
            # ── Custom event shortcut (reusable segment) ────────────────────
            _ce_list = st.session_state.get("custom_events", [])
            _segment_sql = ""
            _segment_sql_effective = ""
            _selected_ce = None
            _selected_ce_num = None
            _selected_ce_den = None
            if _ce_list:
                _ce_names = [ce["name"] for ce in _ce_list]
                _ce_desc = {ce["name"]: ce.get("description", ce["name"]) for ce in _ce_list}
                st.markdown(
                    "<div style='font-size:11px;color:#6366f1;font-weight:700;"
                    "text-transform:uppercase;letter-spacing:.08em;margin:12px 0 6px'>"
                    "⚡ Use custom event (optional)</div>",
                    unsafe_allow_html=True,
                )
                if builder_metric_type in {"% of users", "Retention (N-day)"}:
                    cnum, cden = st.columns(2)
                    with cnum:
                        _seg_num = st.selectbox(
                            "Cohort/Numerator custom event",
                            [""] + _ce_names,
                            format_func=lambda x: f"⚡ {_ce_desc.get(x, x)}" if x else "— none —",
                            key=f"{esk}_segment_num",
                        )
                    with cden:
                        _seg_den = st.selectbox(
                            "Return/Denominator custom event",
                            [""] + _ce_names,
                            format_func=lambda x: f"⚡ {_ce_desc.get(x, x)}" if x else "— none —",
                            key=f"{esk}_segment_den",
                        )
                    _selected_ce_num = next((ce for ce in _ce_list if ce.get("name") == _seg_num), None)
                    _selected_ce_den = next((ce for ce in _ce_list if ce.get("name") == _seg_den), None)
                    if _selected_ce_num:
                        _pe = _ce_primary_event(_selected_ce_num)
                        if _pe and _pe in metric_events:
                            primary_event = _pe
                        st.caption(f"Using cohort/numerator custom SQL: `{(_selected_ce_num.get('sql','') or '')[:100]}{'…' if len((_selected_ce_num.get('sql','') or '')) > 100 else ''}`")
                    if _selected_ce_den:
                        _se = _ce_primary_event(_selected_ce_den)
                        if _se and _se in metric_events:
                            secondary_event = _se
                        st.caption(f"Using return/denominator custom SQL: `{(_selected_ce_den.get('sql','') or '')[:100]}{'…' if len((_selected_ce_den.get('sql','') or '')) > 100 else ''}`")
                else:
                    _seg_sel = st.selectbox(
                        "Custom event",
                        [""] + _ce_names,
                        format_func=lambda x: f"⚡ {_ce_desc.get(x, x)}" if x else "— no custom event filter —",
                        key=f"{esk}_segment",
                        help="Applies saved custom event SQL as a reusable filter in this metric.",
                        on_change=_on_custom_event_change,
                    )
                    _selected_ce = next((ce for ce in _ce_list if ce.get("name") == _seg_sel), None)
                    for _ce in _ce_list:
                        if _ce["name"] == _seg_sel:
                            _segment_sql = _ce.get("sql", "")
                            break
                    if _segment_sql:
                        st.caption(f"Using custom event SQL: `{_segment_sql[:120]}{'…' if len(_segment_sql) > 120 else ''}`")
                    if _selected_ce:
                        st.markdown(
                            "<div style='display:inline-block;margin-top:4px;padding:4px 10px;"
                            "border-radius:999px;background:#eef2ff;border:1px solid #c7d2fe;"
                            "color:#4338ca;font-size:11px;font-weight:700'>"
                            "Mode: Custom Event SQL (locked)</div>",
                            unsafe_allow_html=True,
                        )

            _custom_event_selected = bool(_selected_ce or _selected_ce_num or _selected_ce_den)
            if _custom_event_selected:
                st.info("Using selected custom event as final filter logic. Manual filter editing is disabled.")
                mf1 = ""
                mf2 = ""
            else:
                ctx = _filt_fields(primary_event)
                # ── Cohort / primary event filters ──────────────────────────────
                filter_section_label = (
                    "Cohort event filters" if is_retention
                    else f"Filters <span style='font-weight:400;font-size:11px;"
                         f"text-transform:none'>— optional · {len(ctx)} available fields</span>"
                )
                st.markdown(
                    f"<div style='font-size:11px;font-weight:700;color:#64748b;"
                    f"text-transform:uppercase;letter-spacing:.08em;margin:14px 0 8px'>"
                    f"{filter_section_label}</div>",
                    unsafe_allow_html=True,
                )

                f1_field, f1_op, f1_val = _filter_row("Filter 1", f"{esk}_f1", primary_event)
                f2_field, f2_op, f2_val = _filter_row("Filter 2", f"{esk}_f2", primary_event)

                mf1 = _build_filter_clause(metric_table_data, f1_field, f1_op, f1_val)
                mf2 = _build_filter_clause(metric_table_data, f2_field, f2_op, f2_val)

                # ── Denominator event filters (% of users with a base cohort) ──
                if is_pct and secondary_event:
                    st.markdown(
                        "<div style='font-size:11px;font-weight:700;color:#8b5cf6;"
                        "text-transform:uppercase;letter-spacing:.08em;margin:12px 0 8px'>"
                        "Denominator event filters <span style='font-weight:400;text-transform:none'>"
                        f"— applied to <em>{secondary_event}</em> cohort</span></div>",
                        unsafe_allow_html=True,
                    )
                    df1_field, df1_op, df1_val = _filter_row("Denom filter 1", f"{esk}_df1", secondary_event)
                    df2_field, df2_op, df2_val = _filter_row("Denom filter 2", f"{esk}_df2", secondary_event)
                    _df1 = _build_filter_clause(metric_table_data, df1_field, df1_op, df1_val)
                    _df2 = _build_filter_clause(metric_table_data, df2_field, df2_op, df2_val)
                    denom_filter_sql = _combine_clauses([_df1, _df2])

                # ── Return event filters (Retention only) ───────────────────────
                if is_retention:
                    ret_evt = secondary_event or primary_event
                    st.markdown(
                        "<div style='font-size:11px;font-weight:700;color:#ec4899;"
                        "text-transform:uppercase;letter-spacing:.08em;margin:12px 0 8px'>"
                        "Return event filter <span style='font-weight:400;text-transform:none'>"
                        "— applied inside the returned CTE</span></div>",
                        unsafe_allow_html=True,
                    )
                    rf_field, rf_op, rf_val = _filter_row("Return filter", f"{esk}_rf1", ret_evt)
                    ret_filter_sql = _build_filter_clause(metric_table_data, rf_field, rf_op, rf_val)

            # Structural merge: use only non-event filter atoms from custom event.
            if _selected_ce_num or _selected_ce_den:
                if _selected_ce_num:
                    mf1 = _custom_event_extra_clause(_selected_ce_num, metric_table_data, primary_event)
                    mf2 = ""
                    if builder_metric_type == "% of users":
                        numerator_clause_override = (_selected_ce_num.get("sql", "") or "").strip()
                    elif builder_metric_type == "Retention (N-day)":
                        cohort_clause_override = (_selected_ce_num.get("sql", "") or "").strip()
                if _selected_ce_den:
                    _den_clause = _custom_event_extra_clause(_selected_ce_den, metric_table_data, secondary_event)
                    if builder_metric_type == "% of users":
                        denom_filter_sql = _den_clause
                        denominator_clause_override = (_selected_ce_den.get("sql", "") or "").strip()
                    elif builder_metric_type == "Retention (N-day)":
                        ret_filter_sql = _den_clause
                        return_clause_override = (_selected_ce_den.get("sql", "") or "").strip()
            elif _selected_ce:
                _ce_groups = []
                _ce_bdef = _selected_ce.get("builder_definition", {}) if isinstance(_selected_ce, dict) else {}
                if isinstance(_ce_bdef, dict):
                    _ce_groups = _ce_bdef.get("groups", []) or []
                if not _ce_groups:
                    _ce_groups = _parse_custom_event_where(_selected_ce.get("sql", ""))
                _distinct_events = {g.get("event", "") for g in _ce_groups if g.get("event", "")}
                _is_multi_event_ce = len(_distinct_events) > 1
                # For multi-event custom events, keep full OR logic instead of collapsing to one event path.
                if _is_multi_event_ce and builder_metric_type in {"Unique users on event", "Event count", "Sum property", "Average property"}:
                    _segment_sql_effective = (_selected_ce.get("sql", "") or "").strip()
                    # Clear primary event so SQL uses full custom-event WHERE logic.
                    primary_event = ""
                else:
                    _segment_sql_effective = _custom_event_extra_clause(_selected_ce, metric_table_data, primary_event)
            elif _segment_sql:
                # Last-resort fallback for legacy free-form SQL with no parseable structure.
                _segment_sql_effective = _segment_sql
            # Merge segment SQL into filter slots
            _extra_filters = [f for f in [mf1, mf2, _segment_sql_effective or _segment_sql] if f]
            mf1 = _extra_filters[0] if len(_extra_filters) > 0 else ""
            mf2 = _extra_filters[1] if len(_extra_filters) > 1 else ""
            if len(_extra_filters) > 2:
                mf2 = " AND ".join(_extra_filters[1:])

        # ═══════════════════════════════════════════════════════════════════
        # SECTION 5 — SQL preview
        # ═══════════════════════════════════════════════════════════════════
        st.markdown(
            "<div style='font-size:11px;font-weight:700;color:#64748b;"
            "text-transform:uppercase;letter-spacing:.08em;margin:14px 0 6px'>"
            "SQL preview</div>",
            unsafe_allow_html=True,
        )
        if is_formula:
            existing_sql = ""
            if metric_idx_or_none is not None and metric_idx_or_none < len(metrics):
                existing_sql = metrics[metric_idx_or_none].get("sql_hint", "") or ""
            st.markdown(
                "<div style='background:#fef3c7;border-left:3px solid #f59e0b;"
                "border-radius:0 8px 8px 0;padding:8px 14px;margin-bottom:8px;font-size:12px;color:#92400e'>"
                "<strong>Custom SQL formula</strong> — this metric uses a query that can't be"
                " expressed with the guided builder. Edit the SQL directly below."
                " Switch to a different measure type above to use the guided builder instead."
                "</div>",
                unsafe_allow_html=True,
            )
            gen_sql = st.text_area(
                "SQL", height=220,
                value=st.session_state.get(f"{esk}_formula_sql", existing_sql),
                key=f"{esk}_formula_sql",
                label_visibility="collapsed",
                placeholder="-- Write any SQL query here",
            )
        else:
            gen_sql = _metric_preview_sql(
                table_sel, metric_table_data, builder_metric_type,
                primary_event, secondary_event, value_field, mf1, mf2,
                retention_days=retention_days,
                return_filter=ret_filter_sql,
                denom_filter=denom_filter_sql,
                numerator_clause_override=numerator_clause_override,
                denominator_clause_override=denominator_clause_override,
                cohort_clause_override=cohort_clause_override,
                return_clause_override=return_clause_override,
            )
            if gen_sql:
                st.code(gen_sql, language="sql")
            else:
                st.markdown(
                    "<div style='background:#f8fafc;border:1px dashed #cbd5e1;border-radius:8px;"
                    "padding:14px;text-align:center;color:#94a3b8;font-size:13px'>"
                    "Select an event above to preview SQL</div>",
                    unsafe_allow_html=True,
                )

        # ═══════════════════════════════════════════════════════════════════
        # SECTION 6 — Save / Cancel
        # ═══════════════════════════════════════════════════════════════════
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        # Auto-derive SQL type from Measure type (no dropdown needed)
        builder_mtype = _MTYPE_TO_SQL_TYPE.get(builder_metric_type, "simple")
        bdef_to_save = {
            "builder_type":       builder_metric_type,
            "table":              table_sel,
            "primary_event":      primary_event,
            "secondary_event":    secondary_event,
            "value_field":        value_field,
            "retention_days":     retention_days,
            "filters":            [mf1, mf2],
            "return_filter":      ret_filter_sql,
            "denom_filter":       denom_filter_sql,
            "filters_structured": [
                {"field": f1_field, "op": f1_op, "value": f1_val},
                {"field": f2_field, "op": f2_op, "value": f2_val},
            ],
            "denom_filters_structured": [
                {"field": df1_field, "op": df1_op, "value": df1_val},
                {"field": df2_field, "op": df2_op, "value": df2_val},
            ],
        }
        metric_payload = {
            "id":                 metrics[metric_idx_or_none].get("id", "") if is_edit else "",
            "name":               builder_mname,
            "description":        builder_mdesc,
            "sql_hint":           gen_sql,
            "aarrr":              builder_bucket,
            "category":           builder_bucket,
            "type":               builder_mtype,
            "status":             (metrics[metric_idx_or_none].get("status", "approved") if is_edit else "approved"),
            "confidence":         (metrics[metric_idx_or_none].get("confidence", "high") if is_edit else "high"),
            "validation_reasons": (metrics[metric_idx_or_none].get("validation_reasons", []) if is_edit else []),
            "source":             (metrics[metric_idx_or_none].get("source", "human_curated") if is_edit else "human_curated"),
            "builder_definition": bdef_to_save,
        }

        sc, cc = st.columns([3, 1])
        with sc:
            btn_label = "💾 Update Metric" if is_edit else "➕ Add to Metrics"
            if st.button(btn_label, type="primary", key=f"{esk}_save", use_container_width=True):
                if builder_mname and gen_sql:
                    if is_edit:
                        metrics[metric_idx_or_none] = metric_payload
                        st.success(f"Updated '{builder_mname}'")
                    else:
                        metrics.append(metric_payload)
                        st.success(f"Added '{builder_mname}'")
                        st.session_state[new_open_key] = False
                    st.session_state[edit_key]   = None
                    st.session_state[loaded_key] = -2
                    st.rerun()
                else:
                    st.warning("Metric name and SQL are required.")
        with cc:
            if st.button("✕ Cancel", key=f"{esk}_cancel", use_container_width=True):
                st.session_state[edit_key]     = None
                st.session_state[loaded_key]   = -2
                st.session_state[new_open_key] = False
                st.rerun()

    # ── ＋ New Metric button ───────────────────────────────────────────────────
    new_open_key = f"mbe_newopen_{table_sel}"
    if new_open_key not in st.session_state:
        st.session_state[new_open_key] = False

    new_col, _ = st.columns([2, 5])
    with new_col:
        new_btn_label = "✕ Close builder" if (edit_idx is None and st.session_state[new_open_key]) else "＋ New Metric"
        if st.button(new_btn_label, key=f"mbe_new_{table_sel}", use_container_width=True):
            if st.session_state[new_open_key] and edit_idx is None:
                # Close new-metric builder
                st.session_state[new_open_key] = False
            else:
                # Open new-metric builder, close any edit in progress
                st.session_state[new_open_key] = True
                st.session_state[edit_key]     = None
                st.session_state[loaded_key]   = -2
                for suffix in ["name","desc","mtype","bucket","primary","secondary",
                               "vfield","retdays","segment","segment_num","segment_den","formula_sql",
                               "f1_field","f1_op","f1_val","f1_custom",
                               "f2_field","f2_op","f2_val","f2_custom",
                               "df1_field","df1_op","df1_val","df1_custom",
                               "df2_field","df2_op","df2_val","df2_custom",
                               "rf1_field","rf1_op","rf1_val","rf1_custom"]:
                    st.session_state.pop(f"{esk}_{suffix}", None)
            st.rerun()

    # When a metric Edit button is clicked, close the new-metric builder
    if edit_idx is not None:
        st.session_state[new_open_key] = False

    # ── Metric list: full detail cards ────────────────────────────────────────
    if not metrics:
        st.caption("No metrics yet — click ＋ New Metric above to add one.")

    for bucket in METRIC_BUCKET_ORDER:
        bucket_items = grouped_metrics.get(bucket, [])
        if not bucket_items:
            continue
        color = METRIC_BUCKET_COLORS.get(bucket, "#64748b")
        st.markdown(
            f"<div style='font-size:11px;font-weight:700;color:{color};"
            f"text-transform:uppercase;letter-spacing:0.06em;margin:18px 0 8px'>"
            f"{bucket} · {len(bucket_items)}</div>",
            unsafe_allow_html=True,
        )

        for mi, m in bucket_items:
            status = m.get("status", "approved") or "approved"
            if metric_status_filter != "all" and status != metric_status_filter:
                continue
            is_editing = (edit_idx == mi)
            border = "#6366f1" if is_editing else "rgba(15,23,42,0.1)"
            bg     = "#f5f3ff" if is_editing else "white"
            bdef   = m.get("builder_definition", {})
            pev    = bdef.get("primary_event", "")
            sev    = bdef.get("secondary_event", "")
            vf     = bdef.get("value_field", "")
            sql_h  = (m.get("sql_hint") or "").strip()

            # Show builder_type with icon/color (fall back to sql type)
            display_mtype = bdef.get("builder_type") or (m.get("type") or "simple")
            card_meta  = _MTYPE_META.get(display_mtype, {"icon": "📊", "color": "#64748b"})
            type_color = card_meta["color"]
            type_icon  = card_meta["icon"]

            # Build event chip string
            event_parts = []
            if pev:
                event_parts.append(metric_event_labels.get(pev, pev))
            if sev:
                event_parts.append("→ " + metric_event_labels.get(sev, sev))
            if vf:
                event_parts.append(f"({metric_field_labels.get(vf, vf)})")
            event_chip = " &nbsp;→&nbsp; ".join(
                [metric_event_labels.get(pev, pev)] + (["→ " + metric_event_labels.get(sev, sev)] if sev else [])
            ) if pev else ""

            st.markdown(
                f'<div style="background:{bg};border:1px solid {border};'
                f'border-left:3px solid {"#6366f1" if is_editing else type_color};'
                f'border-radius:10px;padding:14px 16px;margin-bottom:4px">'
                f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">'
                f'<div style="flex:1;min-width:0">'
                f'<div style="font-weight:800;font-size:15px;color:#0f172a;margin-bottom:3px">'
                f'{m.get("name","Unnamed")}</div>'
                + (f'<div style="font-size:13px;color:#475569;margin-bottom:5px;line-height:1.4">'
                   f'{m.get("description","")}</div>'
                   if m.get("description") else "")
                + (f'<div style="font-size:12px;font-weight:600;color:{type_color};'
                   f'background:{type_color}12;display:inline-block;padding:2px 8px;'
                   f'border-radius:4px;margin-bottom:2px">'
                   f'{event_chip}</div>'
                   if event_chip else "")
                + f'</div>'
                f'<span style="flex-shrink:0;font-size:11px;background:{type_color}15;color:{type_color};'
                f'border:1px solid {type_color}30;'
                f'padding:3px 9px;border-radius:20px;white-space:nowrap;font-weight:600">'
                f'{type_icon} {display_mtype}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<div style='margin:-2px 0 8px'>"
                f"<span style='font-size:11px;padding:2px 8px;border-radius:999px;"
                f"background:{'#dcfce7' if status=='approved' else '#fef3c7' if status=='candidate' else '#fee2e2'};"
                f"color:{'#166534' if status=='approved' else '#92400e' if status=='candidate' else '#991b1b'};"
                f"font-weight:700'>status: {status}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if sql_h:
                st.code(sql_h, language="sql")
            st.markdown("</div>", unsafe_allow_html=True)

            # Edit / Delete buttons sit just below the card
            btn_c1, btn_c2, btn_c3, btn_c4, _ = st.columns([1, 1, 1, 1, 3])
            with btn_c1:
                edit_label = "✏️ Editing…" if is_editing else "✏️ Edit"
                if st.button(edit_label, key=f"mbedit_sel_{table_sel}_{mi}",
                             type="primary" if is_editing else "secondary",
                             use_container_width=True):
                    if is_editing:
                        # Clicking again collapses the builder
                        st.session_state[edit_key]   = None
                        st.session_state[loaded_key] = -2
                    else:
                        st.session_state[edit_key] = mi
                    st.rerun()
            with btn_c2:
                if st.button("🗑 Delete", key=f"mbedit_del_{table_sel}_{mi}", use_container_width=True):
                    metrics.pop(mi)
                    if st.session_state.get(edit_key) == mi:
                        st.session_state[edit_key]   = None
                        st.session_state[loaded_key] = -2
                    st.rerun()
            with btn_c3:
                if st.button("✅ Approve", key=f"mbedit_app_{table_sel}_{mi}", use_container_width=True):
                    m["status"] = "approved"
                    m["confidence"] = "high"
                    m["validation_reasons"] = []
                    st.rerun()
            with btn_c4:
                if st.button("⛔ Reject", key=f"mbedit_rej_{table_sel}_{mi}", use_container_width=True):
                    m["status"] = "rejected"
                    m["confidence"] = "low"
                    st.rerun()

            # Inline builder — only shown for the metric being edited
            if is_editing:
                _render_inline_builder(mi)

    # ── New Metric builder — shown at the bottom when new_open_key is True ──────
    if edit_idx is None and st.session_state.get(new_open_key, False):
        _render_inline_builder(None)

    if suppressed_metrics:
        with st.expander(f"⚠ {len(suppressed_metrics)} suppressed candidates", expanded=False):
            for sm in suppressed_metrics:
                reasons = "; ".join(sm.get("validation_reasons", ["Validation failed"]))
                st.markdown(
                    f'<div style="background:#fff7ed;border:1px solid #fdba74;'
                    f'border-radius:8px;padding:8px 12px;margin-bottom:6px;font-size:12px">'
                    f'<b>{sm.get("name","")}</b><br>'
                    f'<span style="color:#92400e">{reasons}</span></div>',
                    unsafe_allow_html=True,
                )

    # ── Funnel Studio (easy builder + SQL preview) ─────────────────────────────
    saved_funnels = st.session_state.catalog[table_sel].setdefault("saved_funnels", [])
    st.markdown("## 🧩 Funnel Studio")
    st.caption("Build step-by-step funnels as a flow. Use named funnels in chat and the orchestrator will auto-populate steps.")
    signals = resolve_table_signals(metric_table_data)
    event_col = signals.get("event_col") or "event_name"
    user_col = signals.get("user_col") or "user_id"

    for fi, f in enumerate(saved_funnels):
        with st.expander(f.get("name", f"Funnel {fi+1}"), expanded=False):
            f["name"] = st.text_input("Funnel name", value=f.get("name", ""), key=f"funnel_name_{table_sel}_{fi}")
            f["description"] = st.text_input(
                "Description", value=f.get("description", ""), key=f"funnel_desc_{table_sel}_{fi}"
            )
            current_steps = [s for s in f.get("steps", []) if s in metric_events]
            f["steps"] = st.multiselect(
                "Ordered steps",
                metric_events,
                default=current_steps,
                format_func=lambda x: metric_event_labels.get(x, x),
                key=f"funnel_steps_{table_sel}_{fi}",
            )
            preview_sql = _funnel_preview_sql(table_sel, user_col, event_col, f.get("steps", []))
            if preview_sql:
                st.code(preview_sql, language="sql")
            create_cols = st.columns([1, 1, 2])
            with create_cols[0]:
                if st.button("➕ Create metric from funnel", key=f"funnel_metric_{table_sel}_{fi}"):
                    fname = (f.get("name") or f"funnel_{fi+1}").strip()
                    metric_id_base = re.sub(r"[^a-z0-9_]+", "_", fname.lower()).strip("_") or f"funnel_{fi+1}"
                    metric_id = f"{metric_id_base}_conversion_rate"
                    existing_ids = {m.get("id", "") for m in metrics}
                    if metric_id in existing_ids:
                        suffix = 2
                        while f"{metric_id}_{suffix}" in existing_ids:
                            suffix += 1
                        metric_id = f"{metric_id}_{suffix}"
                    metric_name = f"{fname} Conversion Rate"
                    metrics.append({
                        "id": metric_id,
                        "name": metric_name,
                        "description": f"Conversion from first to last step for funnel '{fname}'.",
                        "sql_hint": preview_sql or "",
                        "aarrr": "Activation",
                        "category": "Activation",
                        "type": "ratio",
                        "status": "candidate",
                        "confidence": "medium",
                        "source": "funnel_builder",
                        "tags": ["funnel"],
                        "validation_reasons": ["Created from curated funnel definition; review SQL if needed."],
                        "builder_definition": {
                            "builder_type": "Conversion funnel",
                            "table": table_sel,
                            "primary_event": f.get("steps", [None])[0],
                            "secondary_event": f.get("steps", [None])[-1],
                            "funnel_steps": f.get("steps", []),
                        },
                    })
                    st.success(f"Added candidate metric '{metric_name}'")
                    st.rerun()
            with create_cols[1]:
                if st.button("🗑 Remove funnel", key=f"funnel_remove_{table_sel}_{fi}"):
                    saved_funnels.pop(fi)
                    st.rerun()

    st.markdown("### ➕ Create New Funnel")
    new_name = st.text_input("Funnel name", key=f"new_funnel_name_{table_sel}")
    new_desc = st.text_input("Funnel description", key=f"new_funnel_desc_{table_sel}")
    flow_key = f"new_funnel_flow_{table_sel}"
    if flow_key not in st.session_state:
        st.session_state[flow_key] = ["", ""]
    flow_steps_state = st.session_state[flow_key]
    if len(flow_steps_state) < 2:
        flow_steps_state.extend([""] * (2 - len(flow_steps_state)))

    st.markdown("**Build your funnel flow (Step 1 -> Step N)**")
    for si in range(len(flow_steps_state)):
        step_col, act_col = st.columns([8, 1])
        with step_col:
            flow_steps_state[si] = st.selectbox(
                f"Step {si + 1}",
                [""] + metric_events,
                index=([""] + metric_events).index(flow_steps_state[si]) if flow_steps_state[si] in metric_events else 0,
                format_func=lambda x: metric_event_labels.get(x, x) if x else "— select event step —",
                key=f"new_funnel_step_{table_sel}_{si}",
            )
        with act_col:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if len(flow_steps_state) > 2 and st.button("✕", key=f"new_funnel_step_del_{table_sel}_{si}"):
                flow_steps_state.pop(si)
                st.rerun()
        if si < len(flow_steps_state) - 1:
            st.markdown("<div style='text-align:center;color:#6366f1;font-size:22px;line-height:1'>↓</div>", unsafe_allow_html=True)

    flow_btn1, flow_btn2, _ = st.columns([1, 1, 4])
    with flow_btn1:
        if st.button("＋ Add step", key=f"new_funnel_add_step_{table_sel}", use_container_width=True):
            flow_steps_state.append("")
            st.rerun()
    with flow_btn2:
        if st.button("Reset flow", key=f"new_funnel_reset_flow_{table_sel}", use_container_width=True):
            st.session_state[flow_key] = ["", ""]
            st.rerun()

    new_steps = [s for s in flow_steps_state if s]
    st.markdown("**Generated SQL preview**")
    new_preview = _funnel_preview_sql(table_sel, user_col, event_col, new_steps)
    if new_preview:
        st.code(new_preview, language="sql")
    else:
        st.info("Pick at least 2 steps to generate SQL preview.")

    if st.button("Save funnel", key=f"save_new_funnel_{table_sel}"):
        if new_name.strip() and len(new_steps) >= 2:
            saved_funnels.append({
                "id": re.sub(r"[^a-z0-9_]+", "_", new_name.lower()).strip("_"),
                "name": new_name.strip(),
                "description": new_desc.strip(),
                "steps": new_steps,
                "status": "approved",
            })
            st.success(f"Saved funnel '{new_name.strip()}'")
            st.session_state[flow_key] = ["", ""]
            st.rerun()
        else:
            st.warning("Please provide a funnel name and at least 2 steps.")

    st.divider()
    st.markdown('</div>', unsafe_allow_html=True)

    # ── Always-Filter Clauses ─────────────────────────────────────────────────
    excl = st.session_state.exclusions
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown("### 🚫 Always-Filter Clauses")
    st.caption("Applied to every query — filters out test/bot users, internal traffic, etc.")

    filters = excl["always_filter"]
    for fi, f in enumerate(filters):
        c1, c2 = st.columns([6, 1])
        with c1:
            filters[fi] = st.text_input(f"f{fi}", value=f, key=f"filter_{fi}", label_visibility="collapsed")
        with c2:
            if st.button("✕", key=f"fdel_{fi}"):
                filters.pop(fi); st.rerun()
    with st.form("add_filter"):
        new_f = st.text_input("New clause", placeholder="platform != 'web'")
        if st.form_submit_button("➕ Add Filter"):
            if new_f:
                filters.append(new_f); st.rerun()

    st.divider()
    st.markdown('</div>', unsafe_allow_html=True)

    # ── SQL Conventions ───────────────────────────────────────────────────────
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown("### ⚙️ SQL Conventions")
    st.caption("Rules for SQL generation — dialect, time windows, formatting.")

    conventions = excl["conventions"]
    for ci, conv in enumerate(conventions):
        c1, c2 = st.columns([6, 1])
        with c1:
            conventions[ci] = st.text_input(f"c{ci}", value=conv, key=f"conv_{ci}", label_visibility="collapsed")
        with c2:
            if st.button("✕", key=f"cdel_{ci}"):
                conventions.pop(ci); st.rerun()
    with st.form("add_convention"):
        new_c = st.text_input("New convention", placeholder="Always cast timestamp to DATE for day-level grouping")
        if st.form_submit_button("➕ Add Convention"):
            if new_c:
                conventions.append(new_c); st.rerun()

    st.divider()
    if st.button("💾 Save Business Context", type="primary", use_container_width=True):
        save_and_regenerate(st.session_state.catalog, catalog_path)
        st.session_state.original_catalog = json.loads(json.dumps(st.session_state.catalog))
        st.success("Saved! catalog.json + catalog.md updated.")
    st.markdown('</div>', unsafe_allow_html=True)
