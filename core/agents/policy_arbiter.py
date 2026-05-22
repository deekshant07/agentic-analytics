"""
Policy Arbiter
--------------
Decides whether a routed query is safe enough to execute or should clarify first.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass
class ArbiterResult:
    allow_execute: bool
    confidence: float
    reasons: list[str]
    clarify_message: str | None = None


_DEFAULT_RULES = {
    "min_confidence": 0.6,
    "allow_low_confidence_with_anchor": True,
}


def _load_rules() -> dict[str, Any]:
    path = Path(__file__).with_name("policy_arbiter_rules.json")
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return {**_DEFAULT_RULES, **data}
    except Exception:
        pass
    return _DEFAULT_RULES


def _has_strong_anchor(qo, decision: Any = None) -> bool:
    at = getattr(qo, "analysis_type", "")
    if bool(
        getattr(qo, "metric_id", None)
        or getattr(qo, "event", None)
        or (at == "funnel_compare" and getattr(qo, "funnel_steps", None))
        or at in {"funnel", "retention", "diagnose", "journey"}
    ):
        return True
    if decision is not None:
        route = getattr(decision, "route", "") or ""
        matched = getattr(decision, "matched_custom_events", None) or []
        if route in {"custom_split", "custom_single", "custom_segment"} and len(matched) >= 1:
            return True
    return False


def arbitrate(decision, qo, question: str) -> ArbiterResult:
    rules = _load_rules()
    conf = float(getattr(decision, "confidence", 0.5) or 0.5)
    reasons = list(getattr(decision, "reason_codes", []) or [])
    min_conf = float(rules.get("min_confidence", 0.6))
    at = (getattr(qo, "analysis_type", "") or "").strip().lower()
    orc_msg = (getattr(qo, "clarify_message", None) or "").strip()

    # Orchestrator already chose clarify / out_of_scope — never execute SQL, and prefer its
    # message over a generic "metric vs segment" prompt.
    if at in ("clarify", "out_of_scope"):
        fallback = (
            "I want to make sure I interpret this correctly before running queries. "
            "Do you want metric trend, segment split, or retention view?"
        )
        return ArbiterResult(
            False,
            conf,
            reasons + ["orchestrator_clarify_or_scope"],
            orc_msg or fallback,
        )

    if conf >= min_conf:
        return ArbiterResult(True, conf, reasons)

    if rules.get("allow_low_confidence_with_anchor", True) and _has_strong_anchor(qo, decision):
        reasons = reasons + ["low_confidence_but_strong_anchor"]
        return ArbiterResult(True, conf, reasons)

    msg = (
        "I want to make sure I interpret this correctly before running queries. "
        "Do you want metric trend, segment split, or retention view?"
    )
    return ArbiterResult(False, conf, reasons + ["low_confidence_requires_clarify"], msg)

