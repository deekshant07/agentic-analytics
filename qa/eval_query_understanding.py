"""
eval_query_understanding.py — Component-isolated eval: orchestrator layer only.

Tests: does the orchestrator correctly map natural language → QO slots?
  - analysis_type accuracy (intent)
  - slot accuracy per field: metric_id, event, event_b, breakdown, metric_variant, filters

Uses questions from build_eval_suite() where gold_qo or expected_analysis_type is defined.
Requires: LLM API key.
Does NOT require: SQL compilation, DuckDB.

Usage:
    uv run python -m qa.eval_query_understanding
    uv run python -m qa.eval_query_understanding --output qu_run.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.eval_env import load_project_dotenv
from qa.eval_production import (
    apply_post_orchestrator_fixups,
    build_metrics_like_chat,
    configure_sql_guards_from_catalog,
)
from qa.eval_benchmark import (
    OUT_DIR,
    _orchestrate_with_retry,
    build_eval_suite,
    load_catalog_and_sampled,
    score_gold_qo,
    score_intent_accuracy,
)

_env_paths, _env_diag = load_project_dotenv(ROOT)

_SCORED_SLOTS = ("analysis_type", "metric_id", "event", "event_b", "breakdown", "metric_variant")


def run_query_understanding_eval(
    catalog: dict,
    sampled: dict,
    metrics: list[dict],
    api_key: str | None,
    *,
    eval_provider: str | None = None,
    case_delay_sec: float = 2.0,
) -> dict[str, Any]:
    all_cases = build_eval_suite(catalog)
    # Only run cases that have an oracle to check against.
    cases = [c for c in all_cases if c.gold_qo or c.expected_analysis_type]

    slot_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    misroutes: list[dict] = []
    results = []

    for i, case in enumerate(cases):
        t_start = time.perf_counter()
        try:
            qo = _orchestrate_with_retry(
                question=case.question,
                catalog=catalog,
                sampled_values=sampled,
                openai_api_key=api_key,
                history=[],
                eval_provider=eval_provider,
            )
            apply_post_orchestrator_fixups(
                qo,
                question=case.question,
                catalog=catalog,
                sampled=sampled,
                metrics=metrics,
                history=[],
            )
            ia = score_intent_accuracy(qo.analysis_type, case)
            gold_s, gold_d = score_gold_qo(qo, case)

            # Per-slot accuracy tracking
            for slot in _SCORED_SLOTS:
                detail = gold_d.get(slot)
                if isinstance(detail, dict) and "match" in detail:
                    slot_counts[slot]["total"] += 1
                    if detail["match"]:
                        slot_counts[slot]["correct"] += 1
            # Track filters_contain separately
            fc_detail = gold_d.get("filters_contain")
            if isinstance(fc_detail, dict) and "match" in fc_detail:
                slot_counts["filters"]["total"] += 1
                if fc_detail["match"]:
                    slot_counts["filters"]["correct"] += 1

            passed = (ia >= 1.0 if not case.gold_qo else gold_s >= 1.0)
            if not passed:
                misroutes.append({
                    "question": case.question,
                    "expected_analysis_type": case.expected_analysis_type,
                    "got_analysis_type": qo.analysis_type,
                    "gold_qo_detail": gold_d,
                })

            results.append({
                "question": case.question,
                "tags": case.tags,
                "expected_analysis_type": case.expected_analysis_type,
                "got_analysis_type": qo.analysis_type,
                "intent_accuracy": round(ia, 3),
                "gold_qo_score": round(gold_s, 3),
                "gold_qo_detail": gold_d,
                "pass": passed,
                "latency_ms": round((time.perf_counter() - t_start) * 1000),
            })
        except Exception as e:
            results.append({
                "question": case.question,
                "tags": case.tags,
                "error": f"{type(e).__name__}: {e}",
                "pass": False,
                "latency_ms": round((time.perf_counter() - t_start) * 1000),
            })

        if case_delay_sec > 0 and i + 1 < len(cases):
            time.sleep(case_delay_sec)

    n = len(results)
    intent_scores = [r["intent_accuracy"] for r in results if "intent_accuracy" in r]
    gold_scores = [r["gold_qo_score"] for r in results if "gold_qo_score" in r]

    slot_accuracy = {
        slot: round(v["correct"] / v["total"], 3)
        for slot, v in sorted(slot_counts.items())
        if v["total"] > 0
    }

    return {
        "component": "query_understanding",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_cases": n,
        "pass_count": sum(1 for r in results if r.get("pass")),
        "pass_rate": round(sum(1 for r in results if r.get("pass")) / max(n, 1), 3),
        "avg_intent_accuracy": round(sum(intent_scores) / max(len(intent_scores), 1), 3),
        "avg_gold_qo_score": round(sum(gold_scores) / max(len(gold_scores), 1), 3),
        "slot_accuracy": slot_accuracy,
        "misroutes": misroutes,
        "cases": results,
    }


def _print_report(report: dict) -> None:
    print(f"\n{'='*58}")
    print("  QUERY UNDERSTANDING EVAL  (orchestrator layer)")
    print(f"{'='*58}")
    print(f"  Cases evaluated : {report['n_cases']}")
    print(f"  Pass rate       : {report['pass_rate']:.1%}  ({report['pass_count']}/{report['n_cases']})")
    print(f"  Intent accuracy : {report['avg_intent_accuracy']:.3f}")
    print(f"  Gold QO score   : {report['avg_gold_qo_score']:.3f}")
    sa = report.get("slot_accuracy", {})
    if sa:
        print(f"\n  Slot accuracy (% of cases where slot matched gold):")
        for slot, acc in sa.items():
            bar = "█" * int(acc * 20) + "░" * (20 - int(acc * 20))
            print(f"    {slot:<20} {acc:.0%}  {bar}")
    misroutes = report.get("misroutes", [])
    if misroutes:
        print(f"\n  Wrong intent / slot ({len(misroutes)} cases):")
        for m in misroutes[:10]:
            exp = m.get("expected_analysis_type", "?")
            got = m.get("got_analysis_type", "?")
            tag = f"{exp} → {got}" if exp != got else exp
            print(f"    [{tag}] {m['question'][:65]}")
            for k, d in (m.get("gold_qo_detail") or {}).items():
                if isinstance(d, dict) and not d.get("match", True):
                    print(f"      slot '{k}': expected={d.get('expected')} got={d.get('got')}")
    print(f"{'='*58}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Query Understanding component eval.")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument(
        "--case-delay",
        type=float,
        default=float(os.environ.get("EVAL_CASE_DELAY_SEC", "2.0")),
        metavar="SEC",
    )
    args = parser.parse_args()

    catalog, sampled = load_catalog_and_sampled()
    configure_sql_guards_from_catalog(catalog)
    metrics = build_metrics_like_chat(catalog)

    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip() or None
    eval_provider = os.environ.get("EVAL_LLM_PROVIDER") or None

    report = run_query_understanding_eval(
        catalog, sampled, metrics, api_key,
        eval_provider=eval_provider,
        case_delay_sec=args.case_delay,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / (args.output or f"qu_eval_{stamp}.json")
    out.write_text(json.dumps(report, indent=2, default=str))

    _print_report(report)
    print(f"Saved to: {out}")


if __name__ == "__main__":
    main()
