"""
analyst.py — Multi-investigation engine + narrative synthesis.

For analysis types that need more than one SQL query (funnel, retention,
journey, forecast, demographic_breakdown, etc.), investigate() plans a set
of investigations, executes them against DuckDB, then calls build_story_arc()
to synthesise a structured narrative.

Public API:
    investigate(question, qo, db_path, openai_key, catalog, ...)
        → AnalystReport
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import duckdb
import pandas as pd

from core.sql.query_object import QueryObject
from core.infra.logger import log_sql
from core.infra.tracer import track


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Investigation:
    name: str
    purpose: str
    sql: str = ""
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    insight: str = ""
    error: str = ""


@dataclass
class AnalystReport:
    analysis_type: str
    investigations: list[Investigation] = field(default_factory=list)
    narrative: str = ""
    executive_summary: str = ""
    next_steps: list[str] = field(default_factory=list)
    beats: list = field(default_factory=list)   # NarrativeBeat list: context/tension/resolution
    confidence_label: str = ""
    hypothesis_verdict: str = ""
    data_quality_blocked: bool = False


# ── SQL execution ─────────────────────────────────────────────────────────────

def _run_sql(sql: str, db_path: str) -> pd.DataFrame:
    conn = duckdb.connect(db_path, read_only=True)
    try:
        return conn.execute(sql).df()
    finally:
        conn.close()


def _exec_investigation(inv: Investigation, db_path: str) -> None:
    """Run inv.sql, populate inv.df or inv.error, and log timing."""
    if not inv.sql:
        inv.error = "no SQL generated"
        return
    t0 = time.perf_counter()
    try:
        inv.df = _run_sql(inv.sql, db_path)
        log_sql(
            investigation_name=inv.name,
            latency_ms=(time.perf_counter() - t0) * 1000,
            row_count=len(inv.df),
        )
    except Exception as exc:
        inv.error = str(exc)
        log_sql(
            investigation_name=inv.name,
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=inv.error,
        )


# ── Rule-based insight generator ─────────────────────────────────────────────

def _auto_insight(inv: Investigation, qo: QueryObject) -> str:
    """Generate a one-line insight from the dataframe without an LLM call."""
    df = inv.df
    if df is None or df.empty:
        return ""

    at = qo.analysis_type

    try:
        if at == "user_lifecycle" and "lifecycle_stage" in df.columns and "users" in df.columns:
            top = df.iloc[0]
            churned = df[df["lifecycle_stage"].str.startswith("6.")]["users"].sum() if "lifecycle_stage" in df.columns else 0
            total   = df["users"].sum()
            churn_pct = round(churned * 100 / total, 1) if total else 0
            return (
                f"Largest stage: {top['lifecycle_stage']} ({int(top['users']):,} users). "
                f"Churned/at-risk: {churn_pct:.1f}% of all users."
            )

        if at == "stickiness" and "dau_mau_pct" in df.columns:
            avg_sticky = float(df["dau_mau_pct"].mean())
            return f"Average DAU/MAU stickiness: {avg_sticky:.1f}% (industry benchmark: 20-25%)."

        if at == "funnel_property_drilldown" and "cvr_pct" in df.columns:
            best = df.loc[df["cvr_pct"].idxmax()]
            worst = df.loc[df["cvr_pct"].idxmin()]
            return (
                f"Best converting segment: {best.iloc[0]} ({best['cvr_pct']:.1f}%). "
                f"Worst: {worst.iloc[0]} ({worst['cvr_pct']:.1f}%)."
            )

        if at == "xyz_matrix" and "users" in df.columns:
            total = int(df["users"].sum())
            return f"XYZ matrix covers {total:,} total user-cohort-segment combinations."

        if at == "funnel" and "step_name" in df.columns and "users" in df.columns:
            top = int(df["users"].iloc[0]) if len(df) > 0 else 0
            if "step_cvr" in df.columns:
                worst_idx = df["step_cvr"].idxmin()
                worst_step = df.loc[worst_idx, "step_name"]
                worst_cvr  = df.loc[worst_idx, "step_cvr"]
                return (
                    f"Biggest drop-off at '{worst_step}' "
                    f"({worst_cvr:.0f}% step conversion, {top:,} users at top)."
                )
            return f"{top:,} users entered the funnel."

        if at == "funnel_compare" and "step_name" in df.columns:
            if "curr_pct_top" in df.columns and len(df) > 1:
                last = df.iloc[-1]
                return (
                    f"Bottom-of-funnel: {last['curr_n']:,} users current "
                    f"vs {last['prev_n']:,} prior ({last['curr_pct_top']:.1f}% vs "
                    f"{last['prev_pct_top']:.1f}% of top)."
                )

        if at == "retention" and "retention_pct" in df.columns:
            from datetime import date, timedelta
            import calendar as _cal
            win = int(getattr(qo, "retention_window_days", None) or 7)
            time_col = next((c for c in df.columns if "cohort" in c.lower()), None)
            if time_col:
                d = df.copy()
                d["_cohort_dt"] = pd.to_datetime(d[time_col], errors="coerce")
                today = date.today()
                def _mature(x):
                    if pd.isna(x):
                        return True
                    try:
                        y, m = x.year, x.month
                        last_day = date(y, m, _cal.monthrange(y, m)[1])
                        return (last_day + timedelta(days=win)) <= today
                    except Exception:
                        return True
                d["_mature"] = d["_cohort_dt"].apply(_mature)
                mature_df = d[d["_mature"]]
                n_immature = int((~d["_mature"]).sum())
                if not mature_df.empty:
                    avg_ret = mature_df["retention_pct"].mean()
                    note = f"; {n_immature} cohort(s) pending — window not yet complete" if n_immature else ""
                    return f"Average D{win} retention (mature cohorts): {avg_ret:.1f}%{note}."
            avg_ret = df["retention_pct"].mean()
            return f"Average D{win} retention: {avg_ret:.1f}%."

        if at == "behavioral_cohort" and "never_did_b" in df.columns:
            if (
                len(df) == 1
                and "also_did_b" in df.columns
                and getattr(qo, "event", None)
                and str(qo.event) == str(getattr(qo, "event_b", None) or "")
                and getattr(qo, "secondary_date_from", None)
            ):
                row = df.iloc[0]
                a = int(row.get("did_a_users", 0) or 0)
                also = int(row.get("also_did_b", 0) or 0)
                only = int(row.get("never_did_b", 0) or 0)
                pct = round(also * 100 / a, 1) if a else 0.0
                base = (
                    f"{a:,} users in the primary window; {also:,} ({pct}%) also appear in the second window; "
                    f"{only:,} only in the primary window."
                )
                n_new = row.get("never_b_first_onboarding_in_primary_month")
                n_rest = row.get("never_b_onboarded_before_primary_or_missing")
                if n_new is not None and n_rest is not None and only > 0:
                    nn = int(n_new or 0)
                    nr = int(n_rest or 0)
                    pn = round(nn * 100.0 / only, 1) if only else 0.0
                    return (
                        f"{base} Of those {only:,} with no activity in the second window, "
                        f"{nn:,} ({pn}%) had their **first onboarding_completed** in the primary month "
                        f"(short tenure — they may not have been active in the second window at all); "
                        f"{nr:,} had first onboarding before that month or no onboarding row in data "
                        f"(they could have transacted in the second window but did not)."
                    )
                return base
            total_a = int(df["did_a_users"].sum()) if "did_a_users" in df.columns else 0
            total_b = int(df["never_did_b"].sum())
            pct = round(total_b * 100 / total_a, 1) if total_a else 0
            return f"{total_b:,} users ({pct}%) did the primary event but never converted."

        if at == "time_between" and "median_hours" in df.columns:
            med = float(df["median_hours"].iloc[0])
            days = med / 24
            if days >= 1:
                return f"Median time: {days:.1f} days ({med:.0f} hours)."
            return f"Median time: {med:.1f} hours."

        if at == "journey" and "next_event" in df.columns and "users" in df.columns:
            top = df.iloc[0]
            pct = top.get("pct_of_anchor", "")
            pct_str = f" ({pct:.0f}% of anchor users)" if pct else ""
            return f"Most common next event: '{top['next_event']}'{pct_str}."

        if at == "forecast" and "users" in df.columns:
            last_val = int(df["users"].iloc[-1])
            first_val = int(df["users"].iloc[0])
            direction = "up" if last_val > first_val else "down"
            return f"Recent trend: {direction} from {first_val:,} to {last_val:,}."

        # Generic: highlight top row for segmented data
        num_cols = df.select_dtypes("number").columns.tolist()
        cat_cols = df.select_dtypes("object").columns.tolist()
        if cat_cols and num_cols:
            top_label = str(df[cat_cols[0]].iloc[0])
            top_val   = df[num_cols[0]].iloc[0]
            return f"Largest segment: {top_label} ({int(top_val):,})."

    except Exception:
        pass

    return ""


# ── Investigation planner ─────────────────────────────────────────────────────

_DIM_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*")
_TOP_N_RE = re.compile(r"\btop\s*([3-4])\b", re.IGNORECASE)
_BREAKDOWN_HINT_RE = re.compile(
    r"\b(break[\s-]?down|broken\s+down|split|segment|segmentation|by\s+dimension|by\s+dim)\b",
    re.IGNORECASE,
)
_USER_DIM_SKIP = {
    "user_id", "session_id", "event_id", "transaction_id", "device_id",
    "email", "phone", "name", "first_name", "last_name",
    "timestamp", "created_at", "updated_at", "date", "dt",
}
_DIM_PRIORITY = [
    "platform",
    "device_type",
    "city",
    "country",
    "state",
    "region",
    "age_bucket",
    "occupation",
    "income_bucket",
    "acquisition_cohort",
]


def _requested_breakdown_count(question: str | None) -> int:
    m = _TOP_N_RE.search((question or "").strip())
    return int(m.group(1)) if m else 3


def _retention_wants_survival_curve(question: str | None) -> bool:
    """Multi-window survival SQL only when the user asks for more than one retention window."""
    q = (question or "").lower()
    if not q:
        return False
    if any(
        tok in q
        for tok in (
            "survival", "curve", "d1", "d30", "day 1", "day 30",
            "all windows", "multiple windows", "d1/d7", "d7/d30",
        )
    ):
        return True
    return bool(re.search(r"\bd1\b.*\b(d7|d30)\b|\b(d7|d30)\b.*\bd1\b", q))


def _wants_dimension_breakdown(question: str | None) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    if _BREAKDOWN_HINT_RE.search(q):
        return True
    return bool(re.search(r"\bby\s+(platform|city|country|age|cohort|segment|dimension)\b", q))


def _pick_top_user_dimensions(
    user_sampled_values: Optional[dict],
    *,
    max_dims: int,
) -> list[str]:
    if not isinstance(user_sampled_values, dict):
        return []
    scored: list[tuple[int, int, str]] = []
    for col, vals in user_sampled_values.items():
        c = str(col or "").strip()
        if not c:
            continue
        lc = c.lower()
        if lc in _USER_DIM_SKIP:
            continue
        if any(tok in lc for tok in ("id", "uuid", "email", "phone", "timestamp", "date")):
            continue
        if not _DIM_TOKEN_RE.fullmatch(lc):
            continue
        n = len(vals or [])
        if n < 2 or n > 50:
            continue
        pr = _DIM_PRIORITY.index(lc) if lc in _DIM_PRIORITY else len(_DIM_PRIORITY) + 1
        scored.append((pr, n, c))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [c for _, _, c in scored[:max_dims]]


def _plan_investigations(
    qo: QueryObject,
    *,
    question: str | None = None,
    user_sampled_values: Optional[dict] = None,
) -> list[Investigation]:
    """
    Build a list of Investigation objects (with SQL) for the given QueryObject.
    Imports compilers here to avoid circular imports.
    """
    from core.sql.compilers import (
        _compile_funnel, _compile_funnel_compare, _compile_retention,
        _compile_behavioral_cohort, _compile_time_between, _compile_journey,
        _compile_demographic_breakdown, _compile_forecast,
        _compile_metric, _compile_segment,
        _compile_user_lifecycle, _compile_stickiness,
        _compile_funnel_property_drilldown, _compile_xyz_matrix,
    )
    from copy import copy

    at = qo.analysis_type
    wants_breakdown = _wants_dimension_breakdown(question)
    dim_limit       = _requested_breakdown_count(question)
    top_dims        = _pick_top_user_dimensions(user_sampled_values, max_dims=max(1, dim_limit))

    if at == "funnel":
        invs = [Investigation(
            name="funnel_conversion",
            purpose=f"Ordered funnel: {qo.funnel_steps[0] if qo.funnel_steps else 'N/A'} "
                    f"→ {qo.funnel_steps[-1] if qo.funnel_steps else 'N/A'}",
            sql=_compile_funnel(qo),
        )]
        # Optional breakdown only when explicitly requested.
        if wants_breakdown and qo.funnel_steps and top_dims:
            for dim in top_dims:
                top_qo = copy(qo)
                top_qo.analysis_type = "segment"
                top_qo.event = qo.funnel_steps[0]
                top_qo.breakdown = dim
                invs.append(Investigation(
                    name=f"funnel_entry_by_{dim}",
                    purpose=f"{dim.replace('_', ' ').title()} mix at funnel entry ({qo.funnel_steps[0]})",
                    sql=_compile_segment(top_qo),
                ))
        return invs

    if at == "funnel_compare":
        return [Investigation(
            name="funnel_period_compare",
            purpose="Funnel steps: current half vs prior half of window",
            sql=_compile_funnel_compare(qo),
        )]

    if at == "funnel_property_drilldown":
        invs = [Investigation(
            name="funnel_property_drilldown",
            purpose=(
                f"'{qo.funnel_steps[0]}' → '{qo.funnel_steps[1]}' conversion by "
                f"{qo.breakdown or 'selected dimension'}"
            ),
            sql=_compile_funnel_property_drilldown(qo),
        )]
        # Also run the overall funnel for context
        invs.append(Investigation(
            name="funnel_overview",
            purpose="Overall funnel conversion for context",
            sql=_compile_funnel(qo),
        ))
        return invs

    if at == "retention":
        bd = str(getattr(qo, "breakdown", None) or "").strip()
        if not bd and wants_breakdown and top_dims:
            bd = top_dims[0]
            qo.breakdown = bd
        if bd:
            invs = [
                Investigation(
                    name=f"retention_by_{bd}",
                    purpose=(
                        f"First {qo.retention_window_days}-day retention by "
                        f"{bd.replace('_', ' ')} for '{qo.event}'"
                    ),
                    sql=_compile_retention(qo),
                )
            ]
        else:
            invs = [
                Investigation(
                    name="retention_cohort",
                    purpose=(
                        f"First {qo.retention_window_days}-day cohort retention for '{qo.event}'"
                    ),
                    sql=_compile_retention(qo),
                )
            ]
        if _retention_wants_survival_curve(question):
            invs.append(Investigation(
                name="retention_survival_curve",
                purpose=f"Survival curve: D1 / D7 / D30 retention across cohorts for '{qo.event}'",
                sql=_compile_retention_survival(qo),
            ))
        return invs

    if at == "behavioral_cohort":
        cross = (
            qo.event
            and qo.event_b
            and str(qo.event) == str(qo.event_b)
            and getattr(qo, "secondary_date_from", None)
            and getattr(qo, "secondary_date_to", None)
        )
        invs = [Investigation(
            name="cohort_trend",
            purpose=(
                f"Primary-window users who did '{qo.event}' vs overlap in second window"
                if cross
                else (
                    f"Users who did '{qo.event}' but not '{qo.event_b}'"
                    if qo.event_b
                    else f"Users who did '{qo.event}'"
                )
            ),
            sql=_compile_behavioral_cohort(qo),
        )]
        if wants_breakdown and top_dims:
            for dim in top_dims:
                seg_qo = copy(qo)
                seg_qo.analysis_type = "segment"
                seg_qo.breakdown = dim
                invs.append(Investigation(
                    name=f"cohort_{dim}_breakdown",
                    purpose=f"{dim.replace('_', ' ').title()} mix of the cohort",
                    sql=_compile_segment(seg_qo),
                ))
        return invs

    if at == "time_between":
        return [Investigation(
            name="time_between_distribution",
            purpose=f"Time from '{qo.event}' to '{qo.event_b or qo.event}'",
            sql=_compile_time_between(qo),
        )]

    if at == "journey":
        invs = [Investigation(
            name="journey_next_events",
            purpose=f"Top next events after '{qo.event}' within 7 days",
            sql=_compile_journey(qo),
        )]
        # Add Sankey-ready data: top 2-hop paths
        invs.append(Investigation(
            name="journey_two_hop",
            purpose=f"Two-step paths starting from '{qo.event}' (for flow diagram)",
            sql=_compile_journey_two_hop(qo),
        ))
        return invs

    if at == "user_lifecycle":
        invs = [Investigation(
            name="lifecycle_stages",
            purpose=f"Lifecycle stage distribution for '{qo.event}' users",
            sql=_compile_user_lifecycle(qo),
        )]
        # Optional breakdown only when explicitly requested.
        if wants_breakdown and top_dims:
            for dim in top_dims:
                stage_seg_qo = copy(qo)
                stage_seg_qo.analysis_type = "segment"
                stage_seg_qo.breakdown = dim
                invs.append(Investigation(
                    name=f"lifecycle_by_{dim}",
                    purpose=f"Active user base by {dim.replace('_', ' ')}",
                    sql=_compile_segment(stage_seg_qo),
                ))
        # Trend: core event over time
        trend_qo = copy(qo)
        trend_qo.analysis_type = "metric"
        invs.append(Investigation(
            name="lifecycle_activity_trend",
            purpose=f"Daily trend of '{qo.event}' activity",
            sql=_compile_metric(trend_qo),
        ))
        return invs

    if at == "stickiness":
        invs = [Investigation(
            name="stickiness_dau_mau",
            purpose=f"DAU/WAU/MAU stickiness ratios for '{qo.event}'",
            sql=_compile_stickiness(qo),
        )]
        # Power user curve: event count distribution
        invs.append(Investigation(
            name="power_user_curve",
            purpose=f"Event frequency distribution — who are the power users of '{qo.event}'?",
            sql=_compile_power_user_distribution(qo),
        ))
        return invs

    if at == "xyz_matrix":
        return [Investigation(
            name="xyz_cohort_matrix",
            purpose=(
                f"3-axis matrix: cohort × {qo.xyz_axis1 or qo.breakdown or 'selected dimension'} "
                f"× users for '{qo.event}'"
            ),
            sql=_compile_xyz_matrix(qo),
        )]

    if at == "forecast":
        return [Investigation(
            name="historical_trend",
            purpose=f"Historical trend for '{qo.event}' (used as forecast basis)",
            sql=_compile_forecast(qo),
        )]

    if at == "demographic_breakdown":
        picked = _pick_top_user_dimensions(user_sampled_values, max_dims=8)
        if not picked:
            picked = [d for d in _DIM_PRIORITY if d not in _USER_DIM_SKIP]
        invs = []
        for col in picked:
            dim_qo = copy(qo)
            dim_qo.analysis_type = "segment"
            dim_qo.breakdown = col
            invs.append(Investigation(
                name=f"event_{col}",
                purpose=f"{col.replace('_', ' ').title()} distribution",
                sql=_compile_segment(dim_qo),
            ))
        return invs

    # Fallback: treat as a simple metric trend
    return [Investigation(
        name="metric_trend",
        purpose=f"Trend for '{qo.event or qo.metric_id}'",
        sql=_compile_metric(qo) if qo.event else "",
    )]


# ── Helper SQL for enhanced investigations ────────────────────────────────────

def _compile_retention_survival(qo: QueryObject) -> str:
    """
    Multi-window retention: D1, D7, D30 per cohort week in one query.
    Returns: cohort_week, d1_pct, d7_pct, d30_pct, cohort_size.
    """
    from core.sql.compilers import (
        _cohort_anchor_time_filter,
        _qo_filters_clause,
        _guards_clause,
        _guards_clause as gc_fn,
    )
    # Survival curves need enough lookback to populate multiple cohorts.
    # If the user did not pin explicit dates, widen narrow defaults.
    if not getattr(qo, "date_from", None) and not getattr(qo, "date_to", None):
        try:
            if int(getattr(qo, "time_range_days", 30) or 30) < 120:
                qo.time_range_days = 120
        except Exception:
            qo.time_range_days = 120
    caf = _cohort_anchor_time_filter(qo, "first_seen_ts")
    event = str(qo.event or "app_opened").replace("'", "''")
    fc  = _qo_filters_clause(qo)
    fc_e = _qo_filters_clause(qo, "e")
    gc  = _guards_clause()
    gc_e = gc_fn("e")
    return f"""WITH user_first AS (
  SELECT user_id, MIN(timestamp) AS first_seen_ts
  FROM events
  WHERE event_name = '{event}'{fc}{gc}
  GROUP BY user_id
),
cohort AS (
  SELECT
    user_id,
    first_seen_ts,
    DATE_TRUNC('week', first_seen_ts)::DATE AS cohort_week
  FROM user_first
  WHERE {caf}
),
ret AS (
  SELECT
    c.user_id,
    c.cohort_week,
    MAX(CASE WHEN DATEDIFF('day', c.first_seen_ts, e.timestamp) BETWEEN 1  AND 2  THEN 1 ELSE 0 END) AS ret_d1,
    MAX(CASE WHEN DATEDIFF('day', c.first_seen_ts, e.timestamp) BETWEEN 7  AND 8  THEN 1 ELSE 0 END) AS ret_d7,
    MAX(CASE WHEN DATEDIFF('day', c.first_seen_ts, e.timestamp) BETWEEN 30 AND 31 THEN 1 ELSE 0 END) AS ret_d30
  FROM cohort c
  JOIN events e ON c.user_id = e.user_id
  WHERE e.event_name = '{event}'{fc_e}{gc_e}
  GROUP BY 1, 2
)
SELECT
  cohort_week,
  COUNT(DISTINCT user_id)                                                          AS cohort_size,
  ROUND(SUM(ret_d1)  * 100.0 / NULLIF(COUNT(DISTINCT user_id), 0), 1)            AS d1_retention_pct,
  ROUND(SUM(ret_d7)  * 100.0 / NULLIF(COUNT(DISTINCT user_id), 0), 1)            AS d7_retention_pct,
  ROUND(SUM(ret_d30) * 100.0 / NULLIF(COUNT(DISTINCT user_id), 0), 1)            AS d30_retention_pct
FROM ret
GROUP BY 1
ORDER BY 1"""


def _compile_journey_two_hop(qo: QueryObject) -> str:
    """
    Two-step event paths after anchor event. Returns source → target pairs
    suitable for Sankey diagram rendering.
    """
    from core.sql.compilers import _time_filter, _filters_clause, _guards_clause
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc    = _filters_clause(qo.filters or {})
    gc    = _guards_clause()
    gc_e  = _guards_clause("e")

    return f"""WITH anchor AS (
  SELECT user_id, MAX(timestamp) AS anchor_time
  FROM events WHERE event_name = '{event}' AND {tf}{fc}{gc}
  GROUP BY user_id
),
next_steps AS (
  SELECT
    a.user_id,
    e.event_name,
    e.timestamp,
    ROW_NUMBER() OVER (PARTITION BY a.user_id ORDER BY e.timestamp) AS step_n
  FROM anchor a
  JOIN events e ON a.user_id = e.user_id
  WHERE e.timestamp > a.anchor_time
    AND e.timestamp < a.anchor_time + INTERVAL '7' DAY
    AND e.event_name != '{event}'{gc_e}
),
hop1 AS (SELECT user_id, event_name AS step1 FROM next_steps WHERE step_n = 1),
hop2 AS (SELECT user_id, event_name AS step2 FROM next_steps WHERE step_n = 2)
SELECT
  '{event}'                                                    AS source,
  h1.step1                                                     AS target,
  COUNT(DISTINCT h1.user_id)                                   AS users
FROM hop1 h1
GROUP BY 1, 2
UNION ALL
SELECT
  h1.step1                                                     AS source,
  h2.step2                                                     AS target,
  COUNT(DISTINCT h1.user_id)                                   AS users
FROM hop1 h1
JOIN hop2 h2 ON h1.user_id = h2.user_id
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 40"""


def _compile_power_user_distribution(qo: QueryObject) -> str:
    """
    Event frequency distribution — how many events did each user fire?
    Buckets users into frequency tiers for the power user curve.
    """
    from core.sql.compilers import _time_filter, _filters_clause, _guards_clause
    tf    = _time_filter(qo)
    event = str(qo.event or "app_opened").replace("'", "''")
    fc    = _filters_clause(qo.filters or {})
    gc    = _guards_clause()

    return f"""WITH user_counts AS (
  SELECT user_id, COUNT(*) AS event_count
  FROM events
  WHERE event_name = '{event}' AND {tf}{fc}{gc}
  GROUP BY user_id
)
SELECT
  CASE
    WHEN event_count = 1  THEN '1x (one-time)'
    WHEN event_count <= 3  THEN '2-3x (low frequency)'
    WHEN event_count <= 7  THEN '4-7x (occasional)'
    WHEN event_count <= 15 THEN '8-15x (regular)'
    WHEN event_count <= 30 THEN '16-30x (frequent)'
    ELSE '31+ (power user)'
  END                                    AS frequency_bucket,
  COUNT(DISTINCT user_id)                AS users,
  ROUND(COUNT(DISTINCT user_id) * 100.0 / SUM(COUNT(DISTINCT user_id)) OVER (), 1) AS pct_of_total,
  ROUND(AVG(event_count), 1)             AS avg_events_in_bucket
FROM user_counts
GROUP BY 1
ORDER BY MIN(event_count)"""


# ── Data quality helpers ──────────────────────────────────────────────────────

def _format_data_quality_block(val) -> str:
    reason = getattr(val, "sanity_reason", "") or ""
    arith = getattr(val, "arithmetic_warnings", []) or []
    details = "; ".join(w.detail for w in arith[:2]) if arith else reason
    if not details:
        details = "The result is outside a plausible range for this metric."
    return (
        "The data for this query looks unusual and may not be reliable. "
        f"Reason: {details}. "
        "Suggested action: check your data pipeline or narrow the time range before interpreting results."
    )


# ── Main entry point ──────────────────────────────────────────────────────────

@track(name="investigate", tags=["analysis"], capture_input=False, capture_output=False)
def investigate(
    question: str,
    qo: QueryObject,
    db_path: str,
    openai_key: Optional[str] = None,
    catalog: Optional[dict] = None,
    event_sampled_values: Optional[dict] = None,
    user_sampled_values: Optional[dict] = None,
    hypothesis_doc=None,
    narrative_thread: Optional[str] = None,
    stream_callback: Optional[Callable[[str], None]] = None,
    skip_narrative: bool = False,
) -> AnalystReport:
    """
    Full analytics pipeline for one QueryObject:
      plan → execute SQL → rule-based insights → story arc narrative.

    skip_narrative: when True, skips validate_findings and build_story_arc.
    Used by deep analysis sub-investigations — the deep story arc synthesises
    all results in one pass, so per-sub narratives are redundant.
    """
    from core.agents.story_architect import STORY_ARC_FAILURE_SUMMARY, build_story_arc
    from core.sql.validator import validate_findings

    report = AnalystReport(analysis_type=qo.analysis_type)

    # 1. Plan investigations
    report.investigations = _plan_investigations(
        qo,
        question=question,
        user_sampled_values=user_sampled_values,
    )

    # 2. Execute SQL
    for inv in report.investigations:
        _exec_investigation(inv, db_path)

    # 3. Rule-based insights
    for inv in report.investigations:
        if not inv.error and not inv.df.empty:
            inv.insight = _auto_insight(inv, qo)

    # 4. Validate (skipped for deep-analysis sub-investigations)
    valid_invs = [i for i in report.investigations if not i.error and not i.df.empty]
    validation = None
    if valid_invs and not skip_narrative:
        try:
            validation = validate_findings(qo, report.investigations, catalog, openai_key)
        except Exception:
            pass

    # 4b. Data quality gate — block story arc for SUSPICIOUS / grade-D results
    if validation and not skip_narrative and (
        validation.grade == "D" or validation.sanity_flag == "SUSPICIOUS"
    ):
        report.narrative = _format_data_quality_block(validation)
        report.executive_summary = report.narrative
        report.data_quality_blocked = True
        return report

    # 5. Build narrative (skipped for deep-analysis sub-investigations)
    if valid_invs and not skip_narrative:
        try:
            arc = build_story_arc(
                question=question,
                qo=qo,
                plan=report.investigations,
                hypothesis_doc=hypothesis_doc,
                validation=validation,
                catalog=catalog,
                openai_api_key=openai_key,
                narrative_thread=narrative_thread,
                stream_callback=stream_callback,
            )
            report.narrative         = arc.full_narrative or arc.executive_summary
            report.executive_summary = arc.executive_summary
            report.next_steps        = arc.next_steps
            report.beats             = arc.beats
            report.confidence_label  = arc.confidence_label
            report.hypothesis_verdict = arc.hypothesis_verdict
            if (report.narrative or "").strip() == STORY_ARC_FAILURE_SUMMARY:
                bits = [
                    f"{inv.name}: {inv.insight or '(see data)'}"
                    for inv in valid_invs[:4]
                ]
                report.narrative = (
                    "Structured narrative was unavailable. Key signals: " + " · ".join(bits)
                    if bits
                    else f"Analysis complete ({len(valid_invs)} slice(s)); narrative synthesis failed."
                )
                report.executive_summary = report.narrative[:800]
        except Exception as exc:
            report.narrative = f"Analysis complete. {len(valid_invs)} investigation(s) ran successfully."
    else:
        all_invs = report.investigations
        if not all_invs:
            report.narrative = "I couldn't generate a query for this request. Try rephrasing or narrowing the question."
        else:
            errors   = [i for i in all_invs if i.error]
            no_data  = [i for i in all_invs if not i.error and i.df.empty]
            subject  = qo.metric_id or qo.event or qo.analysis_type
            if errors and not no_data:
                first_err = errors[0].error
                simplified = first_err[:120] if len(first_err) > 120 else first_err
                report.narrative = (
                    f"The query for **{subject}** hit an execution error: {simplified}. "
                    "This may be a temporary issue — try rephrasing or adjusting the filters."
                )
            else:
                tf = ""
                if qo.date_from and qo.date_to:
                    tf = f" between {qo.date_from} and {qo.date_to}"
                elif qo.time_range_days:
                    tf = f" in the last {qo.time_range_days} days"
                report.narrative = (
                    f"No data found for **{subject}**{tf}. "
                    "Check that this event occurred in the selected time window, "
                    "or try a broader date range."
                )
        report.executive_summary = report.narrative

    return report
