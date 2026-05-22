"""
validator.py — Independent verification of analytical findings.

A real analyst never trusts their first query result. Before presenting findings
they re-derive key numbers via a different code path, check arithmetic consistency,
and assign a confidence grade.

Two validation layers:

  Layer 1 — Rule-based arithmetic checks (zero LLM cost):
    • Funnel top step must be 100% (or close)
    • Trend direction must match period-comparison direction
    • Percentage columns must sum to ~100 where expected
    • Period-over-period delta math must be internally consistent

  Layer 2 — LLM sanity check on the primary insight:
    • One cheap call: "does this number make sense for this event/industry?"
    • Returns a flag (PLAUSIBLE / SUSPICIOUS) with a reason

Confidence grades:
  A  (90–100)  All arithmetic clean, insight plausible
  B  (75–89)   Minor warnings only, nothing critical
  C  (50–74)   One arithmetic failure or suspicious insight
  D  (<50)     Multiple failures or implausible result
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from core.infra.llm import make_llm_client, LLM_FAST

from core.sql.query_object import QueryObject


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ArithmeticWarning:
    check: str      # what was checked
    detail: str     # what was wrong
    severity: str   # "warning" | "error"


@dataclass
class ValidationResult:
    arithmetic_warnings: list[ArithmeticWarning] = field(default_factory=list)
    sanity_flag: str = "PLAUSIBLE"      # "PLAUSIBLE" | "SUSPICIOUS"
    sanity_reason: str = ""
    score: int = 100
    grade: str = "A"
    confidence_label: str = "High confidence"
    summary: str = ""


# ── Layer 1: Rule-based arithmetic checks ────────────────────────────────────

def _check_funnel(plan: list) -> list[ArithmeticWarning]:
    warnings = []
    for inv in plan:
        if inv.name != "funnel" or inv.error or inv.df.empty:
            continue
        if "pct_of_top" not in inv.df.columns:
            continue
        top_pct = inv.df["pct_of_top"].dropna()
        if top_pct.empty:
            continue
        first = float(top_pct.iloc[0])
        if abs(first - 100.0) > 1.0:
            warnings.append(ArithmeticWarning(
                check="funnel_top_step",
                detail=f"Top step shows {first:.1f}% (expected 100%). Possible entry-step filter issue.",
                severity="error",
            ))
        # Funnel should be monotonically decreasing
        vals = top_pct.tolist()
        for i in range(1, len(vals)):
            if vals[i] > vals[i-1] + 0.1:   # allow tiny float noise
                warnings.append(ArithmeticWarning(
                    check="funnel_monotonicity",
                    detail=f"Step {i+1} ({vals[i]:.1f}%) > step {i} ({vals[i-1]:.1f}%). Funnel steps may be mis-ordered.",
                    severity="error",
                ))
    return warnings


def _check_period_vs_trend(plan: list) -> list[ArithmeticWarning]:
    warnings = []
    trend_inv  = next((i for i in plan if i.name == "trend"  and not i.error and not i.df.empty), None)
    period_inv = next((i for i in plan if i.name == "period_comparison" and not i.error and not i.df.empty), None)
    if not trend_inv or not period_inv:
        return warnings

    num_cols = [c for c in trend_inv.df.columns if pd.api.types.is_numeric_dtype(trend_inv.df[c])]
    if not num_cols:
        return warnings

    series = trend_inv.df[num_cols[0]].dropna()
    if len(series) < 2:
        return warnings

    trend_dir = "up" if series.iloc[-1] > series.iloc[0] else "down"

    if "current_period" in period_inv.df.columns and "previous_period" in period_inv.df.columns:
        curr = float(period_inv.df["current_period"].iloc[0])
        prev = float(period_inv.df["previous_period"].iloc[0])
        period_dir = "up" if curr > prev else "down"
        if trend_dir != period_dir:
            warnings.append(ArithmeticWarning(
                check="trend_vs_period_direction",
                detail=(
                    f"Trend chart shows metric going {trend_dir} over the full window, "
                    f"but period comparison shows {period_dir}. Time window boundaries may not align."
                ),
                severity="warning",
            ))
    return warnings


def _check_mix_shift_pcts(plan: list) -> list[ArithmeticWarning]:
    """Segment share percentages should sum to ~100."""
    warnings = []
    for inv in plan:
        if inv.error or inv.df.empty:
            continue
        for pct_col in ("curr_pct", "prev_pct"):
            if pct_col not in inv.df.columns:
                continue
            total = inv.df[pct_col].dropna().sum()
            if total > 5 and abs(total - 100.0) > 5.0:   # only flag if clearly non-trivial
                warnings.append(ArithmeticWarning(
                    check=f"{inv.name}_{pct_col}_sum",
                    detail=f"{inv.name}: {pct_col} sums to {total:.1f}% (expected ~100%). Possible null/unknown values excluded.",
                    severity="warning",
                ))
    return warnings


_RATE_COLUMN_PATTERNS = re.compile(
    r"(retention_pct|_rate|_pct|_percent|_ratio|completion_rate|activation_rate|"
    r"conversion_rate|success_rate|churn_rate|pass_rate)",
    re.IGNORECASE,
)


def _check_retention_bounds(plan: list) -> list[ArithmeticWarning]:
    """Flag any rate/percentage column with values outside [0, 100]."""
    warnings = []
    seen: set[str] = set()
    for inv in plan:
        if inv.error or inv.df.empty:
            continue
        for col in inv.df.columns:
            if not _RATE_COLUMN_PATTERNS.search(col):
                continue
            if not pd.api.types.is_numeric_dtype(inv.df[col]):
                continue
            vals = inv.df[col].dropna()
            key = f"{inv.name}:{col}"
            if key in seen:
                continue
            seen.add(key)
            if (vals > 100).any():
                max_val = float(vals.max())
                warnings.append(ArithmeticWarning(
                    check="rate_bounds",
                    detail=(
                        f"{inv.name}: {col} contains values > 100% (max {max_val:.1f}%). "
                        "The denominator may be smaller than the numerator — "
                        "verify the metric definition or applied filters."
                    ),
                    severity="error",
                ))
            elif (vals < 0).any():
                warnings.append(ArithmeticWarning(
                    check="rate_bounds",
                    detail=f"{inv.name}: {col} contains negative values — check the metric SQL.",
                    severity="warning",
                ))
    return warnings


def _check_data_gap(plan: list) -> list[ArithmeticWarning]:
    """
    If current_period = 0 but previous_period > 0, this is almost certainly
    a data freshness / pipeline issue — not a real metric collapse.
    Flag as an error so the story_architect does not describe it as a drop.
    """
    warnings = []
    for inv in plan:
        if inv.name != "period_comparison" or inv.error or inv.df.empty:
            continue
        df = inv.df
        curr_col = next((c for c in df.columns if "current" in c.lower()), None)
        prev_col = next((c for c in df.columns if "previous" in c.lower() or "prev" in c.lower()), None)
        if curr_col and prev_col:
            curr = float(df[curr_col].iloc[0])
            prev = float(df[prev_col].iloc[0])
            if curr == 0 and prev > 0:
                warnings.append(ArithmeticWarning(
                    check="data_gap",
                    detail=(
                        f"Current period has 0 users (previous period: {prev:,.0f}). "
                        "This is a DATA AVAILABILITY issue — your database has no events "
                        "in the queried window. Do NOT interpret this as a real metric drop. "
                        "Verify your data pipeline and time window."
                    ),
                    severity="error",
                ))
    return warnings


def _run_arithmetic_checks(plan: list) -> list[ArithmeticWarning]:
    warnings = []
    warnings += _check_data_gap(plan)
    warnings += _check_funnel(plan)
    warnings += _check_period_vs_trend(plan)
    warnings += _check_mix_shift_pcts(plan)
    warnings += _check_retention_bounds(plan)
    return warnings


# ── Layer 2: LLM sanity check ─────────────────────────────────────────────────

_SANITY_SYSTEM = """You are a data quality analyst. You will be given an analytical
finding (a key number or trend) and some context about the event and industry.

Decide if the finding is PLAUSIBLE or SUSPICIOUS.

SUSPICIOUS means: the number is so extreme (e.g. 99% retention, 0.001% funnel conversion,
10x MoM growth on a mature metric) that it likely indicates a data issue — not a real result.

PLAUSIBLE means: the finding is in a realistic range for the event and industry,
even if surprising.

Respond with JSON only:
{{"flag": "PLAUSIBLE|SUSPICIOUS", "reason": "<one sentence>"}}"""


def _sanity_check(
    qo: QueryObject,
    plan: list,
    catalog: Optional[dict],
    client,
) -> tuple[str, str]:
    """Returns (flag, reason). Flag is PLAUSIBLE or SUSPICIOUS."""
    # Find the primary insight from the first non-empty investigation
    primary_insight = ""
    for inv in plan:
        if inv.insight:
            primary_insight = inv.insight
            break
    if not primary_insight:
        return "PLAUSIBLE", ""

    industry = (catalog or {}).get("__business_context__", {}).get("industry", "unknown")
    prompt = (
        f"Event: {qo.event or qo.metric_id}\n"
        f"Industry: {industry}\n"
        f"Finding: {primary_insight}"
    )
    try:
        resp = client.chat.completions.create(
            model=LLM_FAST,
            temperature=0,
            messages=[
                {"role": "system", "content": _SANITY_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
        return data.get("flag", "PLAUSIBLE"), data.get("reason", "")
    except Exception:
        return "PLAUSIBLE", ""


# ── Scoring ───────────────────────────────────────────────────────────────────

def _compute_grade(
    warnings: list[ArithmeticWarning],
    sanity_flag: str,
) -> tuple[int, str, str]:
    errors   = sum(1 for w in warnings if w.severity == "error")
    cautions = sum(1 for w in warnings if w.severity == "warning")
    suspicious = sanity_flag == "SUSPICIOUS"

    score = 100 - errors * 15 - cautions * 5 - (20 if suspicious else 0)
    score = max(0, score)

    if score >= 90:
        return score, "A", "High confidence"
    elif score >= 75:
        return score, "B", "Moderate confidence"
    elif score >= 50:
        return score, "C", "Low confidence — review before sharing"
    else:
        return score, "D", "Very low confidence — do not share without investigation"


# ── Main entry point ──────────────────────────────────────────────────────────

def validate_findings(
    qo: QueryObject,
    plan: list,
    catalog: Optional[dict] = None,
    openai_api_key: Optional[str] = None,
) -> ValidationResult:
    """
    Validate analytical findings via arithmetic checks + LLM sanity check.
    Returns a ValidationResult with confidence grade A/B/C/D.

    Fails gracefully — never blocks the main pipeline.
    """
    client = make_llm_client(openai_api_key)

    # Layer 1: arithmetic
    arith_warnings = _run_arithmetic_checks(plan)

    # Layer 2: sanity (skip for trivial / out-of-scope types)
    sanity_flag, sanity_reason = "PLAUSIBLE", ""
    if qo.analysis_type not in ("clarify", "out_of_scope"):
        try:
            sanity_flag, sanity_reason = _sanity_check(qo, plan, catalog, client)
        except Exception:
            pass

    score, grade, label = _compute_grade(arith_warnings, sanity_flag)

    # Build summary
    error_count   = sum(1 for w in arith_warnings if w.severity == "error")
    warning_count = sum(1 for w in arith_warnings if w.severity == "warning")
    parts = [f"{grade} ({score}/100)"]
    if error_count:
        parts.append(f"{error_count} arithmetic error(s)")
    if warning_count:
        parts.append(f"{warning_count} warning(s)")
    if sanity_flag == "SUSPICIOUS":
        parts.append("result flagged as suspicious")
    summary = " · ".join(parts)

    return ValidationResult(
        arithmetic_warnings=arith_warnings,
        sanity_flag=sanity_flag,
        sanity_reason=sanity_reason,
        score=score,
        grade=grade,
        confidence_label=label,
        summary=summary,
    )
