"""
test_playbook_flow.py — End-to-end smoke test for the playbook → orchestrator pipeline.

Verifies:
  1. PlaybookRegistry loads all YAML files
  2. Intent matching returns the right playbook for each question
  3. to_orchestrator_hint_block() generates a non-empty hint for matched questions
  4. orchestrate() with playbook_hint produces a QueryObject whose analysis_type
     matches the playbook's intent_class (or a valid related type)

Run:
    uv run python qa/test_playbook_flow.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── env ──────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.semantic.playbooks import PlaybookRegistry
from core.pipeline.orchestrator import orchestrate

CATALOG_PATH = ROOT / "catalog.json"
PLAYBOOKS_DIR = ROOT / "playbooks"

catalog: dict = json.loads(CATALOG_PATH.read_text()) if CATALOG_PATH.exists() else {}


# ── Test cases ────────────────────────────────────────────────────────────────
# (question, expected_playbook_intent_class, acceptable_analysis_types)
TEST_CASES = [
    (
        "why are users not returning after their first transaction",
        "retention",
        {"retention"},
    ),
    (
        "show me dau mau ratio for the last 30 days",
        "stickiness",
        {"stickiness", "metric"},
    ),
    (
        "where are users dropping off in the checkout funnel",
        "funnel",
        {"funnel", "funnel_compare"},
    ),
    (
        "what do users do after completing a payment",
        "journey",
        {"journey"},
    ),
    (
        "show me transaction trend week over week",
        "metric",
        {"metric", "segment"},
    ),
    (
        "did the experiment work better for ios users",
        "segment",
        {"segment", "metric"},
    ),
    (
        "who are my power users and what makes them different",
        "behavioral_cohort",
        {"behavioral_cohort", "segment"},
    ),
    (
        "which acquisition channel brings users with the best retention",
        "segment",
        {"segment", "metric", "retention"},
    ),
    (
        "signed up but never made their first transaction",
        "behavioral_cohort",
        {"behavioral_cohort", "segment"},
    ),
    (
        "onboarding funnel drop off — where are new users abandoning",
        "funnel",
        {"funnel"},
    ),
]

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m~\033[0m"


@dataclass
class Result:
    question: str
    playbook_id: Optional[str]
    playbook_intent: Optional[str]
    hint_present: bool
    qo_analysis_type: Optional[str]
    expected_intent: str
    acceptable_types: set
    playbook_ok: bool
    qo_ok: bool
    error: Optional[str] = None


def run_one(
    registry: PlaybookRegistry,
    question: str,
    expected_intent: str,
    acceptable_types: set,
) -> Result:
    pb = registry.find(question)
    hint = pb.to_orchestrator_hint_block() if pb else ""
    hint_present = bool(hint)
    playbook_ok = pb is not None and pb.intent_class == expected_intent

    qo_analysis_type = None
    error = None
    try:
        qo = orchestrate(
            question=question,
            catalog=catalog,
            sampled_values={},
            playbook_hint=hint or None,
        )
        qo_analysis_type = getattr(qo, "analysis_type", None)
    except Exception as exc:
        error = str(exc)

    qo_ok = qo_analysis_type in acceptable_types if qo_analysis_type else False

    return Result(
        question=question,
        playbook_id=pb.id if pb else None,
        playbook_intent=pb.intent_class if pb else None,
        hint_present=hint_present,
        qo_analysis_type=qo_analysis_type,
        expected_intent=expected_intent,
        acceptable_types=acceptable_types,
        playbook_ok=playbook_ok,
        qo_ok=qo_ok,
        error=error,
    )


def main():
    registry = PlaybookRegistry(PLAYBOOKS_DIR)
    print(f"\nLoaded {len(registry)} playbooks from {PLAYBOOKS_DIR}\n")
    print("=" * 72)

    results: list[Result] = []
    for i, (q, expected_intent, acceptable) in enumerate(TEST_CASES, 1):
        print(f"[{i:02d}] {q[:65]!r}")
        r = run_one(registry, q, expected_intent, acceptable)
        results.append(r)

        pb_icon = PASS if r.playbook_ok else FAIL
        qo_icon = PASS if r.qo_ok else (WARN if r.qo_analysis_type else FAIL)

        print(f"     Playbook  {pb_icon}  {r.playbook_id or 'NO MATCH'}"
              f"  (intent={r.playbook_intent or '-'}, expected={expected_intent})")
        print(f"     Hint      {'present' if r.hint_present else 'absent (no match)'}")
        print(f"     QO type   {qo_icon}  analysis_type={r.qo_analysis_type!r}"
              f"  (acceptable={sorted(acceptable)})")
        if r.error:
            print(f"     ERROR: {r.error[:120]}")
        print()

    pb_pass  = sum(1 for r in results if r.playbook_ok)
    qo_pass  = sum(1 for r in results if r.qo_ok)
    total    = len(results)

    print("=" * 72)
    print(f"Playbook matching:  {pb_pass}/{total}")
    print(f"QO analysis_type:   {qo_pass}/{total}")
    print()

    if pb_pass == total and qo_pass == total:
        print(f"{PASS} All checks passed.")
    else:
        print(f"{WARN} Some checks failed — review results above.")

    return 0 if (pb_pass == total and qo_pass == total) else 1


if __name__ == "__main__":
    sys.exit(main())
