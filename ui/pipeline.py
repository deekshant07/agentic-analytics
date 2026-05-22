"""
pipeline.py — Streamlit-layer pipeline helpers.

Bridges the Streamlit UI (chat.py) with core pipeline modules.
Reads catalog / sampled values from st.session_state so callers
don't need to pass them on every invocation.

Public API (imported by chat.py):
    get_sql(prompt, *, hypothesis_doc)          → (sql, metric_name, clarify_msg, qo)
    run_sql(sql)                                → DataFrame
    run_sql_with_retry(sql, prompt, qo)         → (DataFrame, sql)
    fix_sql(sql, error_msg, qo)                 → sql
    ask_llm(messages, *, model, temperature)    → str
    narrate(prompt, df, qo)                     → str
    small_narration(prompt, df, *, qo, metric_name) → str
    should_generate_hypotheses(prompt)          → bool
    should_run_deep_analysis(prompt, qo)        → bool
    DEFAULT_USE_POLICY_ARBITER
    DEFAULT_HYPOTHESIS_ONLY_DIAGNOSE
    DEFAULT_DEEP_ANALYSIS_AUTO
    (plus private helpers re-exported for chat.py's debug_panel calls)
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd
import streamlit as st
from core.infra.llm import make_llm_client, LLM_FAST, LLM_STRONG
from core.semantic.presentation import format_rate_columns_for_display, is_rate_column_name

from core.pipeline.orchestrator import orchestrate
from core.sql.compilers import compile_query
from core.semantic.resolver_policy import resolve_query_policy
from core.agents.policy_arbiter import arbitrate
from core.viz.charts import _is_temporal_col
from core.infra.logger import log_sql, log_llm_call
from ui.cohort_labels import (
    filter_qualifier_prefix,
    activity_cohort_label,
    anchor_noun_phrase,
    cohort_slice_filters_present,
    same_month_anchor_chart_title_parts,
)
from core.memory.chat_history import get_qo_history, get_narrative_thread
from ui.qo_fixups import (
    _apply_time_intent_overrides,
    _hydrate_retention_event_from_metric,
    _hydrate_primary_event_for_action_types,
    _apply_metric_variant_overrides,
    _sanitize_invalid_breakdown,
    _remap_invalid_filter_values_via_custom_events,
    _apply_followup_context_repair,
    _apply_same_query_followup,
    _maybe_resolve_clarify_as_followup,
    _inherit_metric_status_and_filters,
    _sanitize_retention_dimension_status,
    _apply_activation_window_from_prompt,
    _apply_retention_window_from_prompt,
    _apply_same_month_anchor_composite_metric,
    _apply_lineage_rollforward_filters,
    _materialize_metric_status_into_filters_for_equality_cohorts,
    _strip_redundant_calendar_day_filter,
    _extract_clarify_context,
)

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_USE_POLICY_ARBITER      = True
DEFAULT_HYPOTHESIS_ONLY_DIAGNOSE = True
DEFAULT_DEEP_ANALYSIS_AUTO       = False

_DB_PATH = Path(__file__).parent.parent / "jupiter.duckdb"

# Signals that suggest a diagnostic ("why") question → generate hypotheses
_DIAGNOSE_SIGNALS = frozenset({
    "why", "cause", "drop", "spike", "decline", "increase",
    "decrease", "fell", "dropped", "spiked", "jumped",
})

# Signals that suggest deep multi-angle analysis
_DEEP_SIGNALS = frozenset({
    "deep", "thorough", "comprehensive", "detailed", "full",
    "investigate", "analyze", "analyse", "diagnosis",
})


# ── Routing helpers ───────────────────────────────────────────────────────────

def should_generate_hypotheses(prompt: str) -> bool:
    """True when the question looks like a diagnostic 'why did X change' query."""
    lower = prompt.lower()
    return any(s in lower for s in _DIAGNOSE_SIGNALS)


def should_run_deep_analysis(prompt: str, qo) -> bool:
    """
    True when deep analysis should be triggered.
    Respects the flag_deep_auto session state and the QO depth field.
    """
    flag = st.session_state.get("flag_deep_auto", DEFAULT_DEEP_ANALYSIS_AUTO)
    if flag:
        lower = prompt.lower()
        if any(s in lower for s in _DEEP_SIGNALS):
            return True
    return getattr(qo, "depth", "quick") == "deep"


# ── SQL execution ─────────────────────────────────────────────────────────────

def run_sql(sql: str) -> Optional[pd.DataFrame]:
    """Execute SQL against DuckDB and return a DataFrame, or None on error."""
    if not sql or sql.startswith("__"):
        return None
    t0 = time.perf_counter()
    try:
        conn = duckdb.connect(str(_DB_PATH), read_only=True)
        df = conn.execute(sql).df()
        conn.close()
        log_sql(
            investigation_name="pipeline.run_sql",
            latency_ms=(time.perf_counter() - t0) * 1000,
            row_count=len(df),
        )
        return df
    except Exception as exc:
        log_sql(
            investigation_name="pipeline.run_sql",
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc),
        )
        return None


def fix_sql(sql: str, error_msg: str, qo) -> str:
    """Ask the LLM to fix a broken SQL query. Returns the original on failure."""
    if not sql:
        return sql
    client = make_llm_client()
    at     = getattr(qo, "analysis_type", "unknown") if qo else "unknown"
    prompt = (
        f"Fix this DuckDB SQL for a '{at}' analytics query.\n"
        f"Error: {error_msg}\n\n"
        f"SQL:\n{sql}\n\n"
        "Return ONLY the fixed SQL with no explanation."
    )
    t0 = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=LLM_FAST,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        fixed = resp.choices[0].message.content.strip()
        fixed = fixed.removeprefix("```sql").removeprefix("```").removesuffix("```").strip()
        log_llm_call(
            call_site="pipeline.fix_sql",
            model=LLM_FAST,
            prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        return fixed if fixed else sql
    except Exception as exc:
        log_llm_call(
            call_site="pipeline.fix_sql",
            model=LLM_FAST,
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc),
        )
        return sql


def run_sql_with_retry(
    sql: str,
    prompt: str,
    qo,
    max_retries: int = 1,
) -> tuple[Optional[pd.DataFrame], str]:
    """
    Run SQL against DuckDB.  On failure, ask the LLM to fix it and retry once.
    Returns (DataFrame | None, final_sql).
    """
    if not sql or sql.startswith("__"):
        return None, sql

    try:
        conn = duckdb.connect(str(_DB_PATH), read_only=True)
        df = conn.execute(sql).df()
        conn.close()
        return df, sql
    except Exception as exc:
        error_msg = str(exc)

    if max_retries <= 0:
        return None, sql

    fixed_sql = fix_sql(sql, error_msg, qo)
    if fixed_sql == sql:
        return None, sql

    try:
        conn = duckdb.connect(str(_DB_PATH), read_only=True)
        df = conn.execute(fixed_sql).df()
        conn.close()
        return df, fixed_sql
    except Exception:
        return None, fixed_sql


# ── LLM helpers ───────────────────────────────────────────────────────────────

def ask_llm(
    messages: list[dict],
    *,
    model: str = LLM_FAST,
    temperature: float = 0.3,
    call_site: str = "pipeline.ask_llm",
) -> str:
    """Thin wrapper: call LLM chat and return the response text."""
    client = make_llm_client()
    t0 = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model, temperature=temperature, messages=messages
        )
        log_llm_call(
            call_site=call_site,
            model=model,
            prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        log_llm_call(
            call_site=call_site, model=model,
            latency_ms=(time.perf_counter() - t0) * 1000, error=str(exc),
        )
        return ""


def narrate(prompt: str, df: Optional[pd.DataFrame], qo) -> str:
    """Generate a concise narrative for a query result using the LLM."""
    return small_narration(prompt, df, qo=qo, metric_name=None)


# ── Narrative helpers ─────────────────────────────────────────────────────────

def _preferred_metric_column(df: pd.DataFrame, qo=None) -> Optional[str]:
    """Return the most likely 'value' column from a DataFrame."""
    preferred = [
        "retention_pct",
        "activation_rate",
        "rate_pct",
        "status_rate_pct",
        "pct",
        "unique_users",
        "events_per_user",
        "count",
        "value",
        "users",
    ]
    for col in preferred:
        if col in df.columns:
            return col
    pct_cols = [c for c in df.columns if is_rate_column_name(c)]
    if pct_cols:
        return pct_cols[0]
    num_cols = df.select_dtypes("number").columns.tolist()
    skip = {c for c in num_cols if c.endswith("_users") or c == "cohort_size"}
    for c in num_cols:
        if c not in skip:
            return c
    return num_cols[0] if num_cols else None


def _looks_like_rate_metric(df: pd.DataFrame) -> bool:
    metric_col = _preferred_metric_column(df)
    if not metric_col:
        return False
    vals = pd.to_numeric(df[metric_col], errors="coerce").dropna()
    return (vals <= 1.0).all() and (vals >= 0).all() and len(vals) >= 3


def _fmt_period_value(v, col: Optional[str] = None, qo=None) -> str:
    try:
        f = float(v)
        if col and is_rate_column_name(col) and 0 <= f <= 100:
            return f"{f:.1f}%"
        if qo:
            mid = (getattr(qo, "metric_id", None) or "").lower()
            at = (getattr(qo, "analysis_type", None) or "").lower()
            if (at == "retention" or "activation" in mid) and 0 <= f <= 100:
                return f"{f:.1f}%"
        if f >= 1_000_000:
            return f"{f/1_000_000:.1f}M"
        if f >= 1_000:
            return f"{f/1_000:.1f}K"
        return f"{f:,.0f}"
    except Exception:
        return str(v)


def _series_label_from_column(col: str, qo=None) -> str:
    from core.pipeline.activation_window import series_label_for_column
    return series_label_for_column(col, qo)


def _build_multi_metric_time_summary(
    df: pd.DataFrame, date_col: str, num_cols: list[str], qo=None,
) -> str:
    """Engaging summary when several numeric series share one time axis (e.g. active vs transacted)."""
    d = df[[date_col] + num_cols].copy()
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce")
    d = d.dropna(subset=[date_col]).sort_values(date_col)
    if len(d) < 2:
        return ""

    parts: list[str] = []
    for col in num_cols[:4]:
        vals = pd.to_numeric(d[col], errors="coerce").dropna()
        if len(vals) < 2:
            continue
        first, last = float(vals.iloc[0]), float(vals.iloc[-1])
        peak = float(vals.max())
        change = ((last - first) / first * 100) if first else 0.0
        direction = "↑" if change >= 0 else "↓"
        label = _series_label_from_column(col, qo)
        parts.append(
            f"**{label}** {direction}{abs(change):.0f}% "
            f"({_fmt_period_value(first, col, qo)}→{_fmt_period_value(last, col, qo)}; "
            f"peak {_fmt_period_value(peak, col, qo)})"
        )

    extra = ""
    if len(num_cols) >= 2:
        c0, c1 = num_cols[0], num_cols[1]
        try:
            la = pd.to_numeric(d[c0], errors="coerce").astype(float)
            lb = pd.to_numeric(d[c1], errors="coerce").astype(float)
            share = (lb.iloc[-1] / la.iloc[-1] * 100.0) if la.iloc[-1] else 0.0
            if share > 0:
                extra = (
                    f" Latest period: **{share:.0f}%** of "
                    f"{_series_label_from_column(c0, qo)} also show up as "
                    f"{_series_label_from_column(c1, qo)}."
                )
        except Exception:
            pass

    return "; ".join(parts) + extra


def _build_time_series_summary(df: pd.DataFrame, qo=None) -> str:
    """Summarise a time-series DataFrame as: first → last, direction, peak."""
    if df.empty:
        return ""

    date_cols = [c for c in df.columns if c in ("day", "week", "month", "date", "period")]
    if not date_cols:
        date_cols = [c for c in df.columns if _is_temporal_col(df, c)]
    skip = set(date_cols)
    num_cols = [c for c in df.columns if c not in skip and pd.api.types.is_numeric_dtype(df[c])]

    if len(num_cols) >= 2 and date_cols:
        m = _build_multi_metric_time_summary(df, date_cols[0], num_cols, qo=qo)
        if m:
            return m

    metric_col = _preferred_metric_column(df, qo=qo) or (num_cols[0] if num_cols else None)
    if not metric_col:
        return ""

    vals = pd.to_numeric(df[metric_col], errors="coerce").dropna()
    if len(vals) < 2:
        return ""

    first = vals.iloc[0]
    last = vals.iloc[-1]
    peak = vals.max()
    change = ((last - first) / first * 100) if first else 0
    direction = "↑" if change >= 0 else "↓"

    return (
        f"{direction} {abs(change):.1f}% over the period "
        f"({_fmt_period_value(first, metric_col, qo)} → {_fmt_period_value(last, metric_col, qo)}). "
        f"Peak: {_fmt_period_value(peak, metric_col, qo)}."
    )


def _period_hint_from_qo(qo) -> str:
    if not qo:
        return "last 30 days"
    if getattr(qo, "date_from", None) and getattr(qo, "date_to", None):
        base = f"{qo.date_from} to {qo.date_to}"
    else:
        n = getattr(qo, "time_range_days", 30) or 30
        base = f"last {n} days"
    win = getattr(qo, "activation_window_days", None)
    if win and int(win) > 0:
        return f"{base}, {int(win)}-day activation window"
    return base


def _event_display_name(ev: Optional[str]) -> str:
    if not ev:
        return "Activity"
    return str(ev).replace("_", " ").strip().title()


def period_calendar_month_label(qo) -> str:
    """e.g. January 2026 from date_from; empty if unknown."""
    if not qo:
        return ""
    df = getattr(qo, "date_from", None)
    if not df:
        return ""
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(df)[:10]).strftime("%B %Y")
    except Exception:
        return ""


def same_month_anchor_chart_title(qo) -> str:
    """Evidence chart title for same_month_anchor analysis."""
    if not qo or getattr(qo, "analysis_type", "") != "same_month_anchor":
        return ""
    cal = period_calendar_month_label(qo)
    return same_month_anchor_chart_title_parts(
        calendar_month=cal,
        qo=qo,
    )


def evidence_chart_title_for_qo(qo, metric_name: Optional[str]) -> Optional[str]:
    """Prefer contextual titles for special analysis types."""
    if qo and getattr(qo, "analysis_type", "") == "same_month_anchor":
        t = same_month_anchor_chart_title(qo)
        return t or None
    m = (metric_name or "").strip()
    return m or None


def _contextual_metric_label(qo, metric_name: Optional[str]) -> str:
    if qo and getattr(qo, "analysis_type", "") == "same_month_anchor":
        return activity_cohort_label(qo=qo)
    # When a named catalog metric has filter qualifiers, prefix the metric name with
    # the qualifier so "activation_rate + In App" → "In App Activation Rate" instead
    # of "IS_NOT_NULL Activity users" or dropping the metric name entirely.
    if qo and metric_name and cohort_slice_filters_present(qo):
        prefix = filter_qualifier_prefix(qo)
        if prefix:
            return f"{prefix} {metric_name}"
    # Catalog metric names ("Transacting User") omit channel/geo/status slices.
    if qo and cohort_slice_filters_present(qo):
        return activity_cohort_label(qo=qo)
    if metric_name:
        return metric_name
    event = getattr(qo, "event", None) if qo else None
    if event:
        return event.replace("_", " ").title()
    return "metric"


def _scalar_summary_agent(df: pd.DataFrame, qo, metric_name: Optional[str]) -> str:
    """Rule-based scalar summary — used when there's a single-row / single-value result."""
    metric_col = _preferred_metric_column(df)
    if not metric_col or df.empty:
        return ""
    val = df[metric_col].iloc[0]
    label = _contextual_metric_label(qo, metric_name)
    cal = period_calendar_month_label(qo) if qo else ""
    if cal.strip():
        window = f"**{cal.strip()}**"
    else:
        window = f"**{_period_hint_from_qo(qo)}**"
    return f"**{_fmt_period_value(val)}** **{label}** in {window}."


def _analyst_so_what(
    question: str,
    df: pd.DataFrame,
    qo,
    period: str,
) -> str:
    """
    One-sentence business insight: not what the numbers are, but what they mean.
    Returns empty string on any failure — never blocks the main narration.
    """
    if df.empty:
        return ""
    try:
        preview = df.head(8).to_string(index=False)
        at = getattr(qo, "analysis_type", "") or ""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a senior data analyst answering a business question. "
                    "Given the data below, write ONE sentence that answers the business question. "
                    "Lead with the most important finding and its implication — not a restatement of numbers. "
                    "Examples of good headlines:\n"
                    "  • 'Android users drove 78% of the drop — iOS conversion held steady.'\n"
                    "  • 'Step 2 is the critical bottleneck — only 31% of users proceed past onboarding.'\n"
                    "  • 'Power users (>5 transactions) represent just 12% of users but 67% of volume.'\n"
                    "Be specific and direct. No hedging. No markdown."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\nAnalysis type: {at}\nPeriod: {period}\n\n{preview}"
                ),
            },
        ]
        result = ask_llm(
            messages, model=LLM_FAST, temperature=0.2,
            call_site="pipeline.analyst_so_what",
        )
        return f"**Key finding:** {result.strip()}" if result else ""
    except Exception:
        return ""


def small_narration(
    prompt: str,
    df: Optional[pd.DataFrame],
    *,
    qo=None,
    metric_name: Optional[str] = None,
) -> str:
    """
    Generate a short (1-3 sentence) narrative for a simple metric/segment result.
    Uses rule-based logic first; falls back to a cheap LLM call for anything unusual.
    """
    if df is None or df.empty:
        return "No data found for this query."

    label  = _contextual_metric_label(qo, metric_name)
    period = _period_hint_from_qo(qo)

    # same_month_anchor: cohort buckets are already human-readable from SQL
    if getattr(qo, "analysis_type", "") == "same_month_anchor" and "cohort_month_alignment" in df.columns:
        subj = activity_cohort_label(qo=qo)
        anc = anchor_noun_phrase(getattr(qo, "event_b", None))
        cal = period_calendar_month_label(qo)
        window = f"**{cal}**" if cal else f"the selected window ({period})"
        mc = _preferred_metric_column(df)
        if not mc:
            return (
                f"In {window}, among **{subj}**, split by whether **{anc}** fell in the **same calendar month** "
                f"as their first qualifying activity in that window vs earlier vs not seen in data. "
                f"The anchor is inferred from **events**; a **users**-table onboarding date is the usual alternative "
                "once exposed in the semantic layer."
            )
        lines = []
        for _, row in df.sort_values(mc, ascending=False).iterrows():
            cat = str(row["cohort_month_alignment"])
            lines.append(f"**{cat}**: {_fmt_period_value(row[mc])}")
        detail = "; ".join(lines)
        return (
            f"In {window}, among **{subj}**, split by when they first completed **{anc}** "
            f"relative to their first qualifying activity in that month: {detail}. "
            f"Anchor timing uses the first **{anc}** event in the stream; if **users** carries a single "
            "onboarding date in your warehouse, that is often the preferred source once it is wired in."
        )

    # Scalar result
    if len(df) == 1:
        return _scalar_summary_agent(df, qo, metric_name)

    # Time series: build rule-based summary, then prepend LLM "so what?" headline
    date_cols = [c for c in df.columns if c in ("day", "week", "month", "date", "period")]
    if not date_cols:
        date_cols = [c for c in df.columns if _is_temporal_col(df, c)]
    if date_cols:
        ts_summary = _build_time_series_summary(df, qo=qo)
        if ts_summary:
            from core.pipeline.activation_window import (
                incomplete_activation_cohort_note,
                incomplete_retention_cohort_note,
            )
            maturity = incomplete_activation_cohort_note(df, qo)
            maturity = maturity or incomplete_retention_cohort_note(df, qo)
            base = f"**{label}** ({period}): {ts_summary}"
            if maturity:
                base = f"{base}\n\n{maturity}"
            headline = _analyst_so_what(prompt, df, qo, period)
            return f"{headline}\n\n{base}" if headline else base

    # Segment: describe top values, prepend "so what?" headline
    metric_col = _preferred_metric_column(df)
    cat_cols   = df.select_dtypes("object").columns.tolist()
    if metric_col and cat_cols:
        top_rows = df.nlargest(3, metric_col)
        top_str  = ", ".join(
            f"{row[cat_cols[0]]} ({_fmt_period_value(row[metric_col])})"
            for _, row in top_rows.iterrows()
        )
        base = f"**{label}** — top segments: {top_str}."
        headline = _analyst_so_what(prompt, df, qo, period)
        return f"{headline}\n\n{base}" if headline else base

    # LLM fallback for anything else
    preview = df.head(6).to_string(index=False)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise senior analyst. Summarise this result in 1-2 sentences. "
                "Lead with the single most important number and what it implies for the business. "
                "Use plain language. No hedging. No bullet points."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {prompt}\nMetric: {label}\nPeriod: {period}\n\n{preview}"
            ),
        },
    ]
    return ask_llm(messages, model=LLM_FAST, temperature=0.2, call_site="pipeline.small_narration") or f"**{label}** result ready."


# ── Main SQL generation entry point ──────────────────────────────────────────

def get_sql(
    prompt: str,
    *,
    hypothesis_doc=None,
) -> tuple[str, Optional[str], Optional[str], object]:
    """
    Full pipeline: NL question → (sql, metric_name, clarify_msg, qo).

    Reads catalog / sampled_values / metrics from st.session_state.

    sql return values:
        "__diagnose__"  → route to diagnose pipeline
        "__analyst__"   → route to investigate()
        ""              → clarify / unresolvable
        <sql string>    → simple_query path
    """
    catalog       = st.session_state.get("catalog", {})
    sampled       = st.session_state.get("sampled_values", {})
    metrics       = st.session_state.get("metrics", [])
    session_id    = st.session_state.get("session_id", "")
    history = get_qo_history(session_id, limit=5) if session_id else []

    # Clarify context from last turn
    clarify_ctx = _extract_clarify_context(history)

    # Orchestrate: NL → QueryObject (api_key=None → make_llm_client picks up active provider key)
    qo = orchestrate(
        question=prompt,
        catalog=catalog,
        sampled_values=sampled,
        openai_api_key=None,
        history=history,
        hypothesis_doc=hypothesis_doc,
        corrections=([{"question": prompt, "previous_answer": clarify_ctx}]
                     if clarify_ctx else None),
    )

    # Attach debug metadata used by debug_panel.py
    setattr(qo, "_debug_user_prompt",         prompt)
    setattr(qo, "_debug_orchestrator_input",  prompt)
    setattr(qo, "_hypothesis_generated_debug", hypothesis_doc is not None)
    setattr(qo, "_clarify_ctx_injected_debug", clarify_ctx is not None)
    _store_pre_override_debug(qo)

    # Attempt to resolve "clarify" as a display-modifier follow-up
    # (e.g. "can you show percentage split" after a segment query) before
    # bailing. If successful, qo.analysis_type changes to the inherited type.
    _maybe_resolve_clarify_as_followup(qo, history, catalog, prompt)

    if qo.analysis_type in ("clarify", "out_of_scope"):
        msg = qo.clarify_message or "Could you rephrase that?"
        setattr(qo, "_debug_flow", [f"orchestrate → {qo.analysis_type}"])
        return "", None, msg, qo

    from core.pipeline.normalize_query import normalize_query_object

    normalize_query_object(
        qo,
        prompt=prompt,
        history=history,
        catalog=catalog,
        sampled_values=sampled,
        metrics=metrics,
    )

    # Structural validation — catches missing required fields (e.g. event=null for stickiness)
    # after all fixups have run. Returns a clarify message rather than silently compiling
    # with wrong SQL (e.g. the "app_opened" fallback in stickiness/lifecycle/journey/xyz).
    _qo_ok, _qo_err = qo.is_valid()
    if not _qo_ok:
        _clarify = f"I need a bit more info to answer that — {_qo_err}. Could you clarify?"
        setattr(qo, "_debug_flow", [
            f"orchestrate → {qo.analysis_type}",
            f"validation failed: {_qo_err}",
        ])
        return "", None, _clarify, qo

    # Resolver policy
    decision = resolve_query_policy(prompt, qo, catalog, sampled)
    setattr(qo, "_resolver_decision",        decision)
    setattr(qo, "_comparison_intent_debug",  getattr(decision, "comparison_intent", False))

    # Policy arbiter (optional — controlled by session flag)
    use_arbiter = st.session_state.get("flag_use_policy_arbiter", DEFAULT_USE_POLICY_ARBITER)
    if use_arbiter:
        arbiter = arbitrate(decision, qo, prompt)
        if not arbiter.allow_execute and arbiter.clarify_message:
            setattr(qo, "_debug_flow", [
                f"orchestrate → {qo.analysis_type}",
                f"resolver → {decision.route}",
                "arbiter → blocked",
            ])
            return "", None, arbiter.clarify_message, qo

    # Custom event routes
    bctx          = catalog.get("__business_context__", {}) or {}
    custom_events = bctx.get("custom_events", []) or []
    route         = getattr(decision, "route", "orchestrator")
    matched_ces   = getattr(decision, "matched_custom_events", []) or []

    sql          = ""
    metric_name  = None

    from core.sql.compilers import (
        compile_custom_event_split,
        compile_custom_event_single,
        compile_custom_event_segment,
        compile_custom_event_retention,
    )

    if route == "custom_split" and matched_ces:
        ce_a = matched_ces[0]
        ce_b = matched_ces[1] if len(matched_ces) > 1 else None
        sql = compile_custom_event_split(ce_a, ce_b, qo)
        metric_name = (
            f"{_ce_label(ce_a)} vs {_ce_label(ce_b)}"
            if ce_b
            else _ce_label(ce_a)
        )
        setattr(qo, "_executed_custom_event_name", ce_a.get("name"))
    elif route == "custom_single" and matched_ces:
        ce = matched_ces[0]
        if qo.analysis_type == "behavioral_cohort":
            # custom_single resolver matched the event, but behavioral_cohort needs
            # event_b exclusion logic — fall through to the dedicated compiler.
            sql, metric_name = compile_query(qo, metrics)
        else:
            sql         = compile_custom_event_single(ce, qo)
            metric_name = _ce_label(ce)
            setattr(qo, "_executed_custom_event_name", ce.get("name"))
    elif route == "custom_segment" and matched_ces:
        ce          = matched_ces[0]
        sql         = compile_custom_event_segment(ce, qo)
        metric_name = _ce_label(ce)
        setattr(qo, "_executed_custom_event_name", ce.get("name"))
    elif route == "custom_retention" and matched_ces:
        ce          = matched_ces[0]
        sql         = compile_custom_event_retention(ce, qo)
        metric_name = _ce_label(ce)
        setattr(qo, "_executed_custom_event_name", ce.get("name"))
    else:
        sql, metric_name = compile_query(qo, metrics)

    # Resolver custom_* routes compile trend SQL even when the orchestrator chose
    # diagnose — chat only runs structural diagnosis when sql == "__diagnose__".
    if str(getattr(qo, "analysis_type", "") or "").strip().lower() == "diagnose":
        if sql and str(sql).strip() not in ("", "__analyst__"):
            sql = "__diagnose__"

    flow = [
        f"orchestrate → {qo.analysis_type}",
        f"resolver → {route}",
        f"compiler → {'__diagnose__' if sql == '__diagnose__' else '__analyst__' if sql == '__analyst__' else 'SQL'}",
    ]
    setattr(qo, "_debug_flow", flow)

    return sql, metric_name, None, qo


# ── Private helpers ───────────────────────────────────────────────────────────

def _ce_label(ce: dict) -> str:
    name = (ce.get("name") or "").strip()
    return name.replace("_", " ").title() if name else "Custom Event"


def _store_pre_override_debug(qo) -> None:
    setattr(qo, "_qo_pre_override_debug", {
        "analysis_type":  getattr(qo, "analysis_type", None),
        "metric_id":      getattr(qo, "metric_id",     None),
        "event":          getattr(qo, "event",          None),
        "time_granularity": getattr(qo, "time_granularity", None),
        "time_range_days":  getattr(qo, "time_range_days",  None),
        "date_from":      getattr(qo, "date_from",      None),
        "date_to":        getattr(qo, "date_to",        None),
        "breakdown":      getattr(qo, "breakdown",      None),
        "funnel_steps":   list(getattr(qo, "funnel_steps", None) or []),
    })
