"""
Runtime Result Validator
------------------------
Checks whether SQL output shape matches intended analysis type.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import pandas as pd


@dataclass
class RuntimeValidation:
    ok: bool
    reason: str
    clarify_message: str | None = None


def _is_numeric_series(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s)


def _recipe_rule_gate(qo, df: pd.DataFrame) -> RuntimeValidation | None:
    """
    Optional recipe-level guardrails attached on qo._active_recipe_validation_rules.
    Expected shape is a dict with optional keys:
      - required_filters: {"is_test": False, ...}
      - wau_max: <int>
      - enforce_week_start_labels: true/false
    """
    rules = getattr(qo, "_active_recipe_validation_rules", None) or {}
    if not isinstance(rules, dict) or not rules:
        return None

    # 1) Required filters
    req = rules.get("required_filters") or {}
    if isinstance(req, dict) and req:
        qf = getattr(qo, "filters", None) or {}
        missing = []
        for k, v in req.items():
            if k not in qf or qf.get(k) != v:
                missing.append(f"{k}={v}")
        if missing:
            return RuntimeValidation(
                False,
                "recipe_required_filters_missing",
                "This recipe requires filters that were not applied: "
                + ", ".join(missing)
                + ".",
            )

    # 2) WAU sanity bound
    if "wau_max" in rules and df is not None and not df.empty:
        try:
            wau_max = int(rules["wau_max"])
        except Exception:
            wau_max = None
        if wau_max and wau_max > 0:
            metric_id = str(getattr(qo, "metric_id", "") or "").lower()
            metric_name = str(getattr(qo, "_active_metric_name", "") or "").lower()
            if "wau" in metric_id or "weekly active users" in metric_name:
                num_cols = [c for c in df.columns if _is_numeric_series(df[c])]
                if num_cols:
                    m = pd.to_numeric(df[num_cols[0]], errors="coerce").max()
                    if pd.notna(m) and float(m) > float(wau_max):
                        return RuntimeValidation(
                            False,
                            "recipe_wau_bound_exceeded",
                            f"This result exceeded the configured WAU upper bound ({wau_max:,}).",
                        )

    # 3) Week label formatting guard
    if bool(rules.get("enforce_week_start_labels")) and df is not None and not df.empty:
        gran = str(getattr(qo, "time_granularity", "") or "").lower()
        if gran == "week":
            time_candidates = [c for c in df.columns if any(k in c.lower() for k in ("week", "date", "period"))]
            if time_candidates:
                tc = time_candidates[0]
                vals = df[tc].dropna().astype(str).head(20).tolist()
                if any(re.search(r"\bW\d{1,2}\b", v) for v in vals):
                    return RuntimeValidation(
                        False,
                        "recipe_week_label_invalid",
                        "Weekly outputs must use week-start dates (e.g., 'Week of Jan 20, 2026'), not ISO week codes.",
                    )
    return None


def validate_result_shape(qo, df: pd.DataFrame) -> RuntimeValidation:
    if df is None:
        return RuntimeValidation(False, "df_none", "I couldn't get a valid result for this query.")
    if df.empty:
        return RuntimeValidation(True, "empty_result")

    rr = _recipe_rule_gate(qo, df)
    if rr is not None:
        return rr

    cols = list(df.columns)
    num_cols = [c for c in cols if _is_numeric_series(df[c])]
    analysis_type = getattr(qo, "analysis_type", "")

    if analysis_type == "metric":
        if len(num_cols) >= 1:
            return RuntimeValidation(True, "metric_shape_ok")
        return RuntimeValidation(False, "metric_shape_mismatch", "I may have interpreted the metric incorrectly. Could you restate the metric and period?")

    if analysis_type == "segment":
        breakdown = getattr(qo, "breakdown", None)
        if breakdown and breakdown in cols and len(num_cols) >= 1:
            return RuntimeValidation(True, "segment_shape_ok")
        return RuntimeValidation(False, "segment_shape_mismatch", "I expected a segment breakdown but the result shape didn't match. Which dimension should I split by?")

    if analysis_type == "same_month_anchor":
        if "cohort_month_alignment" in cols and len(num_cols) >= 1:
            return RuntimeValidation(True, "same_month_anchor_shape_ok")
        return RuntimeValidation(False, "same_month_anchor_shape_mismatch", "I couldn't build the onboarding-vs-activity month split. Check events and time range.")

    if analysis_type == "retention":
        has_pct = any("retention" in c.lower() and "pct" in c.lower() for c in cols)
        has_matrix = any(c.startswith("cohort_") for c in cols) and "period_idx" in cols
        if has_pct or has_matrix:
            return RuntimeValidation(True, "retention_shape_ok")
        return RuntimeValidation(False, "retention_shape_mismatch", "I couldn't produce a retention-shaped result. Do you want D1, D7, or D30 retention?")

    if analysis_type == "funnel":
        if "step_name" in cols and any(c in cols for c in ("users", "pct_of_top")):
            return RuntimeValidation(True, "funnel_shape_ok")
        return RuntimeValidation(False, "funnel_shape_mismatch", "I couldn't produce a funnel result. Please share the step sequence you want.")

    if analysis_type == "funnel_compare":
        if "step_name" in cols and "curr_n" in cols and "prev_n" in cols:
            return RuntimeValidation(True, "funnel_compare_shape_ok")
        return RuntimeValidation(False, "funnel_compare_shape_mismatch", "I couldn't compare funnel steps across periods. Check the funnel steps and time range.")

    if analysis_type == "journey":
        if "next_event" in cols and any(c in cols for c in ("transitions", "users")):
            return RuntimeValidation(True, "journey_shape_ok")
        if "next_event" in cols and "transitions" in cols:
            return RuntimeValidation(True, "journey_shape_ok")
        return RuntimeValidation(False, "journey_shape_mismatch", "I couldn't build a path view. Which starting event should we use?")

    if analysis_type == "behavioral_cohort":
        gran = (getattr(qo, "time_granularity", None) or "day").lower()
        if gran in ("week", "month"):
            time_col = "month" if gran == "month" else "week"
            if time_col in cols and "users" in cols and len(df) >= 1:
                return RuntimeValidation(True, "behavioral_cohort_trend_ok")
            return RuntimeValidation(
                False,
                "behavioral_cohort_trend_mismatch",
                "I expected a month-by-month (or week-by-week) count of non-converters.",
            )
        if (
            len(df) == 1
            and "did_a_users" in cols
            and "also_did_b" in cols
            and "never_did_b" in cols
        ):
            return RuntimeValidation(True, "behavioral_cohort_overlap_scalar_ok")
        if "users" in cols and len(df) == 1:
            return RuntimeValidation(True, "behavioral_cohort_scalar_ok")
        if "users" in cols:
            return RuntimeValidation(True, "behavioral_cohort_shape_ok")
        return RuntimeValidation(False, "behavioral_cohort_shape_mismatch", "I couldn't size this behavioral cohort.")

    # Default: permissive
    return RuntimeValidation(True, "shape_check_skipped")

