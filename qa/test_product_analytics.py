"""
Product analytics regression suite.

Two layers:
  1. Compiler unit tests  — construct QueryObjects directly, assert SQL structure.
     No LLM, no DB, runs offline.
  2. Orchestrator integration tests — send natural-language questions, assert routing
     + SQL content. Requires a valid LLM provider key and catalog/DB.

Run:
    python qa/test_product_analytics.py              # unit tests only
    python qa/test_product_analytics.py --integration  # needs LLM_PROVIDER + key in .env
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.sql.compilers import (
    compile_query,
    set_global_sql_guards,
    _compile_metric,
    _compile_segment,
    _compile_user_lifecycle,
    _compile_funnel,
    _compile_retention,
)
from core.sql.query_object import QueryObject

set_global_sql_guards(["user_id NOT LIKE 'test_%'"])

# ── Helpers ────────────────────────────────────────────────────────────────────

def where_clause(sql: str) -> str:
    """Extract the WHERE clause body (between WHERE and GROUP/ORDER/LIMIT)."""
    m = re.search(r'\bWHERE\b(.*?)(?:\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b)', sql, re.DOTALL | re.I)
    return m.group(1).strip() if m else ""


def cte_names(sql: str) -> list[str]:
    return re.findall(r'\b(\w+)\s+AS\s*\(', sql, re.I)


@dataclass
class Case:
    name: str
    qo: QueryObject
    assertions: list[tuple[str, str]]   # (description, callable or expression)
    compile_fn: Optional[callable] = None   # override compile function


@dataclass
class OrchestratorCase:
    question: str
    history: list[dict] = field(default_factory=list)
    expect_analysis_type: Optional[str] = None
    expect_metric_variant: Optional[str] = None
    expect_filters_keys: list[str] = field(default_factory=list)
    expect_metric_status_col: Optional[str] = None
    expect_metric_status_target: Optional[str] = None
    expect_threshold: Optional[int] = None
    # All needle checks use plain case-insensitive substring matching (no regex).
    sql_must_contain: list[str] = field(default_factory=list)
    sql_must_not_contain: list[str] = field(default_factory=list)
    # Same but scoped to just the WHERE clause body.
    where_must_not_contain: list[str] = field(default_factory=list)
    route_must_be: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1: COMPILER UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════

PASS = []
FAIL = []

def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append(f"{name}: {detail}")


# ── Group 1: Status filter always goes to WHERE (non-rate variants) ────────────

def test_status_in_where_for_all_non_rate_variants():
    base = dict(
        analysis_type="metric", event="transaction_reconciled",
        metric_status_col="transaction_status", metric_status_target="SUCCESS",
        filters={"transaction_channel": "UPI"},
        date_from="2026-01-01", date_to="2026-02-01",
    )
    for variant in [None, "per_user_count", "per_user_value", "event_count", "threshold_user_count"]:
        d = {**base, "metric_variant": variant}
        if variant == "per_user_value":
            d["metric_value_col"] = "amount"
        if variant == "threshold_user_count":
            d["threshold"] = "5"
        qo = QueryObject.from_dict(d)
        sql = _compile_metric(qo)
        wc = where_clause(sql)
        check(
            f"status_in_where:{variant}",
            "transaction_status = 'SUCCESS'" in wc,
            f"WHERE missing status filter. WHERE={wc!r}",
        )
        check(
            f"channel_in_where:{variant}",
            "transaction_channel = 'UPI'" in wc,
            f"WHERE missing channel filter. WHERE={wc!r}",
        )


# ── Group 2: status_rate keeps status OUT of WHERE, in CASE WHEN ──────────────

def test_status_rate_case_when_only():
    qo = QueryObject.from_dict({
        "analysis_type": "segment",
        "event": "transaction_reconciled",
        "metric_variant": "status_rate",
        "metric_status_col": "transaction_status",
        "metric_status_target": "SUCCESS",
        "breakdown": "platform",
        "date_from": "2026-01-01", "date_to": "2026-02-01",
    })
    sql = _compile_segment(qo)
    wc = where_clause(sql)
    check("rate_status_not_in_where",
          "transaction_status" not in wc,
          f"status_rate: status leaked into WHERE. WHERE={wc!r}")
    check("rate_status_in_case_when",
          "CASE WHEN transaction_status" in sql,
          "status_rate: CASE WHEN missing from SELECT")


# ── Group 2b: status_rate in _compile_metric (time-series path) ───────────────

def test_status_rate_metric_compile():
    """_compile_metric must produce CASE WHEN for status_rate, not plain count."""
    qo = QueryObject.from_dict({
        "analysis_type": "metric",
        "event": "transaction_reconciled",
        "metric_variant": "status_rate",
        "metric_status_col": "transaction_status",
        "metric_status_target": "SUCCESS",
        "filters": {"transaction_channel": "UPI"},
        "date_from": "2026-01-01", "date_to": "2026-02-01",
    })
    sql = _compile_metric(qo)
    wc  = where_clause(sql)
    check("metric_rate_case_when",
          "CASE WHEN transaction_status" in sql,
          "status_rate metric: missing CASE WHEN in SELECT")
    check("metric_rate_not_in_where",
          "transaction_status" not in wc,
          f"status_rate metric: transaction_status leaked into WHERE: {wc!r}")
    check("metric_rate_channel_in_where",
          "transaction_channel = 'UPI'" in wc,
          "status_rate metric: channel filter missing from WHERE")
    check("metric_rate_has_pct_col",
          "_rate_pct" in sql,
          "status_rate metric: missing _rate_pct column alias")


# ── Group 3: threshold_user_count SQL structure ────────────────────────────────

def test_threshold_sql_structure():
    qo = QueryObject.from_dict({
        "analysis_type": "metric",
        "event": "transaction_reconciled",
        "metric_variant": "threshold_user_count",
        "metric_status_col": "transaction_status",
        "metric_status_target": "SUCCESS",
        "filters": {"transaction_channel": "UPI"},
        "date_from": "2026-01-01", "date_to": "2026-02-01",
        "threshold": "5",
    })
    sql = _compile_metric(qo)
    check("threshold_has_per_user_cte",   "per_user" in sql.lower(),           "missing per_user CTE")
    check("threshold_has_event_count",    "event_count" in sql,                "missing event_count column")
    check("threshold_has_n_check",        "event_count > 5" in sql,            "missing >5 check")
    check("threshold_labels_power_users", "Power Users" in sql,                "missing Power Users label")
    check("threshold_has_pct",            "pct_of_total" in sql,               "missing pct_of_total")
    check("threshold_status_in_where",    "transaction_status = 'SUCCESS'" in where_clause(sql),
          "status filter missing from inner WHERE")
    check("threshold_channel_in_where",   "transaction_channel = 'UPI'" in where_clause(sql),
          "channel filter missing from inner WHERE")


# ── Group 4: per_user_count gives COUNT(*)/users, not SUM(amount)/users ───────

def test_per_user_count_vs_value():
    base = dict(
        analysis_type="metric", event="transaction_reconciled",
        date_from="2026-01-01", date_to="2026-02-01",
        metric_status_col="transaction_status", metric_status_target="SUCCESS",
    )
    qo_count = QueryObject.from_dict({**base, "metric_variant": "per_user_count"})
    qo_value = QueryObject.from_dict({**base, "metric_variant": "per_user_value", "metric_value_col": "amount"})

    sql_count = _compile_metric(qo_count)
    sql_value = _compile_metric(qo_value)

    check("per_user_count_uses_count_star",  "COUNT(*)" in sql_count,           "per_user_count: missing COUNT(*)")
    check("per_user_count_no_sum_amount",    "SUM(amount)" not in sql_count,    "per_user_count: incorrectly uses SUM(amount)")
    check("per_user_count_col_alias",        "events_per_user" in sql_count,    "per_user_count: wrong col alias")
    check("per_user_value_uses_sum",         "SUM(amount)" in sql_value,        "per_user_value: missing SUM(amount)")
    check("per_user_value_no_count_star",    "COUNT(*)" not in sql_value.split("SUM")[0],  "per_user_value: COUNT(*) before SUM")


# ── Group 5: user_lifecycle SQL structure ──────────────────────────────────────

def test_lifecycle_sql_structure():
    qo = QueryObject.from_dict({
        "analysis_type": "user_lifecycle",
        "event": "transaction_reconciled",
        "filters": {"transaction_status": "SUCCESS"},
        "date_from": "2026-01-01", "date_to": "2026-02-01",
    })
    sql = _compile_user_lifecycle(qo)
    ctenames = [c.lower() for c in cte_names(sql)]

    check("lifecycle_cohort_cte",      "cohort" in ctenames,          "missing cohort CTE")
    check("lifecycle_activity_cte",    "all_activity" in ctenames,    "missing all_activity CTE")
    check("lifecycle_ref_cte",         "ref" in ctenames,             "missing ref CTE")
    check("lifecycle_uses_max_ts",     "MAX(timestamp)" in sql,       "recency ref not MAX(timestamp)")
    check("lifecycle_no_current_date_in_stage",
          "CURRENT_DATE" not in sql.split("ref AS")[1] if "ref AS" in sql else True,
          "CURRENT_DATE used in stage classification (should use ref_date)")
    # 5 THEN + 1 ELSE = 6 stage labels
    import re as _re
    stage_labels = _re.findall(r"(?:THEN|ELSE)\s+'[^']+'", sql)
    check("lifecycle_has_6_stages",    len(stage_labels) >= 6,        f"expected 6 stage labels, got {len(stage_labels)}: {stage_labels}")
    # all_activity must NOT have an event_name = predicate (comment mentioning "event_name" is fine)
    all_activity_section = sql.split("all_activity AS")[1].split("ref AS")[0] if "all_activity AS" in sql else ""
    check("lifecycle_all_activity_no_event_filter",
          "event_name =" not in all_activity_section and "event_name='" not in all_activity_section,
          "all_activity CTE should not filter by event_name (needs all events for recency)")
    # status filter in cohort CTE WHERE
    cohort_section = sql.split("all_activity AS")[0] if "all_activity AS" in sql else sql
    check("lifecycle_status_in_cohort",
          "transaction_status = 'SUCCESS'" in cohort_section,
          "status filter missing from cohort CTE")


# ── Group 6: funnel SQL structure ──────────────────────────────────────────────

def test_funnel_sql_structure():
    qo = QueryObject.from_dict({
        "analysis_type": "funnel",
        "funnel_steps": ["app_opened", "onboarding_completed", "transaction_reconciled"],
        "date_from": "2026-01-01", "date_to": "2026-02-01",
        "filters": {"transaction_status": "SUCCESS"},
    })
    sql = _compile_funnel(qo)
    check("funnel_has_3_step_ctes",  "step_0" in sql and "step_1" in sql and "step_2" in sql, "missing step CTEs")
    check("funnel_has_pct_of_top",   "pct_of_top" in sql,      "missing pct_of_top column")
    check("funnel_has_step_cvr",     "step_cvr" in sql,        "missing step_cvr column")
    check("funnel_orders_by_step",   "ORDER BY step_num" in sql, "missing ORDER BY step_num")


# ── Group 7: retention SQL structure ──────────────────────────────────────────

def test_retention_sql_structure():
    qo = QueryObject.from_dict({
        "analysis_type": "retention",
        "event": "transaction_reconciled",
        "event_b": "transaction_reconciled",
        "retention_window_days": 7,
        "date_from": "2026-01-01", "date_to": "2026-02-01",
    })
    sql = _compile_retention(qo)
    check("retention_has_cohort_cte",   "cohort" in sql.lower(),       "missing cohort CTE")
    check("retention_has_retained_cte", "retained" in sql.lower(),     "missing retained CTE")
    check("retention_pct",              "retention_pct" in sql,        "missing retention_pct column")
    check("retention_cohort_week",      "cohort_week" in sql,          "missing cohort_week")


# ── Group 8: segment breakdown column ─────────────────────────────────────────

def test_segment_uses_breakdown():
    qo = QueryObject.from_dict({
        "analysis_type": "segment",
        "event": "transaction_reconciled",
        "breakdown": "city",
        "date_from": "2026-01-01", "date_to": "2026-02-01",
        "filters": {"transaction_status": "SUCCESS"},
    })
    sql = _compile_segment(qo)
    check("segment_groups_by_city",  "city" in sql.lower(),        "breakdown column missing from SQL")
    check("segment_status_in_where", "transaction_status = 'SUCCESS'" in where_clause(sql),
          "status filter missing from segment WHERE")


# ── Group 9: QueryObject.to_dict() completeness ───────────────────────────────

def test_to_dict_captures_all_filter_fields():
    qo = QueryObject.from_dict({
        "analysis_type": "metric",
        "event": "transaction_reconciled",
        "metric_variant": "per_user_count",
        "metric_status_col": "transaction_status",
        "metric_status_target": "SUCCESS",
        "metric_value_col": "amount",
        "filters": {"transaction_channel": "UPI"},
        "date_from": "2026-01-01", "date_to": "2026-02-01",
        "threshold": "5",
        "event_b": "onboarding_completed",
        "breakdown": "platform",
    })
    d = qo.to_dict()
    for field_name in ["metric_status_col", "metric_status_target", "metric_variant",
                       "metric_value_col", "threshold", "event_b", "breakdown", "filters"]:
        check(f"to_dict_{field_name}", field_name in d and d[field_name] is not None,
              f"to_dict() missing or null for field {field_name!r}")
    check("to_dict_threshold_is_int", d.get("threshold") == 5, f"threshold should be int 5, got {d.get('threshold')!r}")


# ── Group 10: guards clause alias correctness in JOIN contexts ─────────────────

def test_guards_clause_alias_in_lifecycle_join():
    qo = QueryObject.from_dict({
        "analysis_type": "user_lifecycle",
        "event": "transaction_reconciled",
        "date_from": "2026-01-01", "date_to": "2026-02-01",
    })
    sql = _compile_user_lifecycle(qo)
    # Inside all_activity JOIN, the guard must be prefixed with 'e.'
    all_activity_section = ""
    if "all_activity AS" in sql:
        start = sql.index("all_activity AS")
        end = sql.index("ref AS") if "ref AS" in sql else len(sql)
        all_activity_section = sql[start:end]
    check("lifecycle_guard_alias_in_join",
          "e.user_id NOT LIKE" in all_activity_section,
          "guard in JOIN context must use 'e.user_id' alias to avoid DuckDB ambiguous reference")


# ── Group 11: analysis types that must route to __analyst__ ───────────────────

def test_analyst_only_routing():
    analyst_only_types = ["user_lifecycle", "stickiness", "funnel_property_drilldown",
                          "xyz_matrix", "retention", "journey", "demographic_breakdown"]
    for at in analyst_only_types:
        qo_d = {"analysis_type": at, "event": "app_opened",
                "date_from": "2026-01-01", "date_to": "2026-02-01"}
        if at == "funnel_property_drilldown":
            qo_d["funnel_steps"] = ["app_opened", "transaction_reconciled"]
            qo_d["breakdown"] = "platform"
        if at == "retention":
            qo_d["event_b"] = "app_opened"
        qo = QueryObject.from_dict(qo_d)
        sql, _ = compile_query(qo, metrics=[])
        check(f"analyst_only:{at}", sql == "__analyst__",
              f"expected __analyst__, got {sql!r}")


# ── Run all unit tests ─────────────────────────────────────────────────────────

def run_unit_tests():
    print("=" * 64)
    print("LAYER 1: Compiler unit tests")
    print("=" * 64)
    test_status_in_where_for_all_non_rate_variants()
    test_status_rate_case_when_only()
    test_status_rate_metric_compile()
    test_threshold_sql_structure()
    test_per_user_count_vs_value()
    test_lifecycle_sql_structure()
    test_funnel_sql_structure()
    test_retention_sql_structure()
    test_segment_uses_breakdown()
    test_to_dict_captures_all_filter_fields()
    test_guards_clause_alias_in_lifecycle_join()
    test_analyst_only_routing()

    print(f"\nPass: {len(PASS)}  Fail: {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL  {f}")
    return len(FAIL) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2: ORCHESTRATOR INTEGRATION TESTS (requires LLM_PROVIDER + key in .env)
# ═══════════════════════════════════════════════════════════════════════════════

ORCHESTRATOR_CASES: list[OrchestratorCase] = [
    # ── Correct analysis type routing ─────────────────────────────────────────
    OrchestratorCase(
        question="Show user lifecycle stages for January transacting users",
        expect_analysis_type="user_lifecycle",
        sql_must_contain=["__analyst__"],
    ),
    OrchestratorCase(
        question="How sticky is the product — show DAU/MAU",
        expect_analysis_type="stickiness",
        sql_must_contain=["__analyst__"],
    ),
    OrchestratorCase(
        question="Show the onboarding funnel conversion rates step by step",
        expect_analysis_type="funnel",
    ),
    OrchestratorCase(
        question="Why did DAU drop last month",
        expect_analysis_type="diagnose",
    ),
    OrchestratorCase(
        question="Users who installed but never transacted",
        expect_analysis_type="behavioral_cohort",
    ),
    OrchestratorCase(
        question="How long does it take from signup to first transaction",
        expect_analysis_type="time_between",
    ),
    OrchestratorCase(
        question="What do users do after their first successful transaction",
        expect_analysis_type="journey",
    ),

    # ── Status filter vs rate ──────────────────────────────────────────────────
    OrchestratorCase(
        question="How many successful UPI transactions in January",
        expect_analysis_type="metric",
        expect_filters_keys=["transaction_channel"],
        sql_must_contain=["transaction_status = 'SUCCESS'"],
        sql_must_not_contain=["CASE WHEN transaction_status"],
    ),
    OrchestratorCase(
        question="What is the UPI transaction success rate in January",
        expect_analysis_type="metric",
        expect_metric_status_col="transaction_status",
        expect_metric_status_target="SUCCESS",
        # Status must appear in CASE WHEN, never in the WHERE clause.
        sql_must_contain=["CASE WHEN transaction_status"],
        where_must_not_contain=["transaction_status"],
    ),

    # ── Metric variant selection ───────────────────────────────────────────────
    OrchestratorCase(
        question="Avg transactions per user in January",
        expect_analysis_type="metric",
        expect_metric_variant="per_user_count",
        sql_must_contain=["COUNT(*)", "events_per_user"],
        sql_must_not_contain=["SUM(amount)"],
    ),
    OrchestratorCase(
        question="Avg spend per user in January",
        expect_analysis_type="metric",
        expect_metric_variant="per_user_value",
        sql_must_contain=["SUM(amount)"],
    ),
    OrchestratorCase(
        question="How many users did more than 5 successful UPI transactions in Jan",
        expect_analysis_type="metric",
        expect_metric_variant="threshold_user_count",
        expect_threshold=5,
        sql_must_contain=["event_count > 5", "Power Users", "transaction_status = 'SUCCESS'"],
    ),

    # ── Follow-up filter inheritance ───────────────────────────────────────────
    OrchestratorCase(
        question="Break it down by platform",
        history=[{
            "question": "How many successful UPI transactions in January",
            "qo": {
                "analysis_type": "metric",
                "event": "transaction_reconciled",
                "metric_variant": None,
                "metric_status_col": "transaction_status",
                "metric_status_target": "SUCCESS",
                "filters": {"transaction_channel": "UPI"},
                "date_from": "2026-01-01",
                "date_to": "2026-02-01",
                "time_range_days": 30,
                "time_granularity": "month",
                "time_source": "explicit",
            },
        }],
        expect_analysis_type="segment",
        sql_must_contain=["transaction_status = 'SUCCESS'", "transaction_channel = 'UPI'"],
    ),
    OrchestratorCase(
        question="Now show avg per user",
        history=[{
            "question": "How many successful UPI transactions in January",
            "qo": {
                "analysis_type": "metric",
                "event": "transaction_reconciled",
                "metric_variant": None,
                "metric_status_col": "transaction_status",
                "metric_status_target": "SUCCESS",
                "filters": {"transaction_channel": "UPI"},
                "date_from": "2026-01-01",
                "date_to": "2026-02-01",
                "time_range_days": 30,
                "time_granularity": "month",
                "time_source": "explicit",
            },
        }],
        expect_analysis_type="metric",
        expect_metric_variant="per_user_count",
        sql_must_contain=["transaction_status = 'SUCCESS'", "transaction_channel = 'UPI'"],
        sql_must_not_contain=["SUM(amount)"],
    ),

    # ── Time window rules ──────────────────────────────────────────────────────
    OrchestratorCase(
        question="Show DAU MOM for Jan to March",
        expect_analysis_type="metric",
        sql_must_contain=["2026-01-01", "2026-04-01"],
    ),
    OrchestratorCase(
        question="D30 retention for January cohort",
        expect_analysis_type="retention",
    ),

    # ── Demographic & breakdown queries ───────────────────────────────────────
    OrchestratorCase(
        question="Which city had the most users transacting in January",
        expect_analysis_type="segment",
        sql_must_contain=["city"],
    ),
    OrchestratorCase(
        question="Show user demographics for transacting users in Jan",
        expect_analysis_type="demographic_breakdown",
    ),

    # ── Tricky edge cases ──────────────────────────────────────────────────────
    OrchestratorCase(
        question="Users who transacted in March but not in February",
        expect_analysis_type="behavioral_cohort",
    ),
    OrchestratorCase(
        question="Show the XYZ matrix for transactions by platform",
        expect_analysis_type="xyz_matrix",
    ),
    OrchestratorCase(
        question="Compare the onboarding funnel this month vs last month",
        expect_analysis_type="funnel_compare",
    ),
]


def run_orchestrator_tests(catalog: dict, sampled: dict, metrics: list[dict]):
    from core.pipeline.orchestrator import orchestrate
    from core.semantic.resolver_policy import resolve_query_policy
    from core.agents.policy_arbiter import arbitrate

    print("\n" + "=" * 64)
    print("LAYER 2: Orchestrator integration tests")
    print("=" * 64)

    orch_pass = []
    orch_fail = []

    for case in ORCHESTRATOR_CASES:
        try:
            from ui.qo_fixups import _apply_followup_context_repair, _inherit_metric_status_and_filters
            qo = orchestrate(
                question=case.question,
                catalog=catalog,
                sampled_values=sampled,
                openai_api_key=None,  # make_llm_client resolves key from LLM_PROVIDER
                history=case.history or [],
            )
            # Apply the same post-orchestration fixups as pipeline.get_sql()
            _apply_followup_context_repair(qo, case.history or [], catalog)
            _inherit_metric_status_and_filters(qo, case.history or [], case.question)
            decision = resolve_query_policy(case.question, qo, catalog, sampled)
            arb = arbitrate(decision, qo, case.question)

            failures = []

            if case.expect_analysis_type and qo.analysis_type != case.expect_analysis_type:
                failures.append(
                    f"analysis_type: expected {case.expect_analysis_type!r}, got {qo.analysis_type!r}"
                )
            if case.expect_metric_variant and qo.metric_variant != case.expect_metric_variant:
                failures.append(
                    f"metric_variant: expected {case.expect_metric_variant!r}, got {qo.metric_variant!r}"
                )
            if case.expect_metric_status_col and qo.metric_status_col != case.expect_metric_status_col:
                failures.append(
                    f"metric_status_col: expected {case.expect_metric_status_col!r}, got {qo.metric_status_col!r}"
                )
            if case.expect_metric_status_target and qo.metric_status_target != case.expect_metric_status_target:
                failures.append(
                    f"metric_status_target: expected {case.expect_metric_status_target!r}, got {qo.metric_status_target!r}"
                )
            if case.expect_threshold is not None and qo.threshold != case.expect_threshold:
                failures.append(
                    f"threshold: expected {case.expect_threshold}, got {qo.threshold!r}"
                )
            for fkey in case.expect_filters_keys:
                if fkey not in (qo.filters or {}):
                    failures.append(f"filters: expected key {fkey!r}, got {qo.filters}")

            if case.route_must_be and decision.route != case.route_must_be:
                failures.append(f"route: expected {case.route_must_be!r}, got {decision.route!r}")

            # Compile and check SQL content
            if case.sql_must_contain or case.sql_must_not_contain or case.where_must_not_contain:
                if decision.route == "custom_split":
                    from core.sql.compilers import compile_custom_event_split
                    mce = decision.matched_custom_events or []
                    sql = compile_custom_event_split(mce[0], mce[1] if len(mce) > 1 else None, qo)
                else:
                    sql, _ = compile_query(qo, metrics)

                sql_lower = sql.lower()
                for needle in case.sql_must_contain:
                    if needle.lower() not in sql_lower:
                        failures.append(f"sql missing: {needle!r}  (sql={sql[:200]!r})")
                for needle in case.sql_must_not_contain:
                    if needle.lower() in sql_lower:
                        failures.append(f"sql contains forbidden: {needle!r}")
                if case.where_must_not_contain:
                    wc = where_clause(sql).lower()
                    for needle in case.where_must_not_contain:
                        if needle.lower() in wc:
                            failures.append(f"WHERE clause contains forbidden: {needle!r}  (WHERE={wc[:150]!r})")

            label = f"Q: {case.question[:60]}"
            if failures:
                orch_fail.append((label, failures))
                print(f"  FAIL  {label}")
                for f in failures:
                    print(f"          {f}")
            else:
                orch_pass.append(label)
                print(f"  OK    {label}")

        except Exception as e:
            orch_fail.append((case.question[:60], [str(e)]))
            print(f"  ERR   {case.question[:60]}: {e}")

    print(f"\nOrchestrator pass: {len(orch_pass)}  fail: {len(orch_fail)}")
    return len(orch_fail) == 0


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    unit_ok = run_unit_tests()

    if "--integration" not in sys.argv:
        print("\n(Skipping orchestrator tests. Pass --integration to run them.)")
        sys.exit(0 if unit_ok else 1)

    # Verify the active provider has a key configured
    try:
        from core.infra.llm import _active_provider, _get_api_key
        if not _get_api_key():
            env_var = _active_provider().get("api_key_env", "API_KEY")
            print(f"\nERROR: {env_var} required for --integration tests (LLM_PROVIDER={os.environ.get('LLM_PROVIDER','groq')})")
            sys.exit(1)
    except Exception as e:
        print(f"\nERROR: LLM config invalid: {e}")
        sys.exit(1)

    import duckdb
    CATALOG_PATH = ROOT / "catalog.json"
    DB_PATH = ROOT / "jupiter.duckdb"

    catalog = json.loads(CATALOG_PATH.read_text())
    sampled: dict = {}
    if DB_PATH.exists():
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).df()["table_name"].tolist()
            skip = {"user_id", "session_id", "event_id", "transaction_id"}
            for tname in tables:
                sampled[tname] = {}
                for _, row in conn.execute(f"DESCRIBE {tname}").df().iterrows():
                    cname, ctype = row["column_name"], row["column_type"]
                    if ctype != "VARCHAR" or cname in skip:
                        continue
                    try:
                        vals = conn.execute(
                            f"SELECT DISTINCT {cname} FROM {tname} WHERE {cname} IS NOT NULL LIMIT 20"
                        ).df()[cname].tolist()
                        sampled[tname][cname] = [str(v) for v in vals if v]
                    except Exception:
                        pass
        finally:
            conn.close()

    metrics = []
    for tname, tdata in catalog.items():
        if tname.startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []):
            metrics.append({
                "id": m.get("id", ""), "name": m.get("name", ""),
                "description": m.get("description", ""),
                "sql": m.get("sql_hint", "") or m.get("sql", ""),
            })

    orch_ok = run_orchestrator_tests(catalog, sampled, metrics)
    sys.exit(0 if (unit_ok and orch_ok) else 1)


if __name__ == "__main__":
    main()
