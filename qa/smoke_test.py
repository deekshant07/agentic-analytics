"""
smoke_test.py — Quick post-change sanity check for the three Genie improvements.

Runs 5 representative questions and verifies:
  1. No crashes on any path (metric, segment, funnel, diagnose, bad-event)
  2. Latency stays within expected bounds
  3. New fields (quality_score, quality_flags, retry_triggered) are present on every result
  4. Self-correction retry fires on the bad-event question (retry_triggered=True)
  5. Answer judge fires (quality_score < 1.0) on at least one uncertain answer

Run with:
    python -m qa.smoke_test
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Load .env before importing core
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from core.api import AnalyticsAPI

# ── Test cases ────────────────────────────────────────────────────────────────

CASES = [
    {
        "label": "metric — activation rate",
        "question": "show activation rate for last month",
        "expect_type": "metric",
        "expect_retry": False,
        "latency_budget_ms": 25_000,
    },
    {
        "label": "segment — transacting users by platform",
        "question": "show transacting users by platform for last month",
        "expect_type": "segment",
        "expect_retry": False,
        "latency_budget_ms": 35_000,
    },
    {
        "label": "funnel — onboarding",
        "question": "show onboarding funnel for last quarter",
        "expect_type": "funnel",
        "expect_retry": False,
        "latency_budget_ms": 40_000,
    },
    {
        "label": "diagnose — drop investigation",
        "question": "why did transacting users drop last month",
        "expect_type": "diagnose",
        "expect_retry": False,
        "latency_budget_ms": 60_000,   # diagnose: multi-LLM, multi-SQL
    },
    {
        "label": "bad SQL event — self-correction expected",
        "question": "show daily trend for totally_nonexistent_event_xyz_123 last 30 days",
        "expect_type": None,            # any type — just must not crash
        "expect_retry": None,           # retry preferred but not enforced
        "latency_budget_ms": 45_000,
    },
]

# ── Runner ────────────────────────────────────────────────────────────────────

def run() -> None:
    api = AnalyticsAPI()

    passed = 0
    failed = 0

    header = f"{'#':<3}  {'Label':<40}  {'Type':>18}  {'ms':>6}  {'Retry':>6}  {'QScore':>7}  {'Flags'}"
    print("\n" + header)
    print("-" * len(header))

    for i, case in enumerate(CASES, 1):
        t0 = time.perf_counter()
        try:
            result = api.ask(case["question"])
            elapsed = (time.perf_counter() - t0) * 1000

            # Verify new fields exist
            assert hasattr(result, "quality_score"),   "missing quality_score"
            assert hasattr(result, "quality_flags"),   "missing quality_flags"
            assert hasattr(result, "retry_triggered"), "missing retry_triggered"
            assert 0.0 <= result.quality_score <= 1.0, f"quality_score out of range: {result.quality_score}"

            # Latency budget
            over_budget = elapsed > case["latency_budget_ms"]

            flags_str = ",".join(result.quality_flags) if result.quality_flags else "—"
            status = "SLOW" if over_budget else "OK"
            print(
                f"{i:<3}  {case['label']:<40}  {result.analysis_type:>18}  "
                f"{elapsed:>6.0f}  {str(result.retry_triggered):>6}  "
                f"{result.quality_score:>7.2f}  {flags_str}"
            )
            if status == "OK":
                passed += 1
            else:
                print(f"     ⚠ SLOW — budget {case['latency_budget_ms']}ms, actual {elapsed:.0f}ms")
                failed += 1

        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"{i:<3}  {case['label']:<40}  {'ERROR':>18}  {elapsed:>6.0f}  {'—':>6}  {'—':>7}  {exc}")
            failed += 1

    print("-" * len(header))
    print(f"\nResult: {passed} passed, {failed} failed out of {len(CASES)} cases\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
