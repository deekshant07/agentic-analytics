"""
compilers.py — QueryObject → DuckDB SQL.

Takes a fully validated QueryObject (from orchestrator) and generates
deterministic SQL. No LLM involvement — only rule-based template expansion
against the known schema (events + users tables).

Public API:
    compile_query(qo, metrics)            → (sql, metric_name | None)
    compile_custom_event_split(ce_a, ce_b, qo) → sql  (ce_b optional for legacy)
    compile_custom_event_single(ce, qo)   → sql
    compile_custom_event_segment(ce, qo)  → sql
    compile_custom_event_retention(ce, qo)→ sql
    set_global_sql_guards(always_filter)

Return values for compile_query:
    "__diagnose__"  → route to core.diagnose pipeline
    "__analyst__"   → route to core.analyst.investigate() directly
    ""              → could not compile; caller shows clarify message
    <sql string>    → ready to execute
"""
from __future__ import annotations

import re
from typing import Optional

from core.sql.query_object import QueryObject

# ── Global SQL guards ─────────────────────────────────────────────────────────

_SQL_GUARDS: list[str] = []


def set_global_sql_guards(always_filter: list[str]) -> None:
    """Set business-level always-applied filters (e.g., exclude test accounts)."""
    global _SQL_GUARDS
    _SQL_GUARDS = [f for f in (always_filter or []) if isinstance(f, str) and f.strip()]


def _guards_clause(alias: str = "") -> str:
    """
    Append global SQL guard fragments (from catalog always_filter).

    When ``alias`` is set (e.g. ``\"e\"`` for ``JOIN events e``), qualify the
    leading column in each fragment so ``user_id ...`` becomes ``e.user_id ...``
    and DuckDB does not raise "Ambiguous reference" inside joins.
    """
    if not _SQL_GUARDS:
        return ""
    parts: list[str] = []
    for clause in _SQL_GUARDS:
        s = (clause or "").strip()
        if not s:
            continue
        if alias and not re.match(r"^[a-z_][a-z0-9_]*\.", s, re.I):
            s = re.sub(
                r"^([a-z_][a-z0-9_]*)(\s|$)",
                rf"{alias}.\1\2",
                s,
                count=1,
                flags=re.I,
            )
        parts.append(s)
    return "\n  AND " + "\n  AND ".join(parts) if parts else ""


# ── Time window helpers ───────────────────────────────────────────────────────

def _time_filter(qo: QueryObject, alias: str = "") -> str:
    col = f"{alias}." if alias else ""
    if qo.date_from and qo.date_to:
        return (
            f"{col}timestamp >= TIMESTAMP '{qo.date_from}' "
            f"AND {col}timestamp < TIMESTAMP '{qo.date_to}'"
        )
    n = int(qo.time_range_days or 30)
    return f"{col}timestamp >= CURRENT_DATE - INTERVAL '{n}' DAY"


def _cohort_anchor_time_filter(qo: QueryObject, anchor_col: str = "first_ts") -> str:
    """
    Filter cohort rows by each user's true first-event time (after MIN), not by
    restricting events before MIN — avoids re-cohorting long-tenure users.
    """
    if qo.date_from and qo.date_to:
        return (
            f"{anchor_col} >= TIMESTAMP '{qo.date_from}' "
            f"AND {anchor_col} < TIMESTAMP '{qo.date_to}'"
        )
    n = int(qo.time_range_days or 30)
    return f"{anchor_col} >= CURRENT_DATE - INTERVAL '{n}' DAY"


def _time_bounds_clause(date_from: Optional[str], date_to: Optional[str], alias: str = "") -> str:
    """Absolute [date_from, date_to) timestamp predicate, or rolling window fallback."""
    col = f"{alias}." if alias else ""
    if date_from and date_to:
        return (
            f"{col}timestamp >= TIMESTAMP '{date_from}' "
            f"AND {col}timestamp < TIMESTAMP '{date_to}'"
        )
    return f"{col}timestamp >= CURRENT_DATE - INTERVAL '30' DAY"


def _secondary_time_filter(qo: QueryObject, alias: str = "") -> Optional[str]:
    """Second calendar window for cross-period behavioral overlap; None if unset."""
    sf = getattr(qo, "secondary_date_from", None)
    st = getattr(qo, "secondary_date_to", None)
    if sf and st:
        return _time_bounds_clause(sf, st, alias)
    return None


def _filters_clause_excluding(filters: dict, omit_keys: set[str], alias: str = "") -> str:
    if not filters:
        return ""
    omit = {k.lower() for k in omit_keys}
    trimmed = {k: v for k, v in filters.items() if str(k).lower() not in omit}
    return _filters_clause(trimmed, alias)


def _filter_excludes_clause(filter_excludes: dict, alias: str = "") -> str:
    """Generate AND col != 'val' / AND col NOT IN (...) for exclusion filters."""
    if not filter_excludes:
        return ""
    col = f"{alias}." if alias else ""
    parts = []
    for k, v in filter_excludes.items():
        safe_k = re.sub(r"[^a-z0-9_]", "", str(k).lower())
        if isinstance(v, (list, tuple)):
            escaped = ", ".join(f"'{str(i).replace(chr(39), chr(39)*2)}'" for i in v)
            parts.append(f"{col}{safe_k} NOT IN ({escaped})")
        else:
            safe_v = str(v).replace("'", "''")
            parts.append(f"{col}{safe_k} != '{safe_v}'")
    return ("\n  AND " + "\n  AND ".join(parts)) if parts else ""


def _event_filter(qo: QueryObject, alias: str = "") -> str:
    col = f"{alias}." if alias else ""
    event = str(qo.event or "").replace("'", "''")
    return f"{col}event_name = '{event}'"


_FILTER_SKIP_KEYS = frozenset({"date"})  # handled by timestamp bounds; never render as column filter


_IS_NOT_NULL_SENTINEL = "__IS_NOT_NULL__"
_IS_NULL_SENTINEL     = "__IS_NULL__"


def _render_filter_part(col_prefix: str, safe_k: str, v) -> str:
    """Render one filter condition, handling IS NOT NULL / IS NULL sentinels."""
    if isinstance(v, str) and v == _IS_NOT_NULL_SENTINEL:
        return f"{col_prefix}{safe_k} IS NOT NULL"
    if isinstance(v, str) and v == _IS_NULL_SENTINEL:
        return f"{col_prefix}{safe_k} IS NULL"
    if isinstance(v, (list, tuple)):
        escaped = ", ".join(f"'{str(i).replace(chr(39), chr(39)*2)}'" for i in v)
        return f"{col_prefix}{safe_k} IN ({escaped})"
    safe_v = str(v).replace("'", "''")
    return f"{col_prefix}{safe_k} = '{safe_v}'"


def _filters_clause(filters: dict, alias: str = "") -> str:
    if not filters:
        return ""
    col = f"{alias}." if alias else ""
    parts = []
    for k, v in filters.items():
        if str(k).lower() in _FILTER_SKIP_KEYS:
            continue
        safe_k = re.sub(r"[^a-z0-9_]", "", str(k).lower())
        parts.append(_render_filter_part(col, safe_k, v))
    return ("\n  AND " + "\n  AND ".join(parts)) if parts else ""


def _gran(qo: QueryObject) -> str:
    return {"day": "day", "week": "week", "month": "month"}.get(
        str(qo.time_granularity or "day"), "day"
    )


def _safe_col(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(name).lower())


def _custom_event_supplemental_predicates(qo: QueryObject, alias: str = "") -> str:
    """
    Filters, metric_status_*, and filter_excludes from the orchestrator applied
    as WHERE predicates. Used by all custom-event and standard compilers.

    ``alias`` qualifies columns (e.g. ``\"e\"`` in ``JOIN events e``).
    """
    fc = _filters_clause(qo.filters or {}, alias)
    sc = getattr(qo, "metric_status_col", None)
    tgt = getattr(qo, "metric_status_target", None)
    excl = _filter_excludes_clause(getattr(qo, "filter_excludes", None) or {}, alias)
    if sc and tgt:
        key = str(sc).strip().lower()
        filt = qo.filters or {}
        if not any(str(k).strip().lower() == key for k in filt):
            col = _safe_col(sc)
            ap = f"{alias}." if alias else ""
            sv = str(tgt).replace("'", "''")
            fc = fc + f"\n  AND {ap}{col} = '{sv}'"
    return fc + excl


def _qo_filters_clause(qo: QueryObject, alias: str = "") -> str:
    """
    Canonical filter clause for ALL QueryObject-driven compilers.

    Combines qo.filters + metric_status_col/target into a single WHERE fragment.
    Exception: status_rate variant keeps status OUT of WHERE (the rate denominator
    must be all events; the numerator is handled via CASE WHEN in the SELECT).

    Using this function everywhere means:
    - A new field on QueryObject automatically applies to all queries once added here.
    - No compiler function can silently drop the status filter by forgetting to call
      _custom_event_supplemental_predicates.
    """
    if (qo.metric_variant or "") == "status_rate":
        status_key = str(qo.metric_status_col or "").strip().lower()
        omit = {status_key} if status_key else set()
        excl = _filter_excludes_clause(getattr(qo, "filter_excludes", None) or {}, alias)
        return _filters_clause_excluding(qo.filters or {}, omit, alias) + excl
    return _custom_event_supplemental_predicates(qo, alias)


# ── Pre-built metric lookup ───────────────────────────────────────────────────

def _lookup_metric(qo: QueryObject, metrics: list[dict]) -> Optional[dict]:
    if not qo.metric_id:
        return None
    for m in metrics:
        if m.get("id") == qo.metric_id:
            return m
    return None


def _extract_events_where_from_metric_sql(sql: str) -> Optional[str]:
    """
    Pull the events-table WHERE predicate from a catalog ``sql_hint``.

    Used when ``time_granularity`` is week/month so we cannot reuse the
    daily grouped hint verbatim; we re-aggregate with DATE_TRUNC on raw events
    but must keep the same eligibility predicate (e.g. multi-clause DAU).
    """
    if not sql or "where" not in sql.lower():
        return None
    s = sql.strip().rstrip(";")
    for pat in (
        r'FROM\s+"events"\s+WHERE\s+(.+?)\s+GROUP\s+BY',
        r"FROM\s+events\s+WHERE\s+(.+?)\s+GROUP\s+BY",
    ):
        m = re.search(pat, s, re.IGNORECASE | re.DOTALL)
        if m:
            pred = m.group(1).strip()
            return pred if pred else None
    # Scalar pseudo-metrics: SELECT ... FROM events WHERE (...)
    for pat2 in (
        r"FROM\s+events\s+WHERE\s+(\(.+\))\s*$",
        r'FROM\s+"events"\s+WHERE\s+(\(.+\))\s*$',
    ):
        m2 = re.search(pat2, s, re.IGNORECASE | re.DOTALL)
        if m2:
            return m2.group(1).strip()
    # Scalar hints: FROM events WHERE <pred> with no GROUP BY (e.g. activation_rate IN (...)).
    # If multiple matches (subqueries), prefer the **last** FROM events WHERE — outer scope
    # for simple catalog hints is usually last; single-hint metrics have one match.
    best: Optional[str] = None
    for pat3 in (
        r'FROM\s+"events"\s+WHERE\s+(.+)$',
        r"FROM\s+events\s+WHERE\s+(.+)$",
    ):
        for m3 in re.finditer(pat3, s, re.IGNORECASE | re.DOTALL):
            pred = m3.group(1).strip()
            pred = re.split(r"\s+GROUP\s+BY\b", pred, maxsplit=1, flags=re.I)[0].strip()
            pred = re.split(r"\s+ORDER\s+BY\b", pred, maxsplit=1, flags=re.I)[0].strip()
            pred = re.split(r"\s+LIMIT\b", pred, maxsplit=1, flags=re.I)[0].strip()
            if pred:
                best = pred
    if best:
        return best
    return None


def _extract_round_ratio_inner(hint_sql: str) -> Optional[tuple[str, int]]:
    """
    Parse ``SELECT ROUND(<expr>, N) AS ...`` from a catalog scalar ratio hint.

    Returns (expr, scale) for reuse inside ``ROUND(<expr>, N)`` per time bucket.
    """
    if not hint_sql:
        return None
    s = hint_sql.strip()
    m = re.search(r"SELECT\s+ROUND\s*\(\s*", s, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    i = m.end()
    depth = 0
    start = i
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth > 0:
                depth -= 1
            else:
                return None
        elif ch == "," and depth == 0:
            expr = s[start:i].strip()
            tail = s[i + 1 :].lstrip()
            mscale = re.match(r"(\d+)\s*\)", tail)
            if not mscale:
                return None
            return expr, int(mscale.group(1))
        i += 1
    return None


def _strip_leading_sql_comments(sql: str) -> str:
    """Remove leading -- line comments and blank lines from SQL."""
    return re.sub(r"^(\s*--[^\n]*\n)*\s*", "", sql or "")


def _is_cte_sql(hint_sql: str) -> bool:
    """True when the hint is a CTE or standalone SELECT that cannot be flattened."""
    return _strip_leading_sql_comments(hint_sql).upper().startswith("WITH")


def _compile_pct_users_metric_monthly(
    qo: QueryObject, metric: dict, col_label: str = "pct"
) -> Optional[str]:
    """
    Cohort-correct monthly trend for '% of users' funnel metrics (e.g. activation_rate).

    A naive GROUP BY timestamp gives >100% rates because numerator and denominator
    are from different user populations per month.

    Correct: cohort users by the month they completed the denominator event, then
    left-join to the numerator event.

    Window semantics (via qo.retention_window_days):
      0 / None → lifetime: any time after the cohort event
      N > 0    → restricted: conversion within N days of the cohort event

    Required builder_definition fields: primary_event (numerator), secondary_event (denom).
    """
    bd = metric.get("builder_definition") or {}
    primary   = str(bd.get("primary_event")   or "").strip()
    secondary = str(bd.get("secondary_event") or "").strip()
    if not primary or not secondary:
        return None

    g  = _gran(qo)
    tf = _time_filter(qo)
    gc      = _guards_clause()        # for single-table CTEs (denom)
    gc_join = _guards_clause("e")     # for JOIN CTEs — qualifies user_id as e.user_id
    safe_label = re.sub(r"[^a-z0-9_]", "_", col_label.lower()).strip("_") or "pct"

    # Build numerator filter from two sources:
    # 1. builder_definition.filters_structured — static metric constraints (e.g. status=SUCCESS)
    # 2. qo.filters — user-supplied dimension slices (e.g. transaction_channel=UPI)
    # Both apply to the numerator (conversion event) only — denominator stays unfiltered
    # so the cohort base remains "all users who onboarded", not "UPI-onboarded users".
    numer_filter = ""
    for f in (bd.get("filters_structured") or []):
        if not isinstance(f, dict):
            continue
        col = str(f.get("field") or "").strip()
        val = str(f.get("value") or "").strip()
        op  = str(f.get("op") or "=").strip()
        if col and val and op == "=":
            safe_col = re.sub(r"[^a-z0-9_]", "", col.lower())
            safe_val = val.replace("'", "''")
            numer_filter += f"\n  AND {safe_col} = '{safe_val}'"
    # Merge qo.filters (user dimension slices) into the numerator predicate.
    for k, v in (getattr(qo, "filters", None) or {}).items():
        safe_col = re.sub(r"[^a-z0-9_]", "", str(k).lower())
        if not safe_col:
            continue
        numer_filter += f"\n  AND {_render_filter_part('', safe_col, v)}"

    primary_esc   = primary.replace("'", "''")
    secondary_esc = secondary.replace("'", "''")
    denom_label   = re.sub(r"[^a-z0-9_]", "_", secondary.lower())
    numer_label   = re.sub(r"[^a-z0-9_]", "_", primary.lower())

    # activation_window_days=None → default 30-day window.
    # 0 → explicit lifetime (no upper bound, but still after the anchor event).
    # retention_window_days is NOT used here — it's for retention queries.
    win_raw = getattr(qo, "activation_window_days", None)
    win = int(win_raw) if win_raw is not None else 30
    if win > 0:
        # Time-windowed: conversion must happen within N days of the anchor event.
        denom_select = f"user_id, MIN(timestamp) AS first_ts, DATE_TRUNC('{g}', MIN(timestamp))::DATE AS cohort_{g}"
        numer_cte = (
            f"numer_cohort AS (\n"
            f"  SELECT DISTINCT d.user_id\n"
            f"  FROM denom_cohort d\n"
            f"  JOIN events e ON d.user_id = e.user_id\n"
            f'  WHERE e."event_name" = \'{primary_esc}\'{numer_filter}\n'
            f"  AND e.timestamp BETWEEN d.first_ts AND d.first_ts + INTERVAL '{win}' DAY{gc_join}\n"
            f")"
        )
        win_suffix = f"_{win}d"
    else:
        # Lifetime: conversion at any time AFTER the anchor event (no upper bound).
        # Still joins denom_cohort so we enforce temporal ordering — users who did
        # the numerator event before onboarding must not be counted.
        denom_select = f"user_id, MIN(timestamp) AS first_ts, DATE_TRUNC('{g}', MIN(timestamp))::DATE AS cohort_{g}"
        numer_cte = (
            f"numer_cohort AS (\n"
            f"  SELECT DISTINCT d.user_id\n"
            f"  FROM denom_cohort d\n"
            f"  JOIN events e ON d.user_id = e.user_id\n"
            f'  WHERE e."event_name" = \'{primary_esc}\'{numer_filter}\n'
            f"  AND e.timestamp >= d.first_ts{gc_join}\n"
            f")"
        )
        win_suffix = ""

    return f"""WITH denom_cohort AS (
  SELECT {denom_select}
  FROM events
  WHERE "event_name" = '{secondary_esc}' AND {tf}{gc}
  GROUP BY user_id
),
{numer_cte}
SELECT
  d.cohort_{g}                                                                       AS {g},
  COUNT(DISTINCT d.user_id)                                                          AS {denom_label}_users,
  COUNT(DISTINCT n.user_id)                                                          AS {numer_label}_users{win_suffix},
  ROUND(COUNT(DISTINCT n.user_id) * 100.0 / NULLIF(COUNT(DISTINCT d.user_id), 0), 1) AS {safe_label}{win_suffix}
FROM denom_cohort d
LEFT JOIN numer_cohort n ON d.user_id = n.user_id
GROUP BY 1
ORDER BY 1"""


def _compile_pct_users_metric_scalar(
    qo: QueryObject, metric: dict, col_label: str = "pct"
) -> Optional[str]:
    """
    Cohort-correct scalar (single-row) activation rate for '% of users' metrics.

    Unlike the monthly trend variant, this returns one row: denominator count,
    numerator count, and the rate — all respecting qo.filters on the numerator
    (e.g. transaction_channel=UPI) while keeping the denominator unfiltered so
    the base remains "all users who onboarded", not "UPI-onboarded users".
    """
    bd = metric.get("builder_definition") or {}
    primary   = str(bd.get("primary_event")   or "").strip()
    secondary = str(bd.get("secondary_event") or "").strip()
    if not primary or not secondary:
        return None

    tf = _time_filter(qo)
    gc      = _guards_clause()
    gc_join = _guards_clause("e")

    numer_filter = ""
    for f in (bd.get("filters_structured") or []):
        if not isinstance(f, dict):
            continue
        col = str(f.get("field") or "").strip()
        val = str(f.get("value") or "").strip()
        op  = str(f.get("op") or "=").strip()
        if col and val and op == "=":
            safe_col = re.sub(r"[^a-z0-9_]", "", col.lower())
            safe_val = val.replace("'", "''")
            numer_filter += f"\n  AND {safe_col} = '{safe_val}'"
    for k, v in (getattr(qo, "filters", None) or {}).items():
        safe_col = re.sub(r"[^a-z0-9_]", "", str(k).lower())
        if not safe_col:
            continue
        numer_filter += f"\n  AND {_render_filter_part('', safe_col, v)}"

    primary_esc   = primary.replace("'", "''")
    secondary_esc = secondary.replace("'", "''")
    safe_label    = re.sub(r"[^a-z0-9_]", "_", col_label.lower()).strip("_") or "pct"
    denom_label   = re.sub(r"[^a-z0-9_]", "_", secondary.lower())
    numer_label   = re.sub(r"[^a-z0-9_]", "_", primary.lower())

    win_raw = getattr(qo, "activation_window_days", None)
    win = int(win_raw) if win_raw is not None else 30
    if win > 0:
        win_suffix = f"_{win}d"
        numer_cte = (
            f"numer_cohort AS (\n"
            f"  SELECT DISTINCT d.user_id\n"
            f"  FROM denom_cohort d\n"
            f"  JOIN events e ON d.user_id = e.user_id\n"
            f'  WHERE e."event_name" = \'{primary_esc}\'{numer_filter}\n'
            f"  AND e.timestamp BETWEEN d.first_ts AND d.first_ts + INTERVAL '{win}' DAY{gc_join}\n"
            f")"
        )
        denom_select = "user_id, MIN(timestamp) AS first_ts"
    else:
        # Lifetime: after anchor, no upper bound — still enforce temporal ordering.
        win_suffix = ""
        numer_cte = (
            f"numer_cohort AS (\n"
            f"  SELECT DISTINCT d.user_id\n"
            f"  FROM denom_cohort d\n"
            f"  JOIN events e ON d.user_id = e.user_id\n"
            f'  WHERE e."event_name" = \'{primary_esc}\'{numer_filter}\n'
            f"  AND e.timestamp >= d.first_ts{gc_join}\n"
            f")"
        )
        denom_select = "user_id, MIN(timestamp) AS first_ts"

    return f"""WITH denom_cohort AS (
  SELECT {denom_select}
  FROM events
  WHERE "event_name" = '{secondary_esc}' AND {tf}{gc}
  GROUP BY user_id
),
{numer_cte}
SELECT
  COUNT(DISTINCT d.user_id)                                                          AS {denom_label}_users,
  COUNT(DISTINCT n.user_id)                                                          AS {numer_label}_users{win_suffix},
  ROUND(COUNT(DISTINCT n.user_id) * 100.0 / NULLIF(COUNT(DISTINCT d.user_id), 0), 1) AS {safe_label}{win_suffix}
FROM denom_cohort d
LEFT JOIN numer_cohort n ON d.user_id = n.user_id"""


def _compile_ratio_metric_monthly(
    qo: QueryObject, hint_sql: str, col_label: str = "pct"
) -> Optional[str]:
    """Build week/month trend SQL for catalog ratio metrics (ROUND of aggregate expr)."""
    if _is_cte_sql(hint_sql):
        return None
    inner = _extract_round_ratio_inner(hint_sql)
    pred = _extract_events_where_from_metric_sql(hint_sql)
    if not inner or not pred:
        return None
    expr, scale = inner
    g = _gran(qo)
    tf = _time_filter(qo)
    fc = _qo_filters_clause(qo)
    gc = _guards_clause()
    safe_label = re.sub(r"[^a-z0-9_]", "_", col_label.lower()).strip("_") or "pct"
    return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  ROUND(({expr}), {scale}) AS {safe_label}
FROM events
WHERE ({pred})
  AND {tf}{fc}{gc}
GROUP BY 1
ORDER BY 1"""


def _compile_metric_with_predicate(qo: QueryObject, predicate: str) -> str:
    """Like _compile_metric but WHERE uses a full boolean expression (catalog hint)."""
    g = _gran(qo)
    tf = _time_filter(qo)
    fc = _custom_event_supplemental_predicates(qo)
    gc = _guards_clause()

    if qo.metric_variant == "per_user_count":
        value_expr = "COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT user_id), 0)"
        col_alias = "events_per_user"
    elif qo.metric_variant == "per_user_value" and qo.metric_value_col:
        col = _safe_col(qo.metric_value_col)
        value_expr = f"SUM({col}) * 1.0 / NULLIF(COUNT(DISTINCT user_id), 0)"
        col_alias = f"avg_{col}_per_user"
    elif qo.metric_status_col and qo.metric_status_target:
        # Status col is already in WHERE via supplemental predicates.
        value_expr = "COUNT(DISTINCT user_id)"
        col_alias = "unique_users"
    else:
        value_expr = "COUNT(DISTINCT user_id)"
        col_alias = "unique_users"

    return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  {value_expr} AS {col_alias}
FROM events
WHERE ({predicate})
  AND {tf}{fc}{gc}
GROUP BY 1
ORDER BY 1"""


def _compile_segment_with_predicate(qo: QueryObject, predicate: str) -> str:
    """Like _compile_segment but WHERE uses a full boolean expression (catalog hint)."""
    tf = _time_filter(qo)
    fc = _qo_filters_clause(qo)
    gc = _guards_clause()
    bd = _safe_col(qo.breakdown or "platform")

    if qo.metric_variant == "status_rate" and qo.metric_status_col and qo.metric_status_target:
        sc = _safe_col(qo.metric_status_col)
        sv = str(qo.metric_status_target).replace("'", "''")
        value_expr = f"COUNT(DISTINCT CASE WHEN {sc} = '{sv}' THEN user_id END)"
    else:
        value_expr = "COUNT(DISTINCT user_id)"

    return f"""SELECT
  {bd},
  {value_expr} AS unique_users
FROM events
WHERE ({predicate})
  AND {tf}{fc}{gc}
GROUP BY 1
ORDER BY 2 DESC
LIMIT 25"""


def _inject_time(sql_hint: str, qo: QueryObject) -> str:
    """Inject time filter + exclusion filters into a pre-built SQL hint.

    Only filter_excludes is injected — not the full QO filters — to avoid
    corrupting pre-built SQL hints that have their own WHERE structure
    (e.g. status_rate hints that handle metric_status_col in SELECT, not WHERE).
    """
    if not sql_hint:
        return sql_hint
    fc = _filter_excludes_clause(getattr(qo, "filter_excludes", None) or {})
    if "{time_filter}" in sql_hint:
        result = sql_hint.replace("{time_filter}", _time_filter(qo))
        if fc:
            upper = result.upper()
            for kw in (" GROUP BY", " ORDER BY", " LIMIT"):
                if kw in upper:
                    return result[:upper.index(kw)] + fc + result[upper.index(kw):]
            return result + fc
        return result
    upper = sql_hint.upper()
    has_dynamic_time = "CURRENT_DATE" in upper or "INTERVAL" in upper
    if has_dynamic_time and not fc:
        return sql_hint  # nothing to inject

    tf = "" if has_dynamic_time else _time_filter(qo)
    extra = (f"\n  AND {tf}" if tf else "") + fc
    if not extra.strip():
        return sql_hint

    # Find injection point (before GROUP BY / ORDER BY / LIMIT).
    inject_at = len(sql_hint)
    for kw in (" GROUP BY", " ORDER BY", " LIMIT"):
        if kw in upper:
            inject_at = min(inject_at, upper.index(kw))

    has_where = "WHERE" in upper[:inject_at]
    if not has_where:
        return sql_hint[:inject_at] + f"\nWHERE {tf}{fc}" + sql_hint[inject_at:]

    # WHERE already exists. If it contains a top-level OR we must wrap the existing
    # conditions in parentheses so new AND clauses apply to the whole expression.
    where_start = upper.index("WHERE ") + 6  # position of first char after "WHERE "
    where_body = sql_hint[where_start:inject_at].strip()
    if re.search(r'\bOR\b', where_body, re.IGNORECASE):
        # Rewrap: WHERE (original_conditions) AND new_conditions
        return (
            sql_hint[:where_start - 6]
            + f"WHERE ({where_body})"
            + extra
            + sql_hint[inject_at:]
        )
    # Simple case: just append AND clauses before GROUP BY / ORDER BY
    return sql_hint[:inject_at] + extra + sql_hint[inject_at:]


# ── Individual SQL compilers ──────────────────────────────────────────────────

def _compile_metric(qo: QueryObject) -> str:
    if not str(getattr(qo, "event", None) or "").strip():
        return ""
    g  = _gran(qo)
    tf = _time_filter(qo)
    ef = _event_filter(qo)
    gc = _guards_clause()

    # status_rate: numerator = CASE WHEN in SELECT; denominator = all events (no WHERE filter)
    if qo.metric_variant == "status_rate" and qo.metric_status_col and qo.metric_status_target:
        sc = _safe_col(qo.metric_status_col)
        sv = str(qo.metric_status_target).replace("'", "''")
        fc = _qo_filters_clause(qo)   # strips status col from WHERE
        return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  ROUND(
    COUNT(DISTINCT CASE WHEN {sc} = '{sv}' THEN user_id END) * 100.0 /
    NULLIF(COUNT(DISTINCT user_id), 0),
  1) AS {sc}_rate_pct
FROM events
WHERE {ef}
  AND {tf}{fc}{gc}
GROUP BY 1
ORDER BY 1"""

    # All other variants: status lands in WHERE (equality filter, not ratio).
    fc = _custom_event_supplemental_predicates(qo)

    if qo.metric_variant == "threshold_user_count":
        n = int(getattr(qo, "threshold", None) or 1)
        return f"""WITH per_user AS (
  SELECT user_id, COUNT(*) AS event_count
  FROM events
  WHERE {ef}
    AND {tf}{fc}{gc}
  GROUP BY user_id
)
SELECT
  CASE WHEN event_count > {n}
    THEN 'Power Users (>{n}x)'
    ELSE 'Regular (1-{n}x)'
  END AS frequency_segment,
  COUNT(DISTINCT user_id)                                                            AS users,
  ROUND(COUNT(DISTINCT user_id) * 100.0 / SUM(COUNT(DISTINCT user_id)) OVER (), 1) AS pct_of_total
FROM per_user
GROUP BY 1
ORDER BY MIN(event_count) DESC"""

    if qo.metric_variant == "per_user_count":
        value_expr = "COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT user_id), 0)"
        col_alias  = "events_per_user"
    elif qo.metric_variant == "per_user_value" and qo.metric_value_col:
        col        = _safe_col(qo.metric_value_col)
        value_expr = f"SUM({col}) * 1.0 / NULLIF(COUNT(DISTINCT user_id), 0)"
        col_alias  = f"avg_{col}_per_user"
    elif qo.metric_status_col and qo.metric_status_target:
        # Status col is already in WHERE via supplemental predicates;
        # here we also keep the CASE WHEN so the column alias stays "unique_users".
        value_expr = "COUNT(DISTINCT user_id)"
        col_alias  = "unique_users"
    else:
        value_expr = "COUNT(DISTINCT user_id)"
        col_alias  = "unique_users"

    return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  {value_expr} AS {col_alias}
FROM events
WHERE {ef}
  AND {tf}{fc}{gc}
GROUP BY 1
ORDER BY 1"""


def _compile_segment(qo: QueryObject) -> str:
    if not str(getattr(qo, "event", None) or "").strip():
        return ""
    tf = _time_filter(qo)
    ef = _event_filter(qo)
    fc = _qo_filters_clause(qo)
    gc = _guards_clause()
    bd = _safe_col(qo.breakdown or "platform")
    g  = _gran(qo)

    if qo.metric_variant == "status_rate" and qo.metric_status_col and qo.metric_status_target:
        sc         = _safe_col(qo.metric_status_col)
        sv         = str(qo.metric_status_target).replace("'", "''")
        num_expr   = f"COUNT(DISTINCT CASE WHEN {sc} = '{sv}' THEN user_id END)"
        # Breakdown: compute per-group rate (%) + absolute counts so the result is actionable
        if qo.breakdown:
            if g in ("month", "week"):
                return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  {bd},
  ROUND({num_expr} * 100.0 / NULLIF(COUNT(DISTINCT user_id), 0), 1) AS rate_pct,
  {num_expr} AS success_count,
  COUNT(DISTINCT user_id) AS total_count
FROM events
WHERE {ef}
  AND {tf}{fc}{gc}
GROUP BY 1, 2
ORDER BY 1, 3 DESC"""
            return f"""SELECT
  {bd},
  ROUND({num_expr} * 100.0 / NULLIF(COUNT(DISTINCT user_id), 0), 1) AS rate_pct,
  {num_expr} AS success_count,
  COUNT(DISTINCT user_id) AS total_count
FROM events
WHERE {ef}
  AND {tf}{fc}{gc}
GROUP BY 1
ORDER BY 2 DESC
LIMIT 25"""
        value_expr = num_expr
    else:
        value_expr = "COUNT(DISTINCT user_id)"

    if g in ("month", "week"):
        # Time-bucketed segment: (period, breakdown, count) — enables stacked bar charts
        return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  {bd},
  {value_expr} AS unique_users
FROM events
WHERE {ef}
  AND {tf}{fc}{gc}
GROUP BY 1, 2
ORDER BY 1, 3 DESC"""

    return f"""SELECT
  {bd},
  {value_expr} AS unique_users
FROM events
WHERE {ef}
  AND {tf}{fc}{gc}
GROUP BY 1
ORDER BY 2 DESC
LIMIT 25"""


def _compile_funnel(qo: QueryObject) -> str:
    steps = list(qo.funnel_steps or [])
    if len(steps) < 2:
        return ""
    tf = _time_filter(qo)
    fc = _qo_filters_clause(qo)
    gc = _guards_clause()

    ctes = []
    for i, step in enumerate(steps):
        safe = str(step).replace("'", "''")
        if i == 0:
            ctes.append(
                f"  step_{i} AS (\n"
                f"    SELECT DISTINCT user_id FROM events\n"
                f"    WHERE event_name = '{safe}' AND {tf}{fc}{gc}\n"
                f"  )"
            )
        else:
            ctes.append(
                f"  step_{i} AS (\n"
                f"    SELECT DISTINCT e.user_id FROM events e\n"
                f"    JOIN step_{i-1} USING (user_id)\n"
                f"    WHERE e.event_name = '{safe}' AND {tf}{fc}{gc}\n"
                f"  )"
            )

    vals = ",\n".join(
        f"    ({i}, '{str(s).replace(chr(39), chr(39)*2)}', (SELECT COUNT(*) FROM step_{i}))"
        for i, s in enumerate(steps)
    )

    return (
        "WITH\n"
        + ",\n".join(ctes)
        + "\nSELECT\n"
        "  step_num,\n"
        "  step_name,\n"
        "  users,\n"
        "  ROUND(users * 100.0 / NULLIF(FIRST_VALUE(users) OVER (ORDER BY step_num), 0), 1) AS pct_of_top,\n"
        "  ROUND(users * 100.0 / NULLIF(LAG(users) OVER (ORDER BY step_num), 0), 1) AS step_cvr\n"
        "FROM (\n  VALUES\n"
        + vals
        + "\n) t(step_num, step_name, users)\nORDER BY step_num"
    )


def _compile_funnel_compare(qo: QueryObject) -> str:
    """Funnel for current half vs prior half of the window."""
    steps = list(qo.funnel_steps or [])
    if len(steps) < 2:
        return ""
    n    = int(qo.time_range_days or 30)
    half = max(n // 2, 1)
    fc = _qo_filters_clause(qo)
    gc   = _guards_clause()

    def period_ctes(suffix: str, start_days: int, end_days: int) -> list[str]:
        ptf = (
            f"timestamp >= CURRENT_DATE - INTERVAL '{start_days}' DAY "
            f"AND timestamp < CURRENT_DATE - INTERVAL '{end_days}' DAY"
        )
        out = []
        for i, step in enumerate(steps):
            safe = str(step).replace("'", "''")
            alias = f"s{suffix}{i}"
            if i == 0:
                out.append(
                    f"  {alias} AS (\n"
                    f"    SELECT DISTINCT user_id FROM events\n"
                    f"    WHERE event_name = '{safe}' AND {ptf}{fc}{gc}\n"
                    f"  )"
                )
            else:
                prev = f"s{suffix}{i-1}"
                out.append(
                    f"  {alias} AS (\n"
                    f"    SELECT DISTINCT e.user_id FROM events e\n"
                    f"    JOIN {prev} USING (user_id)\n"
                    f"    WHERE e.event_name = '{safe}' AND {ptf}{fc}{gc}\n"
                    f"  )"
                )
        return out

    all_ctes = period_ctes("c", half, 0) + period_ctes("p", n, half)
    vals = ",\n".join(
        f"    ({i}, '{str(s).replace(chr(39), chr(39)*2)}', "
        f"(SELECT COUNT(*) FROM sc{i}), (SELECT COUNT(*) FROM sp{i}))"
        for i, s in enumerate(steps)
    )

    return (
        "WITH\n"
        + ",\n".join(all_ctes)
        + "\nSELECT\n"
        "  step_num,\n  step_name,\n  curr_n,\n  prev_n,\n"
        "  ROUND(curr_n * 100.0 / NULLIF(FIRST_VALUE(curr_n) OVER (ORDER BY step_num), 0), 1) AS curr_pct_top,\n"
        "  ROUND(prev_n * 100.0 / NULLIF(FIRST_VALUE(prev_n) OVER (ORDER BY step_num), 0), 1) AS prev_pct_top\n"
        "FROM (\n  VALUES\n"
        + vals
        + "\n) t(step_num, step_name, curr_n, prev_n)\nORDER BY step_num"
    )


def _retention_cohort_ctes(
    qo: QueryObject,
    *,
    event: str,
    truncate: str,
    fc: str,
    gc: str,
    caf: str,
) -> str:
    """user_first → cohort with true first_ts, then lookback on anchor."""
    cohort_col = f"cohort_{truncate}"
    return f"""user_first AS (
  SELECT user_id, MIN(timestamp) AS first_ts
  FROM events
  WHERE event_name = '{event}'{fc}{gc}
  GROUP BY user_id
),
cohort AS (
  SELECT user_id, DATE_TRUNC('{truncate}', first_ts)::DATE AS {cohort_col}
  FROM user_first
  WHERE {caf}
)"""


def _compile_retention_period_matrix(
    qo: QueryObject,
    *,
    event: str,
    event_b: str,
    fc: str,
    fc_e: str,
    gc: str,
    gc_e: str,
    caf: str,
) -> str:
    days = int(getattr(qo, "time_range_days", None) or 0)
    num_periods = min(6, max(1, days // 30)) if days > 0 else 6
    period_cases = []
    for i in range(1, num_periods + 1):
        lo = (i - 1) * 30 + 1
        hi = i * 30
        period_cases.append(
            f"  COUNT(DISTINCT CASE WHEN ar.day_offset BETWEEN {lo} AND {hi} THEN ar.user_id END) AS m{i}_users,\n"
            f"  ROUND(COUNT(DISTINCT CASE WHEN ar.day_offset BETWEEN {lo} AND {hi} THEN ar.user_id END) * 100.0 / NULLIF(COUNT(DISTINCT c.user_id), 0), 1) AS m{i}_pct"
        )
    period_select = ",\n".join(period_cases)
    cohort_block = _retention_cohort_ctes(
        qo, event=event, truncate="month", fc=fc, gc=gc, caf=caf,
    )
    return f"""WITH {cohort_block},
all_returns AS (
  SELECT c.user_id, c.cohort_month,
    DATEDIFF('day', c.cohort_month::TIMESTAMP, e.timestamp) AS day_offset
  FROM cohort c
  JOIN events e ON c.user_id = e.user_id
  WHERE e.event_name = '{event_b}'{fc_e}
    AND e.timestamp > c.cohort_month::TIMESTAMP{gc_e}
)
SELECT
  c.cohort_month,
  COUNT(DISTINCT c.user_id) AS cohort_size,
{period_select}
FROM cohort c
LEFT JOIN all_returns ar ON c.user_id = ar.user_id
GROUP BY 1
ORDER BY 1"""


def _compile_retention_nday_scalar(
    qo: QueryObject,
    *,
    event: str,
    event_b: str,
    win: int,
    truncate: str,
    cohort_col: str,
    fc: str,
    fc_e: str,
    gc: str,
    gc_e: str,
    caf: str,
) -> str:
    cohort_block = _retention_cohort_ctes(
        qo, event=event, truncate=truncate, fc=fc, gc=gc, caf=caf,
    )
    return f"""WITH {cohort_block},
retained AS (
  SELECT DISTINCT c.user_id, c.{cohort_col}
  FROM cohort c
  JOIN events e ON c.user_id = e.user_id
  WHERE e.event_name = '{event_b}'{fc_e}
    AND e.timestamp >= c.{cohort_col}::TIMESTAMP
    AND e.timestamp <  c.{cohort_col}::TIMESTAMP + INTERVAL '{win}' DAY{gc_e}
)
SELECT
  c.{cohort_col},
  COUNT(DISTINCT c.user_id)  AS cohort_size,
  COUNT(DISTINCT r.user_id)  AS retained_users,
  ROUND(COUNT(DISTINCT r.user_id) * 100.0 / NULLIF(COUNT(DISTINCT c.user_id), 0), 1) AS retention_pct
FROM cohort c
LEFT JOIN retained r ON c.user_id = r.user_id
GROUP BY 1
ORDER BY 1"""


def _compile_retention_breakdown(qo: QueryObject, *, ret_sem) -> str:
    """
    First-N-day retention by a dimension (e.g. platform), one row per cohort × breakdown value.
    Cohort dimension = property on the user's first qualifying event.
    """
    from core.semantic.query_semantics import RetentionTemplate

    bd = _safe_col(getattr(qo, "breakdown", None) or "platform")
    event = str(qo.event or "app_opened").replace("'", "''")
    event_b = str(qo.event_b or qo.event or "app_opened").replace("'", "''")
    win = int(ret_sem.return_window_days)
    fc = _qo_filters_clause(qo)
    fc_e = _qo_filters_clause(qo, "e")
    gc = _guards_clause()
    gc_e = _guards_clause("e")
    caf = _cohort_anchor_time_filter(qo)

    if ret_sem.template == RetentionTemplate.PERIOD_MATRIX:
        truncate = "month"
        cohort_col = "cohort_month"
    elif ret_sem.template == RetentionTemplate.MOM_NDAY:
        truncate = "month"
        cohort_col = "cohort_month"
    else:
        truncate = "week"
        cohort_col = "cohort_week"

    return f"""WITH user_first AS (
  SELECT user_id, MIN(timestamp) AS first_ts
  FROM events
  WHERE event_name = '{event}'{fc}{gc}
  GROUP BY user_id
),
cohort AS (
  SELECT
    uf.user_id,
    DATE_TRUNC('{truncate}', uf.first_ts)::DATE AS {cohort_col},
    e.{bd} AS {bd}
  FROM user_first uf
  INNER JOIN events e
    ON e.user_id = uf.user_id
   AND e.timestamp = uf.first_ts
   AND e.event_name = '{event}'{fc}{gc}
  WHERE {caf}
),
retained AS (
  SELECT DISTINCT c.user_id, c.{cohort_col}, c.{bd}
  FROM cohort c
  JOIN events e ON c.user_id = e.user_id
  WHERE e.event_name = '{event_b}'{fc_e}
    AND e.timestamp >= c.{cohort_col}::TIMESTAMP
    AND e.timestamp <  c.{cohort_col}::TIMESTAMP + INTERVAL '{win}' DAY{gc_e}
)
SELECT
  c.{cohort_col},
  c.{bd},
  COUNT(DISTINCT c.user_id)  AS cohort_size,
  COUNT(DISTINCT r.user_id)  AS retained_users,
  ROUND(COUNT(DISTINCT r.user_id) * 100.0 / NULLIF(COUNT(DISTINCT c.user_id), 0), 1) AS retention_pct
FROM cohort c
LEFT JOIN retained r
  ON c.user_id = r.user_id AND c.{cohort_col} = r.{cohort_col} AND c.{bd} = r.{bd}
GROUP BY 1, 2
ORDER BY 1, 4 DESC"""


def _compile_retention(qo: QueryObject) -> str:
    from core.semantic.query_semantics import (
        RetentionTemplate,
        resolve_retention_semantics,
    )

    sem = getattr(qo, "_query_semantics", None)
    if sem is not None and sem.retention is not None:
        ret_sem = sem.retention
    else:
        ret_sem = resolve_retention_semantics(qo)

    event   = str(qo.event or "app_opened").replace("'", "''")
    event_b = str(qo.event_b or qo.event or "app_opened").replace("'", "''")
    win     = ret_sem.return_window_days
    fc      = _qo_filters_clause(qo)
    fc_e    = _qo_filters_clause(qo, "e")
    gc      = _guards_clause()
    gc_e    = _guards_clause("e")
    caf     = _cohort_anchor_time_filter(qo)

    if getattr(qo, "breakdown", None):
        return _compile_retention_breakdown(qo, ret_sem=ret_sem)

    if ret_sem.template == RetentionTemplate.PERIOD_MATRIX:
        return _compile_retention_period_matrix(
            qo,
            event=event,
            event_b=event_b,
            fc=fc,
            fc_e=fc_e,
            gc=gc,
            gc_e=gc_e,
            caf=caf,
        )

    if ret_sem.template == RetentionTemplate.MOM_NDAY:
        return _compile_retention_nday_scalar(
            qo,
            event=event,
            event_b=event_b,
            win=win,
            truncate="month",
            cohort_col="cohort_month",
            fc=fc,
            fc_e=fc_e,
            gc=gc,
            gc_e=gc_e,
            caf=caf,
        )

    return _compile_retention_nday_scalar(
        qo,
        event=event,
        event_b=event_b,
        win=win,
        truncate="week",
        cohort_col="cohort_week",
        fc=fc,
        fc_e=fc_e,
        gc=gc,
        gc_e=gc_e,
        caf=caf,
    )


def _compile_behavioral_cohort(qo: QueryObject) -> str:
    tf      = _time_filter(qo)
    tf_b    = _secondary_time_filter(qo) or tf
    event   = str(qo.event or "").replace("'", "''")
    event_b = str(qo.event_b or "").replace("'", "''")
    g       = _gran(qo)
    # Behavioral cohort uses an anti-join for event_b — "not B" is structural, not a filter.
    # filter_excludes must be ignored here to avoid injecting invalid column references.
    #
    # Filter scoping: when event_a != event_b, qo.filters qualify event_b (the behaviour
    # being checked), NOT event_a (the cohort definition). Applying e.g. transaction_channel
    # to the onboarding CTE produces zero rows because that column has no value there.
    # When both events are the same, filters apply to both windows identically.
    gc      = _guards_clause()
    _omit = {"date", "event_date", "dt", "day"}
    fc_b = _filters_clause_excluding(qo.filters or {}, _omit) if _secondary_time_filter(qo) else _filters_clause(qo.filters or {})
    # did_a gets filters only when event_a == event_b (same-event two-window pattern)
    fc = fc_b if (event and event == event_b) else ""

    # Descriptive column labels derived from event names so the judge/reader understands the output.
    def _col(name: str, max_len: int = 28) -> str:
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:max_len] or "event"

    a_col = _col(event)
    b_col = _col(event_b) if event_b else "event_b"

    if event_b:
        # Same event, two explicit windows: "March transactors who also transacted in February"
        if event == event_b and _secondary_time_filter(qo):
            df = str(qo.date_from or "").replace("'", "''")
            dt = str(qo.date_to or "").replace("'", "''")
            return f"""WITH cohort_users AS (
  SELECT DISTINCT user_id
  FROM events
  WHERE event_name = '{event}' AND {tf}{fc}{gc}
),
second_window_users AS (
  SELECT DISTINCT user_id
  FROM events
  WHERE event_name = '{event_b}' AND {tf_b}{fc_b}{gc}
),
primary_only AS (
  SELECT c.user_id
  FROM cohort_users c
  LEFT JOIN second_window_users s ON c.user_id = s.user_id
  WHERE s.user_id IS NULL
),
first_onboarding AS (
  SELECT user_id, MIN(timestamp) AS t_onb
  FROM events
  WHERE event_name = 'onboarding_completed'{gc}
  GROUP BY user_id
),
counts AS (
  SELECT
    (SELECT COUNT(*) FROM cohort_users) AS {a_col}_users,
    (
      SELECT COUNT(DISTINCT c.user_id)
      FROM cohort_users c
      INNER JOIN second_window_users s ON c.user_id = s.user_id
    ) AS also_{b_col},
    (SELECT COUNT(*) FROM primary_only) AS not_{b_col},
    (
      SELECT COUNT(DISTINCT m.user_id)
      FROM primary_only m
      INNER JOIN first_onboarding o ON m.user_id = o.user_id
      WHERE o.t_onb >= TIMESTAMP '{df}'
        AND o.t_onb < TIMESTAMP '{dt}'
    ) AS not_{b_col}_first_onboarding_in_window
)
SELECT
  {a_col}_users,
  also_{b_col},
  not_{b_col},
  not_{b_col}_first_onboarding_in_window,
  not_{b_col} - not_{b_col}_first_onboarding_in_window AS not_{b_col}_onboarded_before_window
FROM counts"""
        # Fixed window (date_from + date_to set) → single aggregate count.
        # Rolling window → daily time-series so the trend is visible.
        if qo.date_from and qo.date_to:
            # anti_cohort variant: user said "not/no/without B" — return only the NOT group.
            if (qo.metric_variant or "") == "anti_cohort":
                return f"""WITH did_a AS (
  SELECT DISTINCT user_id
  FROM events
  WHERE event_name = '{event}' AND {tf}{gc}
),
did_b AS (
  SELECT DISTINCT user_id FROM events
  WHERE event_name = '{event_b}' AND {tf_b}{fc_b}{gc}
)
SELECT
  COUNT(DISTINCT da.user_id) AS {a_col}_without_{b_col}
FROM did_a da
LEFT JOIN did_b db ON da.user_id = db.user_id
WHERE db.user_id IS NULL"""
            return f"""WITH did_a AS (
  SELECT DISTINCT user_id
  FROM events
  WHERE event_name = '{event}' AND {tf}{gc}
),
did_b AS (
  SELECT DISTINCT user_id FROM events
  WHERE event_name = '{event_b}' AND {tf_b}{fc_b}{gc}
)
SELECT
  COUNT(DISTINCT da.user_id)                                            AS {a_col}_users,
  COUNT(DISTINCT CASE WHEN db.user_id IS NOT NULL THEN da.user_id END) AS also_{b_col},
  COUNT(DISTINCT CASE WHEN db.user_id IS NULL     THEN da.user_id END) AS not_{b_col}
FROM did_a da
LEFT JOIN did_b db ON da.user_id = db.user_id"""
        return f"""WITH did_a AS (
  SELECT user_id, DATE_TRUNC('{g}', MIN(timestamp))::DATE AS period
  FROM events
  WHERE event_name = '{event}' AND {tf}{fc}{gc}
  GROUP BY user_id
),
did_b AS (
  SELECT DISTINCT user_id FROM events
  WHERE event_name = '{event_b}' AND {tf_b}{fc_b}{gc}
)
SELECT
  da.period,
  COUNT(DISTINCT da.user_id)                                            AS {a_col}_users,
  COUNT(DISTINCT CASE WHEN db.user_id IS NOT NULL THEN da.user_id END) AS also_{b_col},
  COUNT(DISTINCT CASE WHEN db.user_id IS NULL     THEN da.user_id END) AS not_{b_col}
FROM did_a da
LEFT JOIN did_b db ON da.user_id = db.user_id
GROUP BY 1
ORDER BY 1"""
    return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS period,
  COUNT(DISTINCT user_id) AS users
FROM events
WHERE event_name = '{event}' AND {tf}{fc}{gc}
GROUP BY 1 ORDER BY 1"""


def _same_month_anchor_activity_predicate(
    qo: QueryObject, metrics: list[dict]
) -> tuple[str, Optional[dict]]:
    """
    Activity side: either a catalog metric / custom-event SQL predicate, or ``event_name = event``.
    """
    metric = _lookup_metric(qo, metrics)
    if metric and (metric.get("sql") or "").strip():
        pred = _extract_events_where_from_metric_sql(metric["sql"].strip())
        if pred:
            return pred, metric
    if str(qo.event or "").strip():
        return _event_filter(qo), None
    return "", None


def _compile_same_month_anchor(
    qo: QueryObject, metrics: list[dict]
) -> tuple[str, Optional[str]]:
    """
    Users with at least one qualifying **activity** row in the query window: bucket by whether
    calendar month of their first-ever **event_b** equals calendar month of their first
    qualifying activity in that window.

    Activity can be a single ``qo.event`` or the WHERE predicate from a catalog metric / ce_* SQL hint.
    """
    if not qo.event_b:
        return "", None
    pred, metric = _same_month_anchor_activity_predicate(qo, metrics)
    if not pred:
        return "", None
    if metric:
        nm = metric.get("name")
        if nm:
            setattr(qo, "_same_month_activity_label", nm)
    else:
        if hasattr(qo, "_same_month_activity_label"):
            delattr(qo, "_same_month_activity_label")

    tf = _time_filter(qo)
    fc = _qo_filters_clause(qo)
    gc = _guards_clause()
    anc_evt = str(qo.event_b).replace("'", "''")
    pred_sql = pred.strip()
    if not pred_sql.startswith("("):
        pred_sql = f"({pred_sql})"

    sql = f"""WITH act AS (
  SELECT user_id, MIN(timestamp) AS t_act
  FROM events
  WHERE {pred_sql}
    AND {tf}{fc}{gc}
  GROUP BY user_id
),
anc AS (
  SELECT user_id, MIN(timestamp) AS t_anchor
  FROM events
  WHERE event_name = '{anc_evt}'{gc}
  GROUP BY user_id
),
joined AS (
  SELECT
    a.user_id,
    DATE_TRUNC('month', a.t_act)::DATE AS act_month,
    DATE_TRUNC('month', n.t_anchor)::DATE AS anc_month,
    n.t_anchor AS t_anchor
  FROM act a
  LEFT JOIN anc n ON a.user_id = n.user_id
)
SELECT
  CASE
    WHEN t_anchor IS NULL THEN 'No onboarding in data'
    WHEN anc_month = act_month THEN 'Onboarding same month as first activity'
    ELSE 'Onboarding in an earlier month than first activity'
  END AS cohort_month_alignment,
  COUNT(DISTINCT user_id) AS unique_users
FROM joined
GROUP BY 1
ORDER BY 2 DESC"""
    return sql, (metric.get("name") if metric else None)


def _compile_time_between(qo: QueryObject) -> str:
    tf      = _time_filter(qo)
    event   = str(qo.event or "").replace("'", "''")
    event_b = str(qo.event_b or qo.event or "").replace("'", "''")
    fc = _qo_filters_clause(qo)
    gc      = _guards_clause()

    return f"""WITH first_a AS (
  SELECT user_id, MIN(timestamp) AS a_time
  FROM events WHERE event_name = '{event}' AND {tf}{fc}{gc}
  GROUP BY user_id
),
first_b AS (
  SELECT user_id, MIN(timestamp) AS b_time
  FROM events WHERE event_name = '{event_b}' AND {tf}{gc}
  GROUP BY user_id
)
SELECT
  ROUND(MEDIAN(DATEDIFF('hour', fa.a_time, fb.b_time)), 1) AS median_hours,
  ROUND(AVG(DATEDIFF('hour',    fa.a_time, fb.b_time)), 1) AS avg_hours,
  MIN(DATEDIFF('hour',          fa.a_time, fb.b_time))     AS min_hours,
  MAX(DATEDIFF('hour',          fa.a_time, fb.b_time))     AS max_hours,
  COUNT(*) AS user_count
FROM first_a fa
JOIN first_b fb ON fa.user_id = fb.user_id
WHERE fb.b_time > fa.a_time"""


def _compile_journey(qo: QueryObject) -> str:
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc = _qo_filters_clause(qo)
    gc    = _guards_clause()
    gc_e  = _guards_clause("e")

    return f"""WITH anchor AS (
  SELECT user_id, MAX(timestamp) AS anchor_time
  FROM events WHERE event_name = '{event}' AND {tf}{fc}{gc}
  GROUP BY user_id
),
next_ev AS (
  SELECT a.user_id, e.event_name AS next_event,
         ROW_NUMBER() OVER (PARTITION BY a.user_id ORDER BY e.timestamp) AS rn
  FROM anchor a
  JOIN events e ON a.user_id = e.user_id
  WHERE e.timestamp > a.anchor_time
    AND e.timestamp < a.anchor_time + INTERVAL '7' DAY
    AND e.event_name != '{event}'{gc_e}
)
SELECT
  next_event,
  COUNT(DISTINCT user_id) AS users,
  ROUND(COUNT(DISTINCT user_id) * 100.0 / (SELECT COUNT(DISTINCT user_id) FROM anchor), 1) AS pct_of_anchor
FROM next_ev WHERE rn = 1
GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""


def _compile_demographic_breakdown(qo: QueryObject) -> str:
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc = _qo_filters_clause(qo)
    gc    = _guards_clause()

    return f"""SELECT
  platform, age_bucket, city, occupation, income_bucket, acquisition_cohort,
  COUNT(DISTINCT user_id) AS users
FROM events
WHERE event_name = '{event}' AND {tf}{fc}{gc}
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY 7 DESC LIMIT 100"""


def _compile_forecast(qo: QueryObject) -> str:
    g     = _gran(qo)
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc = _qo_filters_clause(qo)
    gc    = _guards_clause()

    return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS period,
  COUNT(DISTINCT user_id)            AS users
FROM events
WHERE event_name = '{event}' AND {tf}{fc}{gc}
GROUP BY 1 ORDER BY 1"""


# ── New analysis type compilers ───────────────────────────────────────────────

def _compile_user_lifecycle(qo: QueryObject) -> str:
    """
    Classify users into lifecycle stages based on recency + frequency.

    Three-step design:
      1. cohort CTE     — identifies the qualifying user set via the time window + filters
                          (e.g. "users who did transaction_reconciled in January")
      2. all_activity   — looks at those users' COMPLETE activity across ANY event_name,
                          so last_seen reflects true app engagement, not just the filtered
                          event. A January transactor who opened the app yesterday is
                          "Engaged", not "Churned".
      3. stage SELECT   — classifies each user by recency (vs CURRENT_DATE) + active_days.

    Uses gc_e (alias-prefixed guard) inside the JOIN to avoid DuckDB ambiguous-column errors.

    Stages: New · Casual · Engaged · Power User · At Risk · Churned
    """
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc = _qo_filters_clause(qo)
    gc    = _guards_clause()
    gc_e  = _guards_clause("e")   # alias-prefixed for JOIN context

    return f"""WITH cohort AS (
  -- Step 1: identify the qualifying user cohort (time window + event + filters)
  SELECT DISTINCT user_id
  FROM events
  WHERE event_name = '{event}'
    AND {tf}{fc}{gc}
),
all_activity AS (
  -- Step 2: full activity history across ANY event for lifecycle stage classification.
  --         No event_name filter — recency/frequency reflect overall app engagement.
  SELECT
    e.user_id,
    MIN(e.timestamp)                              AS first_seen,
    MAX(e.timestamp)                              AS last_seen,
    COUNT(DISTINCT DATE(e.timestamp))             AS active_days
  FROM events e
  JOIN cohort c ON e.user_id = c.user_id{gc_e}
  GROUP BY e.user_id
),
ref AS (
  -- Step 3: use the latest event date in the warehouse as the reference point.
  --         Avoids 100%-Churned when the database lags the wall-clock date by weeks/months.
  SELECT MAX(timestamp)::DATE AS ref_date FROM events
)
SELECT
  stage                                                                             AS lifecycle_stage,
  COUNT(DISTINCT user_id)                                                           AS users,
  ROUND(COUNT(DISTINCT user_id) * 100.0 / SUM(COUNT(DISTINCT user_id)) OVER (), 1) AS pct_of_total
FROM (
  SELECT
    a.user_id,
    CASE
      WHEN DATEDIFF('day', a.last_seen,  r.ref_date) > 30
        THEN '6. Churned (>30d inactive)'
      WHEN DATEDIFF('day', a.last_seen,  r.ref_date) BETWEEN 15 AND 30
        THEN '5. At Risk (15-30d inactive)'
      WHEN DATEDIFF('day', a.first_seen, r.ref_date) <= 7
        THEN '1. New (<7d since first action)'
      WHEN a.active_days >= 15
        THEN '4. Power User (15+ active days)'
      WHEN a.active_days >= 5
        THEN '3. Engaged (5-14 active days)'
      ELSE '2. Casual (1-4 active days)'
    END AS stage
  FROM all_activity a
  CROSS JOIN ref r
) t
GROUP BY 1
ORDER BY 1"""


def _compile_stickiness(qo: QueryObject) -> str:
    """
    Compute DAU/MAU (and WAU/MAU) stickiness ratios over time.
    Returns: day, dau, wau, mau, dau_mau_ratio, wau_mau_ratio.
    """
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc = _qo_filters_clause(qo)
    gc    = _guards_clause()

    return f"""WITH base AS (
  SELECT timestamp, user_id
  FROM events
  WHERE event_name = '{event}'
    AND {tf}{fc}{gc}
),
daily AS (
  SELECT DATE(timestamp)::DATE AS d, COUNT(DISTINCT user_id) AS dau
  FROM base GROUP BY 1
),
weekly AS (
  SELECT DATE_TRUNC('week', timestamp)::DATE AS w, COUNT(DISTINCT user_id) AS wau
  FROM base GROUP BY 1
),
monthly AS (
  SELECT DATE_TRUNC('month', timestamp)::DATE AS m, COUNT(DISTINCT user_id) AS mau
  FROM base GROUP BY 1
)
SELECT
  d.d                                                                           AS day,
  d.dau,
  w.wau,
  mo.mau,
  ROUND(d.dau  * 100.0 / NULLIF(mo.mau, 0), 1)                                AS dau_mau_pct,
  ROUND(w.wau  * 100.0 / NULLIF(mo.mau, 0), 1)                                AS wau_mau_pct
FROM daily d
JOIN weekly  w  ON DATE_TRUNC('week',  d.d)::DATE = w.w
JOIN monthly mo ON DATE_TRUNC('month', d.d)::DATE = mo.m
ORDER BY 1"""


def _compile_funnel_property_drilldown(qo: QueryObject) -> str:
    """
    For the first two funnel steps, break down who converts vs drops by a
    dimension column (qo.breakdown). Shows converters/total and CVR per value.
    """
    steps = list(qo.funnel_steps or [])
    if len(steps) < 2:
        return ""
    tf  = _time_filter(qo)
    fc = _qo_filters_clause(qo)
    gc  = _guards_clause()
    gc_e = _guards_clause("e")
    bd  = _safe_col(qo.breakdown or "platform")
    s0  = str(steps[0]).replace("'", "''")
    s1  = str(steps[1]).replace("'", "''")

    return f"""WITH step0_users AS (
  SELECT DISTINCT user_id
  FROM events
  WHERE event_name = '{s0}' AND {tf}{fc}{gc}
),
step1_users AS (
  SELECT DISTINCT e.user_id
  FROM events e
  JOIN step0_users USING (user_id)
  WHERE e.event_name = '{s1}' AND {tf}{gc_e}
),
step0_with_dim AS (
  SELECT DISTINCT e.user_id, e.{bd} AS dim_val
  FROM events e
  JOIN step0_users s ON e.user_id = s.user_id
  WHERE e.event_name = '{s0}' AND {tf}{fc}{gc_e}
)
SELECT
  dim_val                                                                           AS {bd},
  COUNT(DISTINCT s0.user_id)                                                        AS entered,
  COUNT(DISTINCT s1.user_id)                                                        AS converted,
  COUNT(DISTINCT s0.user_id) - COUNT(DISTINCT s1.user_id)                           AS dropped,
  ROUND(COUNT(DISTINCT s1.user_id) * 100.0 / NULLIF(COUNT(DISTINCT s0.user_id), 0), 1) AS cvr_pct
FROM step0_with_dim s0
LEFT JOIN step1_users s1 ON s0.user_id = s1.user_id
WHERE dim_val IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC
LIMIT 20"""


def _compile_xyz_matrix(qo: QueryObject) -> str:
    """
    3-axis cohort matrix: cohort_month × axis1_dimension × metric.
    Produces: cohort_month, <axis1>, users, avg_events_per_user.
    Pivot in Python for display.
    """
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc = _qo_filters_clause(qo)
    gc    = _guards_clause()
    gc_e  = _guards_clause("e")
    axis1 = _safe_col(qo.xyz_axis1 or qo.breakdown or "platform")

    return f"""WITH cohort_base AS (
  SELECT
    user_id,
    DATE_TRUNC('month', MIN(timestamp))::DATE AS cohort_month
  FROM events
  WHERE event_name = '{event}' AND {tf}{fc}{gc}
  GROUP BY user_id
),
activity AS (
  SELECT
    cb.user_id,
    cb.cohort_month,
    e.{axis1}                                           AS dim1,
    COUNT(*)                                            AS event_count
  FROM cohort_base cb
  JOIN events e ON cb.user_id = e.user_id
  WHERE e.event_name = '{event}'{gc_e}
  GROUP BY 1, 2, 3
)
SELECT
  cohort_month,
  dim1                                                  AS {axis1},
  COUNT(DISTINCT user_id)                               AS users,
  ROUND(AVG(event_count), 1)                            AS avg_events_per_user,
  SUM(event_count)                                      AS total_events
FROM activity
WHERE dim1 IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 3 DESC"""


# ── Custom event helpers ──────────────────────────────────────────────────────

def _ce_condition(inner_sql: str) -> str:
    """Extract the WHERE condition from a custom event SQL string."""
    s = (inner_sql or "").strip().rstrip(";")
    if not s:
        return "1=0"
    upper = s.upper()
    if "WHERE" in upper:
        idx = upper.index("WHERE")
        cond = s[idx + 5 :].strip()
        for kw in (" GROUP BY", " ORDER BY", " LIMIT", " HAVING"):
            if kw in cond.upper():
                cond = cond[: cond.upper().index(kw)]
        return cond.strip()
    # Catalog ``custom_events.sql`` is usually a parenthesised row predicate on ``events``.
    if s.startswith("("):
        return s
    # Full SELECT or fragment — wrap as membership subquery against events.
    return f"user_id IN (SELECT user_id FROM events _ce_inner WHERE {s})"


def compile_custom_event_single(ce: dict, qo: QueryObject) -> str:
    g         = _gran(qo)
    tf        = _time_filter(qo)
    gc        = _guards_clause()
    extra     = _custom_event_supplemental_predicates(qo)
    condition = _ce_condition((ce.get("sql") or "").strip().rstrip(";"))

    return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  COUNT(DISTINCT user_id)            AS unique_users
FROM events
WHERE ({condition}) AND {tf}{extra}{gc}
GROUP BY 1 ORDER BY 1"""


def _custom_split_sql_alias(ce: dict, fallback: str) -> str:
    raw = (ce.get("name") or fallback or "cohort").strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_") or "cohort"
    return s[:48]


def compile_custom_event_split(
    ce_a: dict,
    ce_b: Optional[dict],
    qo: QueryObject,
) -> str:
    """
    Compare one or two custom-event cohorts.

    * **Week / month** ``time_granularity``: one row per calendar bucket, two
      columns = distinct users matching each cohort's definition in that bucket.
    * **Day** + two cohorts: first vs second half of ``time_range_days``, two
      counts per half (four scalar subqueries).
    * **Day** + one cohort (``ce_b`` is None): legacy single-metric half-window
      comparison (two rows: current / previous).
    """
    gc = _guards_clause()
    cond_a = _ce_condition((ce_a.get("sql") or "").strip().rstrip(";"))
    cond_b = (
        _ce_condition((ce_b.get("sql") or "").strip().rstrip(";"))
        if ce_b
        else cond_a
    )

    gran = str(qo.time_granularity or "day").lower()
    tf = _time_filter(qo)
    extra = _custom_event_supplemental_predicates(qo)

    if gran in ("week", "month"):
        g = _gran(qo)
        alias_a = _custom_split_sql_alias(ce_a, "cohort_a")
        alias_b = _custom_split_sql_alias(ce_b or ce_a, "cohort_b")
        return f"""SELECT
  DATE_TRUNC('{g}', timestamp)::DATE AS {g},
  COUNT(DISTINCT CASE WHEN ({cond_a}) THEN user_id END) AS {alias_a}_users,
  COUNT(DISTINCT CASE WHEN ({cond_b}) THEN user_id END) AS {alias_b}_users
FROM events
WHERE (({cond_a}) OR ({cond_b}))
  AND {tf}{extra}{gc}
GROUP BY 1
ORDER BY 1"""

    n = int(qo.time_range_days or 30)
    half = max(n // 2, 1)
    if ce_b is None:
        return f"""WITH curr AS (
  SELECT COUNT(DISTINCT user_id) AS users FROM events
  WHERE ({cond_a})
    AND timestamp >= CURRENT_DATE - INTERVAL '{half}' DAY{extra}{gc}
),
prev AS (
  SELECT COUNT(DISTINCT user_id) AS users FROM events
  WHERE ({cond_a})
    AND timestamp >= CURRENT_DATE - INTERVAL '{n}' DAY
    AND timestamp <  CURRENT_DATE - INTERVAL '{half}' DAY{extra}{gc}
)
SELECT 'current'  AS period, (SELECT users FROM curr) AS users
UNION ALL
SELECT 'previous' AS period, (SELECT users FROM prev) AS users"""

    alias_a = _custom_split_sql_alias(ce_a, "cohort_a")
    alias_b = _custom_split_sql_alias(ce_b, "cohort_b")
    return f"""WITH ca_curr AS (
  SELECT COUNT(DISTINCT user_id) AS u FROM events
  WHERE ({cond_a}) AND timestamp >= CURRENT_DATE - INTERVAL '{half}' DAY{extra}{gc}
), cb_curr AS (
  SELECT COUNT(DISTINCT user_id) AS u FROM events
  WHERE ({cond_b}) AND timestamp >= CURRENT_DATE - INTERVAL '{half}' DAY{extra}{gc}
), ca_prev AS (
  SELECT COUNT(DISTINCT user_id) AS u FROM events
  WHERE ({cond_a})
    AND timestamp >= CURRENT_DATE - INTERVAL '{n}' DAY
    AND timestamp <  CURRENT_DATE - INTERVAL '{half}' DAY{extra}{gc}
), cb_prev AS (
  SELECT COUNT(DISTINCT user_id) AS u FROM events
  WHERE ({cond_b})
    AND timestamp >= CURRENT_DATE - INTERVAL '{n}' DAY
    AND timestamp <  CURRENT_DATE - INTERVAL '{half}' DAY{extra}{gc}
)
SELECT 'current' AS period,
  (SELECT u FROM ca_curr) AS {alias_a}_users,
  (SELECT u FROM cb_curr) AS {alias_b}_users
UNION ALL
SELECT 'previous' AS period,
  (SELECT u FROM ca_prev) AS {alias_a}_users,
  (SELECT u FROM cb_prev) AS {alias_b}_users"""


def compile_custom_event_segment(ce: dict, qo: QueryObject) -> str:
    tf        = _time_filter(qo)
    gc        = _guards_clause()
    extra     = _custom_event_supplemental_predicates(qo)
    bd        = _safe_col(qo.breakdown or "platform")
    condition = _ce_condition((ce.get("sql") or "").strip().rstrip(";"))

    return f"""SELECT
  {bd},
  COUNT(DISTINCT user_id) AS unique_users
FROM events
WHERE ({condition}) AND {tf}{extra}{gc}
GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""


def compile_custom_event_retention(ce: dict, qo: QueryObject) -> str:
    gc        = _guards_clause()
    gc_e      = _guards_clause("e")
    extra     = _custom_event_supplemental_predicates(qo)
    extra_e   = _custom_event_supplemental_predicates(qo, "e")
    win       = int(qo.retention_window_days or 7)
    condition = _ce_condition((ce.get("sql") or "").strip().rstrip(";"))
    caf       = _cohort_anchor_time_filter(qo)

    return f"""WITH user_first AS (
  SELECT user_id, MIN(timestamp) AS first_ts
  FROM events WHERE ({condition}){extra}{gc}
  GROUP BY user_id
),
cohort AS (
  SELECT user_id, DATE_TRUNC('week', first_ts)::DATE AS cohort_week
  FROM user_first
  WHERE {caf}
),
retained AS (
  SELECT DISTINCT c.user_id, c.cohort_week
  FROM cohort c JOIN events e ON c.user_id = e.user_id
  WHERE ({condition}){extra_e}
    AND e.timestamp >= c.cohort_week::TIMESTAMP
    AND e.timestamp <  c.cohort_week::TIMESTAMP + INTERVAL '{win}' DAY{gc_e}
)
SELECT
  c.cohort_week,
  COUNT(DISTINCT c.user_id) AS cohort_size,
  COUNT(DISTINCT r.user_id) AS retained_users,
  ROUND(COUNT(DISTINCT r.user_id) * 100.0 / NULLIF(COUNT(DISTINCT c.user_id), 0), 1) AS retention_pct
FROM cohort c LEFT JOIN retained r ON c.user_id = r.user_id
GROUP BY 1 ORDER BY 1"""


# ── Main entry point ──────────────────────────────────────────────────────────

# Analysis types where the analyst plans and runs its own queries.
_ANALYST_ONLY = frozenset({
    "forecast", "demographic_breakdown", "journey", "retention",
    "time_between", "funnel", "funnel_compare",
    "user_lifecycle", "stickiness", "funnel_property_drilldown", "xyz_matrix",
})


def compile_query(qo: QueryObject, metrics: list[dict]) -> tuple[str, Optional[str]]:
    """
    Compile a QueryObject into a DuckDB SQL string.

    Returns (sql, metric_name):
        sql == "__diagnose__"  → route to core.diagnose
        sql == "__analyst__"   → route to core.analyst.investigate()
        sql == ""              → could not compile
        otherwise              → ready to run
    """
    at = qo.analysis_type

    if at == "diagnose":
        return "__diagnose__", None

    if at == "same_month_anchor":
        sql, mname = _compile_same_month_anchor(qo, metrics)
        return (sql, mname) if sql else ("", None)

    if at == "behavioral_cohort":
        if not qo.event:
            return "__analyst__", None
        return _compile_behavioral_cohort(qo), None

    if at in _ANALYST_ONLY:
        return "__analyst__", None

    # Pre-built metric SQL hint
    metric = _lookup_metric(qo, metrics)

    # '% of users' cohort metrics: use the builder_definition compiler regardless of
    # whether metric["sql"] is populated. The cohort path never uses metric["sql"].
    # Also handles at=="segment" — when the LLM picks segment+breakdown for an activation
    # metric (e.g. "D7 activation by cohort month"), route to the same monthly compiler.
    if metric and at in ("metric", "segment"):
        bd_type = str((metric.get("builder_definition") or {}).get("builder_type") or "").lower()
        if "% of users" in bd_type or "pct of users" in bd_type:
            _metric_label = re.sub(r"[^a-z0-9]+", "_", (metric.get("name") or "pct").lower()).strip("_") or "pct"
            gran_override = str(qo.time_granularity or "day") != "day"
            if gran_override:
                cohort_sql = _compile_pct_users_metric_monthly(qo, metric, col_label=_metric_label)
            else:
                cohort_sql = _compile_pct_users_metric_scalar(qo, metric, col_label=_metric_label)
            if cohort_sql:
                return cohort_sql, metric.get("name")

    if metric and metric.get("sql"):
        # Segment + breakdown: don't use the raw scalar metric hint — it ignores breakdown.
        # Ratio metrics (ROUND(a/b)) and CTE metrics must go to __analyst__ since a simple
        # GROUP BY query cannot compute per-group rates correctly.
        # For simple event-count metrics, extract the WHERE predicate and build a breakdown.
        if at == "segment" and qo.breakdown:
            hint_raw = metric.get("sql") or ""
            is_ratio = _extract_round_ratio_inner(hint_raw) is not None
            pred = (
                None
                if _is_cte_sql(hint_raw) or is_ratio
                else _extract_events_where_from_metric_sql(hint_raw)
            )
            if pred:
                return _compile_segment_with_predicate(qo, pred), metric.get("name")
            return "__analyst__", metric.get("name")

        # Daily (and default): inject time range into the catalog hint.
        gran_override = str(qo.time_granularity or "day") != "day"
        if not gran_override:
            return _inject_time(metric["sql"], qo), metric.get("name")
        # Week/month: scalar hints need per-bucket logic.
        if at == "metric":
            _metric_label = re.sub(r"[^a-z0-9]+", "_", (metric.get("name") or "pct").lower()).strip("_") or "pct"
            ratio_sql = _compile_ratio_metric_monthly(qo, metric["sql"], col_label=_metric_label)
            if ratio_sql:
                return ratio_sql, metric.get("name")
        # Re-build trend SQL from the hint's WHERE predicate for simple distinct-user metrics.
        hint_raw = metric.get("sql") or ""
        pred = (
            None
            if _is_cte_sql(hint_raw)
            else _extract_events_where_from_metric_sql(hint_raw)
        )
        if pred:
            if at == "metric":
                return _compile_metric_with_predicate(qo, pred), metric.get("name")
            if at == "segment":
                return _compile_segment_with_predicate(qo, pred), metric.get("name")
        # Week/month + catalog hint: cannot flatten to COUNT templates — avoid invalid SQL
        # (e.g. event_name = '' when event slot is empty).
        return "__analyst__", metric.get("name")

    if at == "metric":
        if not qo.event and not qo.metric_id:
            return "", None
        # When a breakdown is requested on an event-based metric (no catalog sql_hint),
        # compile as a segment so the breakdown column appears in results.
        if qo.breakdown and not qo.metric_id:
            sql = _compile_segment(qo)
            return (sql, None) if sql.strip() else ("", None)
        sql = _compile_metric(qo)
        return (sql, None) if sql.strip() else ("", None)

    if at == "segment":
        if not qo.event and not qo.metric_id:
            return "", None
        sql = _compile_segment(qo)
        return (sql, None) if sql.strip() else ("", None)

    # Fallback: let investigate() handle it
    return "__analyst__", None
