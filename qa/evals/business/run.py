"""
evals/business/run.py — Component-isolated eval: result semantics layer.

Tests: given a canonical QueryObject, does the executed result make business sense?
  - Rate columns in [0, 100]
  - Expected columns present
  - Row count in expected range
  - No NULL values in key numeric columns

Does NOT require: LLM.
Requires: catalog.json, jupiter.duckdb.

Usage:
    python -m qa.evals.business.run
    python -m qa.evals.business.run --output /tmp/bc_run.json
Results saved to: qa/results/business/YYYYMMDD_HHMMSS.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.evals.shared.production import (
    apply_post_orchestrator_fixups,
    build_metrics_like_chat,
    configure_sql_guards_from_catalog,
)
from qa.evals.shared.benchmark import choose_sql, load_catalog_and_sampled, score_gold_result
from qa.evals.sql.run import TextToSQLCase, compile_canonical

OUT_DIR = ROOT / "qa" / "results" / "business"
from core.sql.query_object import QueryObject
from core.semantic.resolver_policy import resolve_query_policy

DB_PATH = ROOT / "jupiter.duckdb"


@dataclass
class BusinessCorrectnessCase:
    label: str
    question: str
    qo_kwargs: dict[str, Any]
    gold_result: dict[str, Any]     # col_bounds, row_count_min/max, col_present, col_not_null
    tags: list[str] = field(default_factory=list)


def build_business_correctness_suite() -> list[BusinessCorrectnessCase]:
    """
    Canonical QOs with result-value assertions.
    Each case executes against the real DB and checks that the output is semantically correct.
    Failures here mean: SQL ran without error but returned wrong/nonsensical values.
    """
    return [
        # ── Activation rate: rate must be in [0, 100], right columns ──────────
        BusinessCorrectnessCase(
            label="activation_scalar_rate_in_bounds",
            question="what is activation rate",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "time_range_days": 90,
            },
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "row_count_max": 1,
                "col_present": [
                    "onboarding_completed_users",
                    "transaction_reconciled_users_30d",
                    "activation_rate_30d",
                ],
            },
            tags=["activation", "bounds", "scalar"],
        ),
        BusinessCorrectnessCase(
            label="activation_upi_rate_in_bounds",
            question="what is UPI activation rate",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "filters": {"transaction_channel": "UPI"},
                "time_range_days": 90,
            },
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "row_count_max": 1,
            },
            tags=["activation", "bounds", "filter"],
        ),
        BusinessCorrectnessCase(
            label="activation_mom_columns_and_bounds",
            question="show activation rate MOM for last 6 months",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "time_granularity": "month",
                "time_range_days": 180,
            },
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "col_present": ["month", "activation_rate_30d"],
                "col_not_null": ["activation_rate_30d"],
            },
            tags=["activation", "trend", "columns", "non_null"],
        ),
        BusinessCorrectnessCase(
            label="activation_by_platform_rate_and_dim",
            question="show activation rate by platform",
            qo_kwargs={
                "analysis_type": "segment",
                "metric_id": "activation_rate",
                "breakdown": "platform",
                "time_range_days": 90,
            },
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "col_present": ["platform", "activation_rate_30d"],
            },
            tags=["activation", "breakdown", "bounds"],
        ),
        # ── DAU: time-series result with non-null values ───────────────────────
        BusinessCorrectnessCase(
            label="dau_trend_non_empty_non_null",
            question="show DAU for last month",
            qo_kwargs={
                "analysis_type": "metric",
                "metric_id": "dau",
                "time_range_days": 30,
            },
            gold_result={
                "row_count_min": 7,          # at least a week of data in any real dataset
                "col_present": ["date", "value"],   # DAU metric SQL aliases count as "value"
                "col_not_null": ["value"],
            },
            tags=["dau", "trend", "non_null"],
        ),
        # ── Behavioral cohort: scalar count, finite result ─────────────────────
        BusinessCorrectnessCase(
            label="behavioral_cohort_overlap_scalar",
            question="How many users did onboarding and UPI transactions in Feb",
            qo_kwargs={
                "analysis_type": "behavioral_cohort",
                "event": "onboarding_completed",
                "event_b": "transaction_reconciled",
                "filters": {"transaction_channel": "UPI"},
                "date_from": "2026-02-01",
                "date_to": "2026-03-01",
            },
            gold_result={
                "row_count_min": 1,
                "row_count_max": 1,   # cohort overlap is a single count, not a time series
            },
            tags=["behavioral_cohort", "scalar"],
        ),
        BusinessCorrectnessCase(
            label="anti_cohort_scalar",
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
            gold_result={
                "row_count_min": 1,
                "row_count_max": 1,
            },
            tags=["behavioral_cohort", "anti_cohort", "scalar"],
        ),
        # ── Segment: breakdown produces multiple rows, dimension column present ─
        BusinessCorrectnessCase(
            label="transacting_by_platform_has_dim_column",
            question="show transacting users by platform for Jan",
            qo_kwargs={
                "analysis_type": "segment",
                "event": "transaction_reconciled",
                "breakdown": "platform",
                "date_from": "2026-01-01",
                "date_to": "2026-02-01",
            },
            gold_result={
                "row_count_min": 1,
                "col_present": ["platform"],
            },
            tags=["segment", "breakdown", "columns"],
        ),
    ]


def run_business_correctness_eval(
    catalog: dict,
    sampled: dict,
    metrics: list[dict],
    conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    cases = build_business_correctness_suite()
    results = []

    for case in cases:
        # Reuse the text-to-sql compile path via a proxy TextToSQLCase.
        proxy = TextToSQLCase(
            label=case.label,
            question=case.question,
            qo_kwargs=case.qo_kwargs,
            gold_sql=None,
        )
        try:
            sql = compile_canonical(proxy, catalog, sampled, metrics)
            is_specialist = str(sql or "").strip() in ("__analyst__", "__diagnose__")

            if not sql or is_specialist or sql == "":
                results.append({
                    "label": case.label,
                    "question": case.question,
                    "tags": case.tags,
                    "qo_kwargs": case.qo_kwargs,
                    "sql_compiled": False,
                    "specialist_route": sql if is_specialist else None,
                    "execution_ok": False,
                    "result_score": 0.0,
                    "result_detail": {"note": f"no executable SQL — got: {sql!r}"},
                    "pass": False,
                })
                continue

            try:
                df = conn.execute(sql).df()
                exec_ok = True
                exec_err = ""
            except Exception as e:
                df = pd.DataFrame()
                exec_ok = False
                exec_err = f"{type(e).__name__}: {e}"

            if not exec_ok:
                results.append({
                    "label": case.label,
                    "question": case.question,
                    "tags": case.tags,
                    "qo_kwargs": case.qo_kwargs,
                    "sql_snippet": sql,
                    "sql_compiled": True,
                    "execution_ok": False,
                    "execution_error": exec_err,
                    "result_score": 0.0,
                    "result_detail": {"execution_error": exec_err},
                    "pass": False,
                })
                continue

            proxy_case = type("C", (), {"gold_result": case.gold_result})()
            result_s, result_d = score_gold_result(df, proxy_case)
            results.append({
                "label": case.label,
                "question": case.question,
                "tags": case.tags,
                "qo_kwargs": case.qo_kwargs,
                "sql_snippet": sql,
                "sql_compiled": True,
                "execution_ok": True,
                "row_count": len(df),
                "columns": list(df.columns),
                "result_score": round(result_s, 3),
                "result_detail": result_d,
                "pass": result_s >= 1.0,
            })

        except Exception as e:
            results.append({
                "label": case.label,
                "question": case.question,
                "tags": case.tags,
                "error": f"{type(e).__name__}: {e}",
                "result_score": 0.0,
                "pass": False,
            })

    n = len(results)
    return {
        "component": "business_correctness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_cases": n,
        "pass_count": sum(1 for r in results if r.get("pass")),
        "pass_rate": round(sum(1 for r in results if r.get("pass")) / max(n, 1), 3),
        "avg_result_score": round(sum(r.get("result_score", 0.0) for r in results) / max(n, 1), 3),
        "cases": results,
    }


def _print_report(report: dict) -> None:
    print(f"\n{'='*58}")
    print("  BUSINESS CORRECTNESS EVAL  (result semantics, no LLM)")
    print(f"{'='*58}")
    print(f"  Cases       : {report['n_cases']}")
    print(f"  Pass rate   : {report['pass_rate']:.1%}  ({report['pass_count']}/{report['n_cases']})")
    print(f"  Avg score   : {report['avg_result_score']:.3f}")
    failures = [r for r in report["cases"] if not r.get("pass")]
    if failures:
        print(f"\n  Failures ({len(failures)}):")
        for r in failures:
            print(f"    [{r['label']}]")
            if r.get("error"):
                print(f"      fatal: {r['error'][:100]}")
            elif r.get("execution_error"):
                print(f"      SQL error: {r['execution_error'][:100]}")
            elif not r.get("sql_compiled"):
                print(f"      No SQL compiled (specialist route?): {r.get('specialist_route')}")
            else:
                detail = r.get("result_detail") or {}
                for chk, d in detail.items():
                    if isinstance(d, dict) and not d.get("match", True):
                        print(f"      FAIL  {chk}: {d}")
    else:
        print("  All cases passed.")
    print(f"{'='*58}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Business Correctness component eval — needs DuckDB, no LLM."
    )
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DuckDB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    catalog, sampled = load_catalog_and_sampled()
    configure_sql_guards_from_catalog(catalog)
    metrics = build_metrics_like_chat(catalog)

    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        report = run_business_correctness_eval(catalog, sampled, metrics, conn)
    finally:
        conn.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / (args.output or f"bc_eval_{stamp}.json")
    out.write_text(json.dumps(report, indent=2, default=str))

    _print_report(report)
    print(f"Saved to: {out}")


if __name__ == "__main__":
    main()
