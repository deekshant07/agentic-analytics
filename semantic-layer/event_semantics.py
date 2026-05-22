"""
event_semantics.py — deterministic semantic inference for event logs.

Builds two reusable layers from raw event names:
- event_semantics: per-event object / stage / outcome / journey signals
- flow_candidates: grouped process flows with entry / success / failure events

This gives downstream metric generation a more stable abstraction than
matching raw event names directly.
"""
from __future__ import annotations

from collections import defaultdict
import re

START_TOKENS = {
    "start", "started", "begin", "began", "initiate", "initiated",
    "request", "requested", "launch", "launched", "submit", "submitted",
    "create", "created", "open", "opened", "attempt", "attempted",
}
SUCCESS_TOKENS = {
    "complete", "completed", "success", "successful", "succeed", "succeeded",
    "verify", "verified", "approve", "approved", "confirm", "confirmed",
    "finish", "finished", "done", "reconcile", "reconciled",
}
FAILURE_TOKENS = {
    "fail", "failed", "failure", "error", "errored",
    "reject", "rejected", "decline", "declined", "cancel", "cancelled",
}
INTERMEDIATE_TOKENS = {
    "enter", "entered", "view", "viewed", "screen", "click", "clicked",
    "select", "selected", "receive", "received", "set", "bound", "binding",
}
GENERIC_TOKENS = {
    "event", "user", "app", "call", "flow", "process",
}
JOURNEY_RULES = {
    "verification": {"kyc", "vkyc", "aadhaar", "pan", "otp", "identity", "verify", "sim", "bureau"},
    "onboarding": {"onboard", "signup", "register", "install", "welcome", "profile", "mpin"},
    "authentication": {"login", "logout", "otp", "password", "auth"},
    "transaction": {"transaction", "payment", "transfer", "purchase", "reconcile", "amount"},
    "engagement": {"open", "view", "screen", "session", "notification", "click"},
}


def _tokens(event_name: str) -> list[str]:
    return [tok for tok in re.split(r"[_\-\s]+", event_name.lower()) if tok]


def _stage(tokens: set[str]) -> tuple[str, str, bool]:
    if tokens & FAILURE_TOKENS:
        return "failed", "failure", True
    if tokens & SUCCESS_TOKENS:
        return "completed", "success", True
    if tokens & START_TOKENS:
        return "started", "unknown", False
    if tokens & INTERMEDIATE_TOKENS:
        return "intermediate", "unknown", False
    return "observed", "unknown", False


def _journey(tokens: set[str], groups: dict) -> str:
    group_values = " ".join(str(v).lower() for v in (groups or {}).values())
    for journey, keywords in JOURNEY_RULES.items():
        if tokens & keywords or any(keyword in group_values for keyword in keywords):
            return journey
    if groups:
        first = next(iter(groups.values()), "")
        if first:
            return str(first).lower().replace(" ", "_")
    return "general"


def _object_key(tokens: list[str]) -> str:
    filtered = [
        tok for tok in tokens
        if tok not in START_TOKENS
        and tok not in SUCCESS_TOKENS
        and tok not in FAILURE_TOKENS
        and tok not in INTERMEDIATE_TOKENS
        and tok not in GENERIC_TOKENS
    ]
    if not filtered:
        filtered = [tok for tok in tokens if tok not in GENERIC_TOKENS]
    return "_".join(filtered[:3]) if filtered else "general"


def infer_event_semantics(event_names: list[str], event_counts: dict | None = None,
                          event_groups: dict | None = None) -> dict[str, dict]:
    semantics = {}
    event_counts = event_counts or {}
    event_groups = event_groups or {}

    for event_name in event_names:
        token_list = _tokens(event_name)
        token_set = set(token_list)
        stage, outcome, terminal = _stage(token_set)
        object_key = _object_key(token_list)
        journey = _journey(token_set, event_groups.get(event_name, {}))

        confidence = 0.45
        if token_set & (START_TOKENS | SUCCESS_TOKENS | FAILURE_TOKENS):
            confidence += 0.2
        if object_key != "general":
            confidence += 0.15
        if journey != "general":
            confidence += 0.1
        if event_groups.get(event_name):
            confidence += 0.05

        semantics[event_name] = {
            "raw_name": event_name,
            "tokens": token_list,
            "object": object_key,
            "journey": journey,
            "stage": stage,
            "outcome": outcome,
            "terminal": terminal,
            "event_count": event_counts.get(event_name, 0),
            "groups": event_groups.get(event_name, {}),
            "confidence": round(min(confidence, 0.95), 2),
        }

    return semantics


def infer_flow_candidates(event_semantics: dict[str, dict],
                          auto_funnels: list[dict] | None = None) -> list[dict]:
    auto_funnels = auto_funnels or []
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for event_name, info in event_semantics.items():
        key = (info.get("journey", "general"), info.get("object", "general"))
        grouped[key].append(info)

    flows = []
    seen_ids = set()

    for (journey, object_key), items in grouped.items():
        entry = [e["raw_name"] for e in items if e.get("stage") == "started"]
        success = [e["raw_name"] for e in items if e.get("outcome") == "success"]
        failure = [e["raw_name"] for e in items if e.get("outcome") == "failure"]
        intermediate = [
            e["raw_name"] for e in items
            if e.get("stage") not in {"started"} and e.get("outcome") == "unknown"
        ]

        if not (entry or success or failure):
            continue

        flow_id = f"{journey}__{object_key}"
        seen_ids.add(flow_id)
        avg_conf = sum(e.get("confidence", 0.5) for e in items) / max(len(items), 1)
        flows.append({
            "flow_id": flow_id,
            "journey": journey,
            "object": object_key,
            "entry_events": sorted(entry),
            "success_events": sorted(success),
            "failure_events": sorted(failure),
            "intermediate_events": sorted(intermediate),
            "events": sorted(e["raw_name"] for e in items),
            "confidence": round(avg_conf, 2),
            "source": "semantic_grouping",
        })

    for funnel in auto_funnels:
        start_event = funnel.get("from")
        end_event = funnel.get("to")
        if not start_event or not end_event:
            continue
        start_info = event_semantics.get(start_event, {})
        end_info = event_semantics.get(end_event, {})
        journey = start_info.get("journey") or end_info.get("journey") or "general"
        object_key = start_info.get("object") or end_info.get("object") or "general"
        flow_id = f"{journey}__{object_key}__funnel"
        if flow_id in seen_ids:
            continue
        flows.append({
            "flow_id": flow_id,
            "journey": journey,
            "object": object_key,
            "entry_events": [start_event],
            "success_events": [end_event],
            "failure_events": [],
            "intermediate_events": [],
            "events": sorted({start_event, end_event}),
            "confidence": 0.78,
            "source": "auto_funnel",
        })
        seen_ids.add(flow_id)

    flows.sort(key=lambda flow: (flow.get("journey", ""), flow.get("object", ""), -flow.get("confidence", 0)))
    return flows


def infer_capabilities(event_semantics: dict[str, dict], flow_candidates: list[dict],
                       columns: list[dict]) -> dict:
    """Infer generic product/data capabilities from journeys, flows, and columns."""
    journey_counts: dict[str, int] = defaultdict(int)
    for info in event_semantics.values():
        journey_counts[info.get("journey", "general")] += 1

    flow_presence = {
        flow.get("journey", "general"): True
        for flow in flow_candidates
        if flow.get("success_events") or flow.get("entry_events")
    }

    col_names = {col.get("name", "").lower() for col in columns}
    numeric_cols = {
        col.get("name", "").lower()
        for col in columns
        if any(marker in (col.get("type", "") or "").upper()
               for marker in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "NUMERIC", "REAL"))
    }

    fee_like = any(token in name for name in numeric_cols for token in ("fee", "commission", "revenue", "margin"))
    transaction_value_like = any(token in name for name in numeric_cols for token in ("amount", "value", "price", "gmv"))

    monetization_model = "unknown"
    if fee_like:
        monetization_model = "explicit_revenue"
    elif transaction_value_like and flow_presence.get("transaction"):
        monetization_model = "transaction_value_only"

    return {
        "journeys": dict(journey_counts),
        "flow_presence": flow_presence,
        "has_terminal_success": any(flow.get("success_events") for flow in flow_candidates),
        "monetization_model": monetization_model,
        "numeric_signals": {
            "fee_like_columns": sorted(name for name in numeric_cols if any(tok in name for tok in ("fee", "commission", "revenue", "margin"))),
            "transaction_value_columns": sorted(name for name in numeric_cols if any(tok in name for tok in ("amount", "value", "price", "gmv"))),
        },
        "has_referral_signal": any("referral" in info.get("raw_name", "") or "invite" in info.get("raw_name", "")
                                   for info in event_semantics.values()),
    }
