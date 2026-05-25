"""
evals/sql/run.py — Component-isolated eval: compiler (text-to-SQL) layer.

Tests: given a canonical QueryObject (known-good orchestrator output), does the compiler
produce structurally correct SQL?

Does NOT require: LLM, DuckDB.
Requires: catalog.json only.

Usage:
    python -m qa.evals.sql.run
    python -m qa.evals.sql.run --output /tmp/sql_run.json
Results saved to: qa/results/sql/YYYYMMDD_HHMMSS.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.evals.shared.production import (
    apply_post_orchestrator_fixups,
    build_metrics_like_chat,
    configure_sql_guards_from_catalog,
)
from qa.evals.shared.benchmark import choose_sql, load_catalog_and_sampled, score_gold_sql

OUT_DIR = ROOT / "qa" / "results" / "sql"
from core.sql.query_object import QueryObject
from core.semantic.resolver_policy import resolve_query_policy


@dataclass
class TextToSQLCase:
    label: str
    question: str                       # natural-language description (used by resolver + fixups)
    qo_kwargs: dict[str, Any]           # canonical post-orchestrator QO fields
    gold_sql: dict[str, Any] | None     # structural assertions (required / forbidden / *_in_cte)
    # For specialist analysis types (retention, funnel, journey, stickiness, user_lifecycle)
    # that compile to __analyst__ or __diagnose__ — assert on the route, not on SQL patterns.
    expected_route: str | None = None
    tags: list[str] = field(default_factory=list)


def build_text_to_sql_suite() -> list[TextToSQLCase]:
    """
    Canonical QOs covering key compiler invariants.
    No LLM required — each QO is the expected orchestrator output for the given question.
    Failures here = compiler or fixup bug, not orchestrator issue.
    """
    return [
        # ── Activation: CTE structure ─────────────────────────────────────────
        TextToSQLCase(
            label="activation_scalar_cte",
            question="what is activation rate",
            qo_kwargs={"analysis_type": "metric", "metric_id": "activation_rate", "time_range_days": 90},
            gold_sql={"required": ["WITH denom_cohort", "numer_events", "LEFT JOIN numer_events"]},
            tags=["activation", "pct_users_cte"],
        ),
        TextToSQLCase(
            label="activation_upi_filter_in_numerator_only",
            question="what is UPI activation rate",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "filters": {"transaction_channel": "UPI"},
                "time_range_days": 90,
            },
            gold_sql={
                "required": ["WITH denom_cohort", "transaction_channel = 'UPI'"],
                # UPI filter must NOT appear in the denominator cohort.
                "forbidden_in_cte": {"denom_cohort": ["transaction_channel"]},
            },
            tags=["activation", "filter_in_numerator"],
        ),
        TextToSQLCase(
            label="activation_mom_groups_by_cohort_month",
            question="show activation rate MOM for last 6 months",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "time_granularity": "month",
                "time_range_days": 180,
            },
            gold_sql={
                "required": ["WITH denom_cohort", "numer_events", "cohort_month", "GROUP BY 1"],
            },
            tags=["activation", "monthly_trend", "pct_users_cte"],
        ),
        TextToSQLCase(
            label="activation_calendar_filter_no_bracket_literal",
            question="show activation rate for Jan",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "date_from": "2026-01-01",
                "date_to": "2026-02-01",
            },
            gold_sql={"forbidden": ["AND date =", "date = '['"]},
            tags=["activation", "filter_rendering", "calendar"],
        ),
        TextToSQLCase(
            label="activation_48h_window_explicit",
            question="show 48 hour UPI activation",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "filters": {"transaction_channel": "UPI"},
                "activation_window_days": 2,
                "activation_window_days_source": "explicit",
                "time_range_days": 90,
            },
            gold_sql={
                "required": ["WITH denom_cohort", "transaction_channel = 'UPI'"],
                "forbidden_in_cte": {"denom_cohort": ["transaction_channel"]},
            },
            tags=["activation", "filter_in_numerator", "hours_window"],
        ),
        # ── Behavioral cohort: filter scoping ─────────────────────────────────
        TextToSQLCase(
            label="behavioral_cohort_filter_in_did_b_not_did_a",
            question="How many users did onboarding and UPI transactions in Feb",
            qo_kwargs={
                "analysis_type": "behavioral_cohort",
                "event": "onboarding_completed",
                "event_b": "transaction_reconciled",
                "filters": {"transaction_channel": "UPI"},
                "date_from": "2026-02-01",
                "date_to": "2026-03-01",
            },
            gold_sql={
                "forbidden_in_cte": {"did_a": ["transaction_channel"]},
                "required_in_cte":  {"did_b": ["transaction_channel"]},
            },
            tags=["behavioral_cohort", "filter_scoping"],
        ),
        TextToSQLCase(
            label="anti_cohort_left_join_null_guard",
            question="How many users did onboarding and no UPI transactions in Feb",
            qo_kwargs={
                "analysis_type": "behavioral_cohort",
                "event": "onboarding_completed",
                "event_b": "transaction_reconciled",
                "metric_variant": "anti_cohort",
                "filters": {"transaction_channel": "UPI"},
                "date_from": "2026-02-01",
                "date_to": "2026-03-01",
            },
            gold_sql={
                "required": ["WHERE db.user_id IS NULL"],
                "forbidden_in_cte": {"did_a": ["transaction_channel"]},
                "required_in_cte":  {"did_b": ["transaction_channel"]},
            },
            tags=["behavioral_cohort", "anti_cohort"],
        ),
        # ── Segment: list filter must not render as Python repr ────────────────
        TextToSQLCase(
            label="segment_list_filter_no_bracket_literal",
            question="show transacting users by platform for iOS and Android last month",
            qo_kwargs={
                "analysis_type": "segment",
                "event": "transaction_reconciled",
                "breakdown": "platform",
                "filters": {"platform": "iOS"},
                "time_range_days": 30,
            },
            gold_sql={"forbidden": ["= '['", "= \"['"]},
            tags=["segment", "filter_rendering", "list_filter"],
        ),
        # ── Specialist routes: stickiness / user_lifecycle → __analyst__ ─────
        # These analysis types always compile to __analyst__ (not direct SQL).
        # Assert on routing, not on SQL patterns — the analyst layer is tested separately.
        TextToSQLCase(
            label="stickiness_routes_to_analyst",
            question="how sticky is our product",
            qo_kwargs={"analysis_type": "stickiness", "time_range_days": 30},
            gold_sql=None,
            expected_route="__analyst__",
            tags=["stickiness", "routing"],
        ),
        TextToSQLCase(
            label="user_lifecycle_routes_to_analyst",
            question="show user lifecycle stages",
            qo_kwargs={"analysis_type": "user_lifecycle", "time_range_days": 90},
            gold_sql=None,
            expected_route="__analyst__",
            tags=["user_lifecycle", "routing"],
        ),
        # ── Activation by platform: breakdown in segment path ─────────────────
        TextToSQLCase(
            label="activation_by_platform_segment_path",
            question="show activation rate by platform for last 90 days",
            qo_kwargs={
                "analysis_type": "segment",
                "metric_id": "activation_rate",
                "breakdown": "platform",
                "time_range_days": 90,
            },
            gold_sql={
                "required": ["WITH denom_cohort", "platform"],
                "forbidden": ["= '['"],
            },
            tags=["activation", "segment", "breakdown"],
        ),
        # ── DAU: basic metric time series ─────────────────────────────────────
        # DAU compiles to direct SQL (not __analyst__). Assert it groups by date
        # and doesn't include any list-literal rendering artifacts.
        TextToSQLCase(
            label="dau_trend_groups_by_date",
            question="show DAU for last month",
            qo_kwargs={"analysis_type": "metric", "metric_id": "dau", "time_range_days": 30},
            gold_sql={"required": ["GROUP BY", "date"]},
            tags=["metric", "time_series", "dau"],
        ),
        # ── Segment: transacting users by platform ────────────────────────────
        TextToSQLCase(
            label="transacting_by_platform_has_breakdown_col",
            question="show transacting users by platform for Jan",
            qo_kwargs={
                "analysis_type": "segment",
                "event": "transaction_reconciled",
                "breakdown": "platform",
                "date_from": "2026-01-01",
                "date_to": "2026-02-01",
            },
            gold_sql={"required": ["platform"], "forbidden": ["= '['"]},
            tags=["segment", "breakdown"],
        ),
        # ── Retention: all route to __analyst__ ───────────────────────────────
        # Retention is always compiled by the analyst layer — the compiler never
        # emits raw SQL for retention queries. Assert on routing only.
        TextToSQLCase(
            label="d7_retention_routes_to_analyst",
            question="show D7 retention",
            qo_kwargs={"analysis_type": "retention", "metric_id": "d7_retention", "time_range_days": 90},
            gold_sql=None,
            expected_route="__analyst__",
            tags=["retention", "routing"],
        ),
        TextToSQLCase(
            label="d30_retention_routes_to_analyst",
            question="show D30 retention",
            qo_kwargs={"analysis_type": "retention", "metric_id": "d30_retention", "time_range_days": 90},
            gold_sql=None,
            expected_route="__analyst__",
            tags=["retention", "routing"],
        ),
        TextToSQLCase(
            label="retention_by_platform_routes_to_analyst",
            question="show D7 retention by platform",
            qo_kwargs={
                "analysis_type": "retention",
                "metric_id": "d7_retention",
                "breakdown": "platform",
                "time_range_days": 90,
            },
            gold_sql=None,
            expected_route="__analyst__",
            tags=["retention", "routing", "breakdown"],
        ),
        TextToSQLCase(
            label="retention_mom_routes_to_analyst",
            question="share MOM retention",
            qo_kwargs={
                "analysis_type": "retention",
                "metric_id": "d7_retention",
                "time_granularity": "month",
                "time_range_days": 90,
            },
            gold_sql=None,
            expected_route="__analyst__",
            tags=["retention", "routing", "monthly_trend"],
        ),
        # ── Funnel → __analyst__ ─────────────────────────────────────────────
        TextToSQLCase(
            label="funnel_routes_to_analyst",
            question="show onboarding funnel for Jan",
            qo_kwargs={
                "analysis_type": "funnel",
                "event": "onboarding_completed",
                "date_from": "2026-01-01",
                "date_to": "2026-02-01",
            },
            gold_sql=None,
            expected_route="__analyst__",
            tags=["funnel", "routing"],
        ),
        # ── Journey → __analyst__ ────────────────────────────────────────────
        TextToSQLCase(
            label="journey_routes_to_analyst",
            question="show user journey",
            qo_kwargs={"analysis_type": "journey", "time_range_days": 30},
            gold_sql=None,
            expected_route="__analyst__",
            tags=["journey", "routing"],
        ),
        # ── Time-between → __analyst__ ───────────────────────────────────────
        TextToSQLCase(
            label="time_between_routes_to_analyst",
            question="time between onboarding completed and transaction reconciled",
            qo_kwargs={
                "analysis_type": "time_between",
                "event": "onboarding_completed",
                "event_b": "transaction_reconciled",
                "time_range_days": 90,
            },
            gold_sql=None,
            expected_route="__analyst__",
            tags=["time_between", "routing"],
        ),
        # ── Diagnose → __diagnose__ ──────────────────────────────────────────
        TextToSQLCase(
            label="diagnose_routes_to_diagnose",
            question="why did transacting users drop last month",
            qo_kwargs={"analysis_type": "diagnose", "time_range_days": 30},
            gold_sql=None,
            expected_route="__diagnose__",
            tags=["diagnose", "routing"],
        ),
    ]


def compile_canonical(
    case: TextToSQLCase,
    catalog: dict,
    sampled: dict,
    metrics: list[dict],
) -> str | None:
    """
    Build a QueryObject from canonical kwargs, run fixups, resolve route, compile SQL.
    Returns the SQL string (or __analyst__ / __diagnose__ for specialist routes).
    Exported for use by eval_business_correctness.
    """
    qo = QueryObject(**case.qo_kwargs)
    apply_post_orchestrator_fixups(
        qo,
        question=case.question,
        catalog=catalog,
        sampled=sampled,
        metrics=metrics,
        history=[],
    )
    decision = resolve_query_policy(case.question, qo, catalog, sampled)
    sql, _ = choose_sql(decision, qo, metrics)
    return sql


def run_text_to_sql_eval(
    catalog: dict,
    sampled: dict,
    metrics: list[dict],
) -> dict[str, Any]:
    cases = build_text_to_sql_suite()
    results = []

    for case in cases:
        try:
            sql = compile_canonical(case, catalog, sampled, metrics)
            actual_route = str(sql or "").strip()
            is_specialist = actual_route in ("__analyst__", "__diagnose__")

            # Cases with expected_route assert on routing, not on SQL structure.
            if case.expected_route is not None:
                route_match = (actual_route == case.expected_route)
                results.append({
                    "label": case.label,
                    "question": case.question,
                    "tags": case.tags,
                    "qo_kwargs": case.qo_kwargs,
                    "compiled": False,
                    "expected_route": case.expected_route,
                    "actual_route": actual_route,
                    "sql_score": 1.0 if route_match else 0.0,
                    "sql_detail": {"route_match": route_match},
                    "pass": route_match,
                })
                continue

            if not sql or is_specialist or sql == "":
                results.append({
                    "label": case.label,
                    "question": case.question,
                    "tags": case.tags,
                    "qo_kwargs": case.qo_kwargs,
                    "compiled": False,
                    "specialist_route": actual_route if is_specialist else None,
                    "sql_score": 0.0,
                    "sql_detail": {"note": f"no testable SQL — got: {actual_route!r}"},
                    "pass": False,
                })
                continue

            proxy = type("C", (), {"gold_sql": case.gold_sql})()
            sql_s, sql_d = score_gold_sql(sql, proxy)
            results.append({
                "label": case.label,
                "question": case.question,
                "tags": case.tags,
                "qo_kwargs": case.qo_kwargs,
                "compiled": True,
                "sql_score": round(sql_s, 3),
                "sql_detail": sql_d,
                "sql_snippet": sql,
                "pass": sql_s >= 1.0,
            })
        except Exception as e:
            results.append({
                "label": case.label,
                "question": case.question,
                "tags": case.tags,
                "qo_kwargs": case.qo_kwargs,
                "compiled": False,
                "error": f"{type(e).__name__}: {e}",
                "sql_score": 0.0,
                "pass": False,
            })

    n = len(results)
    return {
        "component": "text_to_sql",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_cases": n,
        "pass_count": sum(1 for r in results if r.get("pass")),
        "pass_rate": round(sum(1 for r in results if r.get("pass")) / max(n, 1), 3),
        "avg_sql_score": round(sum(r.get("sql_score", 0.0) for r in results) / max(n, 1), 3),
        "cases": results,
    }


def _print_report(report: dict) -> None:
    print(f"\n{'='*58}")
    print("  TEXT-TO-SQL EVAL  (compiler layer, no LLM required)")
    print(f"{'='*58}")
    print(f"  Cases      : {report['n_cases']}")
    print(f"  Pass rate  : {report['pass_rate']:.1%}  ({report['pass_count']}/{report['n_cases']})")
    print(f"  Avg SQL score: {report['avg_sql_score']:.3f}")
    failures = [r for r in report["cases"] if not r.get("pass")]
    if failures:
        print(f"\n  Failures ({len(failures)}):")
        for r in failures:
            print(f"    [{r['label']}]  {r['question'][:60]}")
            if r.get("error"):
                print(f"      error: {r['error'][:100]}")
            detail = r.get("sql_detail") or {}
            for chk, d in detail.items():
                if isinstance(d, dict) and not d.get("match", True):
                    note = f"found={d.get('found_in_cte', d.get('found', '?'))}"
                    print(f"      FAIL  {chk}  ({note})")
    else:
        print("  All cases passed.")
    print(f"{'='*58}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Text-to-SQL component eval — no LLM or DB required."
    )
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    catalog, sampled = load_catalog_and_sampled()
    configure_sql_guards_from_catalog(catalog)
    metrics = build_metrics_like_chat(catalog)

    report = run_text_to_sql_eval(catalog, sampled, metrics)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / (args.output or f"t2s_eval_{stamp}.json")
    out.write_text(json.dumps(report, indent=2, default=str))

    _print_report(report)
    print(f"Saved to: {out}")


if __name__ == "__main__":
    main()
