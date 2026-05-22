"""
metrics_matcher.py — Zero LLM. Zero cost.

Loads metric templates from metrics_library/*.json,
checks each template's requirements against the scanned schema,
and returns only the metrics that are actually computable.

The LLM's job is then just to write natural-language descriptions
for pre-matched metrics — not to invent metrics from scratch.
"""

import json
import re
from pathlib import Path
from typing import Optional

LIBRARY_DIR = Path(__file__).parent / "metrics_library"
FAMILY_HINTS = {
    "transaction": {"payment", "transaction", "transfer", "purchase", "checkout"},
    "lending": {"loan", "credit", "lending", "borrow", "disburs", "underwrit"},
    "verification": {"kyc", "verify", "identity", "aadhaar", "otp", "pan"},
    "referral": {"referral", "invite", "share", "refer"},
    "investment": {"invest", "portfolio", "fund", "stock", "gold", "sip"},
    "subscription": {"subscription", "plan", "renew", "billing", "invoice"},
    "engagement": {"active", "session", "open", "visit", "view", "launch"},
}


def _load_library(industry: Optional[str] = None) -> list[dict]:
    """Load universal + optional industry metric templates."""
    templates = []

    universal = LIBRARY_DIR / "universal.json"
    if universal.exists():
        data = json.loads(universal.read_text())
        templates.extend(data.get("metrics", []))

    if industry:
        # Normalise: "fintech neobank" → try "fintech.json"
        for word in industry.lower().split():
            path = LIBRARY_DIR / f"{word}.json"
            if path.exists():
                data = json.loads(path.read_text())
                templates.extend(data.get("metrics", []))
                break

    return templates


def _find_col(columns: list[dict], signals: set[str]) -> Optional[str]:
    """Return first column whose name contains any signal token."""
    for col in columns:
        tokens = set(col["name"].lower().split("_"))
        if tokens & signals:
            return col["name"]
    return None


def _find_exact_col(columns: list[dict], signals: set[str]) -> Optional[str]:
    """Return first column whose lowercase name exactly matches a signal."""
    for col in columns:
        if col["name"].lower() in signals:
            return col["name"]
    return None


def _infer_metric_family(template: dict) -> set[str]:
    text = " ".join([
        template.get("id", ""),
        template.get("name", ""),
        template.get("category", ""),
        template.get("description_hint", ""),
    ]).lower()
    families = set()
    for family, hints in FAMILY_HINTS.items():
        if any(hint in text for hint in hints):
            families.add(family)
    return families or {"general"}


def _match_event(event_names: list[str], signals: list[str]) -> Optional[str]:
    """Return first event name whose tokens contain any of the signals."""
    for evt in event_names:
        tokens = set(evt.lower().split("_"))
        if any(sig in tokens or sig in evt.lower() for sig in signals):
            return evt
    return None


def _match_activation_event(table_info: dict, event_names: list[str], signals: list[str]) -> Optional[str]:
    """Prefer likely activation milestones over arbitrary 'verified' intermediate steps."""
    semantics = table_info.get("event_semantics", {})
    best_event = None
    best_score = -1

    for evt in event_names:
        info = semantics.get(evt, {})
        tokens = set(evt.lower().split("_"))
        lower = evt.lower()
        score = 0
        for sig in signals:
            if sig in tokens:
                score += 3
            elif sig in lower:
                score += 1

        if info.get("journey") == "onboarding":
            score += 3
        if info.get("outcome") == "success":
            score += 2
        if info.get("terminal"):
            score += 2
        if info.get("stage") == "completed":
            score += 2
        if info.get("stage") == "started":
            score -= 2

        score += min(int((info.get("event_count", 0) or 0) / 100000), 2)

        if score > best_score:
            best_score = score
            best_event = evt

    return best_event


def _match_flow_pair(flow_candidates: list[dict],
                     signal_groups: list[list[str]]) -> tuple[Optional[str], Optional[str]]:
    """Resolve conversion metrics from detected flows before raw token matching."""
    if len(signal_groups) < 2:
        return None, None

    start_signals, end_signals = signal_groups[0], signal_groups[1]

    def _score(flow: dict) -> int:
        haystack = " ".join([
            flow.get("journey", ""),
            flow.get("object", ""),
            flow.get("flow_id", ""),
            " ".join(flow.get("events", [])),
        ]).lower()
        score = 0
        for sig in start_signals + end_signals:
            if sig in haystack:
                score += 1
        return score

    ranked = sorted(flow_candidates, key=_score, reverse=True)
    for flow in ranked:
        if _score(flow) == 0:
            continue
        entry = flow.get("entry_events", [])
        success = flow.get("success_events", [])
        if entry and success:
            return entry[0], success[0]
    return None, None


def _find_flow_for_pair(flow_candidates: list[dict], start_event: str, end_event: str) -> Optional[dict]:
    for flow in flow_candidates:
        if start_event in flow.get("events", []) and end_event in flow.get("events", []):
            return flow
    return None


def _find_flow_for_event(flow_candidates: list[dict], event_name: str) -> Optional[dict]:
    for flow in flow_candidates:
        if event_name in flow.get("events", []):
            return flow
    return None


def _validate_metric_candidate(template: dict, candidate: dict, table_info: dict) -> tuple[str, list[str]]:
    reasons = []
    capabilities = table_info.get("capabilities", {})
    families = _infer_metric_family(template)
    flow_candidates = table_info.get("flow_candidates", [])

    if "transaction" in families and not capabilities.get("flow_presence", {}).get("transaction"):
        reasons.append("No transaction/payment flow detected for this table.")
    if "lending" in families and not capabilities.get("flow_presence", {}).get("lending"):
        reasons.append("No lending/credit flow detected for this table.")
    if "subscription" in families and not capabilities.get("flow_presence", {}).get("subscription"):
        reasons.append("No subscription/billing flow detected for this table.")
    if "referral" in families and not capabilities.get("has_referral_signal"):
        reasons.append("No referral/invite signal detected for this table.")

    start_event = candidate.get("_start_event") or ""
    end_event = candidate.get("_end_event") or ""
    matched_event = candidate.get("_matched_event") or ""

    if start_event and end_event:
        flow = _find_flow_for_pair(flow_candidates, start_event, end_event)
        if not flow:
            reasons.append("Start and end events do not belong to the same inferred flow.")
        else:
            journey = flow.get("journey", "general")
            if "transaction" in families and journey != "transaction":
                reasons.append(f"Matched flow journey is '{journey}', not a transaction/payment flow.")
            if "lending" in families and journey != "lending":
                reasons.append(f"Matched flow journey is '{journey}', not a lending flow.")
            if "verification" in families and journey not in {"verification", "onboarding"}:
                reasons.append(f"Matched flow journey is '{journey}', not a verification-like flow.")
            if not flow.get("success_events"):
                reasons.append("Matched flow has no terminal success event.")

    elif matched_event:
        flow = _find_flow_for_event(flow_candidates, matched_event)
        if "transaction" in families and flow and flow.get("journey") != "transaction":
            reasons.append(f"Matched event belongs to '{flow.get('journey')}' rather than a transaction/payment flow.")

    metric_id = template.get("id", "")
    monetization_model = capabilities.get("monetization_model", "unknown")
    if metric_id == "arpu" and monetization_model == "transaction_value_only":
        reasons.append("Only transaction value columns were detected; no explicit revenue/fee signal found for ARPU.")

    if reasons:
        return "suppressed", reasons
    return "approved", []


def _match_event_pair(event_names: list[str],
                       signal_groups: list[list[str]]) -> tuple[Optional[str], Optional[str]]:
    """
    For conversion metrics: find a start_event and end_event.
    signal_groups is a list of 2 groups:
      group[0] → signals for the start event (e.g. ["kyc", "verif"])
      group[1] → signals for the end event   (e.g. ["complete", "success"])
    """
    if len(signal_groups) < 2:
        return None, None

    start_signals, end_signals = signal_groups[0], signal_groups[1]

    start_event = None
    end_event   = None

    for evt in event_names:
        lower = evt.lower()
        tokens = set(lower.split("_"))
        has_start = any(s in lower for s in start_signals)
        has_end   = any(s in lower for s in end_signals)

        if has_start and not has_end and start_event is None:
            start_event = evt
        if has_end and end_event is None:
            end_event = evt

    return start_event, end_event


def _check_requirements(req: dict, table_info: dict,
                         columns: list[dict], event_names: list[str]) -> bool:
    """Return True if ALL requirements in req are satisfied."""
    if req.get("has_user_col") and not table_info.get("unique_users"):
        return False
    if req.get("has_time_col") and not table_info.get("date_range"):
        return False

    days = (table_info.get("date_range") or {}).get("days") or 0
    min_days = req.get("min_date_range_days", 0)
    if days < min_days:
        return False

    # event_name_contains: any single event must match at least one signal
    if "event_name_contains" in req:
        if not _match_event(event_names, req["event_name_contains"]):
            return False

    # event_name_contains_all: must find both a start AND end event
    if "event_name_contains_all" in req:
        flow_candidates = table_info.get("flow_candidates", [])
        start, end = _match_flow_pair(flow_candidates, req["event_name_contains_all"])
        if not start or not end:
            start, end = _match_event_pair(event_names, req["event_name_contains_all"])
        if not start or not end:
            return False

    # numeric_col_contains: table must have a numeric column with matching name
    if "numeric_col_contains" in req:
        numeric_types = {"INTEGER", "BIGINT", "DOUBLE", "FLOAT",
                         "DECIMAL", "NUMERIC", "REAL", "SMALLINT", "TINYINT"}
        found = any(
            c.get("type", "").upper().split("(")[0] in numeric_types
            and any(sig in c["name"].lower() for sig in req["numeric_col_contains"])
            for c in columns
        )
        if not found:
            return False

    return True


def _fill_template(template: dict, table_info: dict,
                   columns: list[dict], event_names: list[str],
                   table_name: str) -> Optional[dict]:
    """Fill SQL template placeholders with actual column / event names."""
    req = template.get("requires", {})

    USER_SIGNALS = {"user_id", "userid", "account_id", "customer_id",
                    "member_id", "uid", "visitor_id"}
    TIME_SIGNALS  = {"event_time", "timestamp", "created_at", "occurred_at",
                     "event_ts", "ts", "time"}
    EVENT_SIGNALS = {"event_name", "event_type", "event", "action", "activity"}

    user_col = (
        _find_exact_col(columns, USER_SIGNALS)
        or _find_col(columns, USER_SIGNALS)
        or "user_id"
    )
    time_col = (
        _find_exact_col(columns, TIME_SIGNALS)
        or _find_col(columns, TIME_SIGNALS)
        or "timestamp"
    )
    event_col = (
        table_info.get("event_name_col")
        or _find_exact_col(columns, EVENT_SIGNALS)
        or _find_col(columns, EVENT_SIGNALS)
        or "event_name"
    )

    # Match the primary event
    if template.get("id") == "activation_rate":
        matched_event = _match_activation_event(
            table_info, event_names, req.get("event_name_contains", [])
        ) or ""
    else:
        matched_event = _match_event(event_names,
                                     req.get("event_name_contains", [])) or ""

    # For conversion metrics: find start + end events
    start_event, end_event = "", ""
    if "event_name_contains_all" in req:
        start_event, end_event = _match_flow_pair(
            table_info.get("flow_candidates", []), req["event_name_contains_all"]
        )
        if not start_event or not end_event:
            start_event, end_event = _match_event_pair(
                event_names, req["event_name_contains_all"]
            )
        start_event = start_event or ""
        end_event   = end_event   or ""

    # Find numeric column if required
    numeric_col = ""
    if "numeric_col_contains" in req:
        numeric_types = {"INTEGER", "BIGINT", "DOUBLE", "FLOAT",
                         "DECIMAL", "NUMERIC", "REAL", "SMALLINT", "TINYINT"}
        for c in columns:
            if (c.get("type", "").upper().split("(")[0] in numeric_types
                    and any(sig in c["name"].lower()
                            for sig in req["numeric_col_contains"])):
                numeric_col = c["name"]
                break

    sql_template = template.get("sql_template", "")
    if "{matched_event}" in sql_template and not matched_event:
        return None
    if "{start_event}" in sql_template and not start_event:
        return None
    if "{end_event}" in sql_template and not end_event:
        return None
    if "{numeric_col}" in sql_template and not numeric_col:
        return None

    sql = (sql_template
           .replace("{table}",         table_name)
           .replace("{user_col}",      user_col)
           .replace("{time_col}",      time_col)
           .replace("{event_col}",     event_col)
           .replace("{matched_event}", matched_event)
           .replace("{start_event}",   start_event)
           .replace("{end_event}",     end_event)
           .replace("{numeric_col}",   numeric_col))

    # Skip malformed templates rather than publishing broken SQL.
    if re.search(r"=\s*''|IN\s*\(\s*''\s*\)", sql):
        return None

    return {
        "id":              template["id"],
        "name":            template["name"],
        "category":        template["category"],
        "aarrr":           template.get("aarrr", ""),
        "type":            template.get("type", "simple"),
        "description_hint": template.get("description_hint", ""),
        "sql":             sql,
        # Debug info — stripped before sending to LLM
        "_matched_event":  matched_event,
        "_start_event":    start_event,
        "_end_event":      end_event,
        "_numeric_col":    numeric_col,
        "_families":       sorted(_infer_metric_family(template)),
    }


def diagnose_metrics(table_info: dict, table_name: str,
                     industry: Optional[str] = None) -> list[dict]:
    """
    Return all metric candidates with validation status and reasons.
    """
    if not table_info.get("is_event_table"):
        return []

    templates   = _load_library(industry)
    columns     = table_info.get("columns", [])
    event_names = table_info.get("event_names", [])

    matched = []
    seen_ids = set()

    for tmpl in templates:
        if tmpl["id"] in seen_ids:
            continue
        req = tmpl.get("requires", {})
        if _check_requirements(req, table_info, columns, event_names):
            filled = _fill_template(tmpl, table_info, columns, event_names, table_name)
            if filled:
                status, reasons = _validate_metric_candidate(tmpl, filled, table_info)
                filled["status"] = status
                filled["validation_reasons"] = reasons
                matched.append(filled)
                seen_ids.add(tmpl["id"])

    return matched


def match_metrics(table_info: dict, table_name: str,
                  industry: Optional[str] = None) -> list[dict]:
    """
    Main entry point.

    Returns only approved metric dicts with filled SQL.
    Each dict has: id, name, category, aarrr, type, description_hint, sql.
    """
    return [
        metric for metric in diagnose_metrics(table_info, table_name, industry=industry)
        if metric.get("status") == "approved"
    ]


if __name__ == "__main__":
    import sys
    schema_path = sys.argv[1] if len(sys.argv) > 1 else "../raw_schema.json"
    industry    = sys.argv[2] if len(sys.argv) > 2 else "fintech"

    schema = json.loads(Path(schema_path).read_text())
    for tname, tinfo in schema.items():
        if not tinfo.get("is_event_table"):
            continue
        metrics = match_metrics(tinfo, tname, industry)
        print(f"\n{'='*55}")
        print(f"Table: {tname}  →  {len(metrics)} metrics matched")
        print(f"{'='*55}")
        for m in metrics:
            start = f"  {m['_start_event']} → {m['_end_event']}" if m.get("_start_event") else ""
            print(f"  [{m['aarrr']:12s}] {m['name']}{start}")
