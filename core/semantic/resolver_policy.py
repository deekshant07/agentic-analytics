"""
Centralized resolver policy for routing user queries.

This module keeps priority/order rules in one place so chat orchestration
does not rely on scattered fallback conditionals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

# Analysis types that must route through the analyst/compiler — resolver must never
# intercept these with custom_event heuristics regardless of keyword matches.
_DIRECT_COMPILER_ANALYSIS_TYPES = frozenset({
    "same_month_anchor",
    "behavioral_cohort",
    "time_between",
    "funnel",
    "funnel_compare",
    "journey",
    "user_lifecycle",
    "stickiness",
    "xyz_matrix",
    "funnel_property_drilldown",
})


@dataclass
class ResolverDecision:
    route: str = "orchestrator"
    matched_custom_events: list[dict[str, Any]] = field(default_factory=list)
    is_breakdown_query: bool = False
    is_core_metric_query: bool = False
    confidence: float = 0.5
    reason_codes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    comparison_intent: bool = False
    comparison_entities: list[str] = field(default_factory=list)
    comparison_guard_status: str = "not_applicable"  # pass|recover|clarify|not_applicable


_DEFAULT_RULES = {
    "routes": [
        {"name": "core_metric", "route": "orchestrator", "priority": 100, "base_confidence": 0.95},
        {"name": "custom_split", "route": "custom_split", "priority": 80, "base_confidence": 0.9},
        {"name": "custom_single", "route": "custom_single", "priority": 60, "base_confidence": 0.78},
        {"name": "custom_segment", "route": "custom_segment", "priority": 55, "base_confidence": 0.8},
        {"name": "default_compiler", "route": "orchestrator", "priority": 10, "base_confidence": 0.55},
    ]
}


def _load_rules() -> dict[str, Any]:
    path = Path(__file__).with_name("resolver_policy_rules.json")
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict) and isinstance(data.get("routes"), list):
                return data
    except Exception:
        pass
    return _DEFAULT_RULES


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _tokenize(s: str) -> set[str]:
    stop = {
        "share", "show", "for", "in", "on", "of", "the", "a", "an",
        "users", "user", "numbers", "number", "count", "how", "many",
        "split", "by", "vs", "versus", "jan", "january", "feb", "february",
        "mar", "march", "apr", "april", "may", "jun", "june", "jul", "july",
        "aug", "august", "sep", "september", "oct", "october", "nov", "november", "dec", "december",
    }
    return {t for t in _norm_text(s).split() if t and t not in stop}


def _best_custom_events(q: str, custom_events: list[dict[str, Any]], top_k: int = 2) -> list[dict[str, Any]]:
    qn = _norm_text(q)
    qt = _tokenize(q)

    docs = []
    for ce in custom_events:
        name = (ce.get("name") or "").strip()
        desc = (ce.get("description") or "").strip()
        sql = (ce.get("sql") or "").strip()
        if not name or not sql:
            continue
        docs.append(_tokenize(name) | _tokenize(desc))

    df: dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    n_docs = max(len(docs), 1)
    idf = {t: 1.0 - (df.get(t, 1) / n_docs) for t in df}
    q_weight = sum(idf.get(t, 1.0) for t in qt) or 1.0

    q_terms = qn.split()
    q_bigrams = {" ".join(q_terms[i:i + 2]) for i in range(max(0, len(q_terms) - 1))}
    scored = []
    for ce in custom_events:
        name = (ce.get("name") or "").strip()
        desc = (ce.get("description") or "").strip()
        sql = (ce.get("sql") or "").strip()
        if not name or not sql:
            continue
        name_norm = _norm_text(name)
        name_tok = _tokenize(name)
        desc_tok = _tokenize(desc)
        ce_text_norm = f"{name_norm} {_norm_text(desc)}"
        exact = 1.0 if name_norm in qn else 0.0
        overlap_name = (len(qt & name_tok) / max(len(name_tok), 1)) if name_tok else 0.0
        overlap_desc = (len(qt & desc_tok) / max(len(desc_tok), 1)) if desc_tok else 0.0
        score = max(exact, 0.8 * overlap_name + 0.2 * overlap_desc)

        ce_vocab = name_tok | desc_tok
        overlap = qt & ce_vocab
        rare_boost = 0.0
        for tok in overlap:
            rare_boost += 1.0 - (df.get(tok, 1) / n_docs)
        if overlap:
            rare_boost /= len(overlap)
        score += 0.35 * rare_boost

        coverage = sum(idf.get(t, 1.0) for t in overlap) / q_weight
        score += 0.45 * coverage

        phrase_hits = sum(1 for bg in q_bigrams if bg and bg in ce_text_norm)
        if phrase_hits:
            score += min(0.25, 0.08 * phrase_hits)

        if score >= 0.45:
            scored.append((score, coverage, len(name_tok), ce))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return [ce for _, _, _, ce in scored[:top_k]]


def _split_pair_match_custom_events(question: str, custom_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    For explicit comparison phrasing ("A vs B"), match each side independently.
    This avoids one strong cohort match crowding out the second cohort in top-k ranking.
    """
    q = (question or "").strip()
    if not q:
        return []
    qn = _norm_text(q)
    parts: list[str] = []
    if " vs " in f" {qn} ":
        left, right = qn.split(" vs ", 1)
        parts = [left.strip(), right.strip()]
    elif " versus " in f" {qn} ":
        left, right = qn.split(" versus ", 1)
        parts = [left.strip(), right.strip()]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        # "difference between cohort A and cohort B …"
        if "difference between " in qn and " and " in qn.split("difference between ", 1)[-1]:
            rest = qn.split("difference between ", 1)[1].strip()
            left_s, right_s = rest.split(" and ", 1)
            parts = [left_s.strip(), right_s.strip()]

    if len(parts) != 2 or not parts[0] or not parts[1]:
        return []

    left_match = _best_custom_events(parts[0], custom_events, top_k=1)
    right_match = _best_custom_events(parts[1], custom_events, top_k=1)
    if not left_match or not right_match:
        return []

    a, b = left_match[0], right_match[0]
    if (a.get("name") or "") == (b.get("name") or ""):
        return []
    return [a, b]


def _is_dimension_breakdown_request(question: str, sampled_values: dict[str, dict[str, list[Any]]]) -> bool:
    """
    True when the question asks for a metric broken down by a known schema column.
    Uses only actual column names from sampled_values — no hardcoded domain vocab.

    Detects many surface forms:
      "DAU by city", "city split", "split by platform", "breakdown by region",
      "per channel", "grouped by status", "across segments", etc.
    """
    # Build column vocab from the real schema
    col_vocab: set[str] = set()
    for _, cols in (sampled_values or {}).items():
        for c in (cols or {}).keys():
            col_vocab.add(_norm_text(c))
    if not col_vocab:
        return False

    qn = _norm_text(question)
    q_tokens = set(qn.split())

    # Patterns: capture the word(s) after/before a breakdown keyword
    patterns = [
        re.compile(r"\bby\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bsplit\s+by\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bbroken?\s+down\s+by\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bgrouped?\s+by\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bper\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bacross\s+(\w+)", re.IGNORECASE),
        # "city split", "platform breakdown" — dim word BEFORE the keyword
        re.compile(r"\b(\w+)\s+split\b", re.IGNORECASE),
        re.compile(r"\b(\w+)\s+breakdown\b", re.IGNORECASE),
    ]
    for pat in patterns:
        for m in pat.finditer(question):
            candidate = _norm_text(m.group(1))
            if candidate in col_vocab:
                return True
    return False


def _orchestrator_structured_confidence(qo, question: str) -> float:
    """
    Estimate confidence from QueryObject structure rather than keyword hacks.
    This is used when no explicit resolver route matches.
    """
    q = _norm_text(question)
    at = (getattr(qo, "analysis_type", "") or "").strip().lower()
    metric_id = getattr(qo, "metric_id", None)
    event = getattr(qo, "event", None)
    breakdown = getattr(qo, "breakdown", None)
    filters = getattr(qo, "filters", None) or {}
    date_from = getattr(qo, "date_from", None)
    date_to = getattr(qo, "date_to", None)
    tr = getattr(qo, "time_range_days", None)
    tw = getattr(qo, "retention_window_days", None)

    # Start slightly below gate; structured signals push above threshold.
    conf = 0.56
    executable_types = {
        "metric", "segment", "funnel", "funnel_compare", "journey", "retention",
        "behavioral_cohort", "time_between", "same_month_anchor", "diagnose", "forecast",
        "demographic_breakdown",
    }
    if at in executable_types:
        conf += 0.10
    elif at in {"clarify", "out_of_scope", ""}:
        conf -= 0.08
    else:
        conf += 0.03

    if metric_id:
        conf += 0.12
    if event:
        conf += 0.12
    if breakdown:
        conf += 0.06
    if isinstance(filters, dict) and filters:
        conf += min(0.06, 0.02 * len(filters))
    if date_from and date_to:
        conf += 0.06
    elif isinstance(tr, int) and tr > 0:
        conf += 0.04

    # Retention/funnel intent with valid slots is generally safe to execute.
    if at == "retention":
        conf += 0.06
        if isinstance(tw, int) and tw in {1, 7, 30}:
            conf += 0.02
    elif at in {"funnel", "funnel_compare"}:
        fs = getattr(qo, "funnel_steps", None) or []
        if fs:
            conf += 0.04

    # If user typed explicit period hints, nudge confidence up modestly.
    if any(k in q for k in ("last ", "past ", "this ", "month", "week", "day", "q1", "q2", "q3", "q4")):
        conf += 0.02

    return max(0.35, min(0.93, conf))


def _is_named_core_active_metric_query(question: str, metric_id: str | None) -> bool:
    qn = _norm_text(question)
    mentions_core = any(
        k in qn for k in (
            "dau", "wau", "mau",
            "daily active users", "weekly active users", "monthly active users",
        )
    )
    if mentions_core:
        return True
    mid = (metric_id or "").lower()
    return mid in {"dau", "wau", "mau"}


_MONTH_NAMES = frozenset({
    "jan", "january", "feb", "february", "mar", "march",
    "apr", "april", "may", "jun", "june", "jul", "july",
    "aug", "august", "sep", "september", "oct", "october",
    "nov", "november", "dec", "december",
})

_TEMPORAL_COMPARE_RE = re.compile(
    r"\b(compare|versus|vs\.?|against|vs)\b.{0,40}"
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"last month|previous month|prior month|last week|previous week|\d{4})\b",
    re.IGNORECASE,
)


def _is_temporal_period_comparison(question: str) -> bool:
    """
    True when 'compare / vs / against' is being used to contrast two TIME PERIODS
    rather than two metrics.

    Examples that return True:
      "Transacting users in Feb and compare against Jan"
      "DAU in March vs February"
      "How did signups in Jan compare to December"

    This suppresses the custom_split route so the orchestrator/compiler handles
    month-over-month logic correctly instead of treating it as a two-metric comparison.
    """
    q = question.lower()
    q_pad = f" {q} "
    temporal_keywords = ("compare", " vs ", "versus", "against", "vs.")
    if not any(kw in q_pad for kw in temporal_keywords):
        return False

    # Detect multiple explicit temporal markers (month names / years / relative periods).
    month_markers = set(re.findall(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        q,
        flags=re.IGNORECASE,
    ))
    year_markers = set(re.findall(r"\b(20\d{2})\b", q))
    relative_markers = set(
        m.group(1).lower()
        for m in re.finditer(
            r"\b(last month|previous month|prior month|this month|"
            r"last week|previous week|prior week|this week|"
            r"last quarter|previous quarter|prior quarter|this quarter|"
            r"last year|previous year|prior year|this year)\b",
            q,
            flags=re.IGNORECASE,
        )
    )
    marker_count = len(month_markers) + len(year_markers) + len(relative_markers)
    if marker_count >= 2:
        return True

    # Backward compatibility for explicit month-period compare phrasing.
    # Require at least one explicit period token + comparison keyword.
    if _TEMPORAL_COMPARE_RE.search(question):
        return bool(month_markers or relative_markers or year_markers) and marker_count >= 2
    return False


def _has_comparison_intent(question: str) -> bool:
    """
    Detect generic comparison intent beyond literal 'vs'.
    Excludes pure temporal period comparisons, which should stay in orchestrator.
    """
    q = f" {_norm_text(question)} "
    patterns = (
        " vs ",
        " versus ",
        " compare ",
        " compared ",
        " against ",
        " relative to ",
        " split between ",
        " difference between ",
    )
    return any(p in q for p in patterns)


def resolve_query_policy(question: str, qo, catalog: dict[str, Any], sampled_values: dict[str, dict[str, list[Any]]]) -> ResolverDecision:
    biz = catalog.get("__business_context__", {}) or {}
    custom_events = biz.get("custom_events", []) or []
    q_norm = _norm_text(question)
    bound_nm = getattr(qo, "_bound_custom_event_name", None)
    # Session stickiness must not override an explicit catalog cohort id from the orchestrator.
    mid = getattr(qo, "metric_id", None)
    if (
        bound_nm
        and isinstance(mid, str)
        and mid.startswith("ce_")
    ):
        implied = mid[3:].strip()
        if implied and str(bound_nm).strip() != implied:
            if any(
                isinstance(c, dict) and (c.get("name") or "").strip() == implied
                for c in custom_events
            ):
                bound_nm = None
                setattr(qo, "_bound_custom_event_name", None)
    if bound_nm:
        ce_hit = next(
            (
                c
                for c in custom_events
                if isinstance(c, dict) and (c.get("name") or "").strip() == str(bound_nm).strip()
            ),
            None,
        )
        matched = [ce_hit] if ce_hit else _best_custom_events(question, custom_events, top_k=2)
    else:
        matched = _best_custom_events(question, custom_events, top_k=2)
    split_pair_matched = _split_pair_match_custom_events(question, custom_events)
    if split_pair_matched:
        matched = split_pair_matched
    split_signals = (" vs ", " versus ", "split", "breakdown", "compare")
    count_signals = ("how many", "count", "numbers", "number", "share", "users", "user", "total")

    # Temporal comparisons ("Feb vs Jan") must NOT be treated as custom-event splits.
    # The orchestrator/compiler handles period-over-period correctly.
    is_temporal_comparison = _is_temporal_period_comparison(question)
    comparison_intent = _has_comparison_intent(question) and not is_temporal_comparison

    # If the orchestrator already resolved a breakdown dimension, trust it —
    # no text heuristic needed and custom_split must not fire.
    qo_breakdown    = getattr(qo, "breakdown", None)
    qo_analysis     = getattr(qo, "analysis_type", "")
    # Deterministic compiler path — must not lose to custom-event heuristics.
    if qo_analysis in _DIRECT_COMPILER_ANALYSIS_TYPES:
        matched = []
    is_breakdown_query = bool(qo_breakdown) or _is_dimension_breakdown_request(question, sampled_values)
    # Explicit "A vs B" / "difference between A and B" with two distinct custom
    # events: allow custom_split even if the LLM labeled the query as segment
    # (common failure mode — segment blocks split below).
    cohort_vs_pair = len(split_pair_matched) >= 2
    # Segment breakdown requests are not cohort-vs-cohort comparisons, even if
    # they contain words like "split" ("split by device", "split by city").
    # Exception: when both sides of "A vs B" matched distinct custom events, the
    # breakdown is a misfire from the orchestrator (e.g. "compare in-app vs off-app
    # transacting users" → orchestrator sets breakdown="transaction_channel") and
    # must NOT suppress the split route.
    if is_breakdown_query and not cohort_vs_pair:
        comparison_intent = False

    # custom_split ONLY makes sense when the user explicitly wants two DIFFERENT
    # custom-event cohorts compared — not when:
    #   • the orchestrator decided this is a "segment" analysis
    #   • a breakdown dimension is set (→ use custom_segment instead)
    #   • it's a temporal comparison ("Feb vs Jan")
    #   • "split/breakdown" appear as part of a dimension phrase ("city split")
    is_segment_analysis = qo_analysis == "segment" and not cohort_vs_pair
    # Pair-matched cohorts (vs / versus / difference between) imply split intent even
    # when keywords like "compare" or "split" are missing.
    has_split_phrasing = any(sig in f" {q_norm} " for sig in split_signals) or cohort_vs_pair
    is_split_query = (
        not is_temporal_comparison
        and not is_segment_analysis
        and (not is_breakdown_query or cohort_vs_pair)  # pair match overrides a spurious breakdown
        and len(matched) >= 2
        and has_split_phrasing
    )
    is_core_metric_query = _is_named_core_active_metric_query(question, getattr(qo, "metric_id", None))

    decision = ResolverDecision(
        route="orchestrator",
        matched_custom_events=matched,
        is_breakdown_query=is_breakdown_query,
        is_core_metric_query=is_core_metric_query,
        comparison_intent=comparison_intent,
        comparison_entities=[ce.get("name", "") for ce in (matched or []) if ce.get("name")],
    )
    rules = _load_rules().get("routes", [])
    candidates: list[dict[str, Any]] = []
    if is_core_metric_query:
        cfg = next((r for r in rules if r.get("name") == "core_metric"), {"priority": 100, "base_confidence": 0.95, "route": "orchestrator"})
        candidates.append({
            "name": "core_metric",
            "route": cfg.get("route", "orchestrator"),
            "priority": int(cfg.get("priority", 100)),
            "confidence": float(cfg.get("base_confidence", 0.95)),
            "reason": "core_metric_explicit",
        })
    if is_split_query and len(matched) >= 2:
        cfg = next((r for r in rules if r.get("name") == "custom_split"), {"priority": 80, "base_confidence": 0.9, "route": "custom_split"})
        candidates.append({
            "name": "custom_split",
            "route": cfg.get("route", "custom_split"),
            "priority": int(cfg.get("priority", 80)),
            "confidence": float(cfg.get("base_confidence", 0.9)),
            "reason": "custom_events_split_match",
        })
    if matched and (not is_split_query) and (not is_breakdown_query) and any(sig in q_norm for sig in count_signals):
        cfg = next((r for r in rules if r.get("name") == "custom_single"), {"priority": 60, "base_confidence": 0.78, "route": "custom_single"})
        candidates.append({
            "name": "custom_single",
            "route": cfg.get("route", "custom_single"),
            "priority": int(cfg.get("priority", 60)),
            "confidence": float(cfg.get("base_confidence", 0.78)),
            "reason": "custom_event_count_like",
        })
    if matched and is_breakdown_query and getattr(qo, "analysis_type", "") == "segment" and getattr(qo, "breakdown", None):
        cfg = next((r for r in rules if r.get("name") == "custom_segment"), {"priority": 55, "base_confidence": 0.8, "route": "custom_segment"})
        candidates.append({
            "name": "custom_segment",
            "route": cfg.get("route", "custom_segment"),
            "priority": int(cfg.get("priority", 55)),
            "confidence": float(cfg.get("base_confidence", 0.8)),
            "reason": "custom_event_breakdown_segment",
        })
    if not candidates:
        cfg = next((r for r in rules if r.get("name") == "default_compiler"), {"priority": 10, "base_confidence": 0.55, "route": "orchestrator"})
        candidates.append({
            "name": "default_compiler",
            "route": cfg.get("route", "orchestrator"),
            "priority": int(cfg.get("priority", 10)),
            "confidence": _orchestrator_structured_confidence(qo, question),
            "reason": "orchestrator_structured_intent",
        })

    # Comparison guardrail:
    # If user asks to compare/split and we resolved fewer than 2 custom entities,
    # do not allow custom_single to win. Force clarify path through low-confidence
    # orchestrator fallback so chat asks for the missing second entity.
    if comparison_intent and len(decision.comparison_entities) < 2:
        filtered = [c for c in candidates if c.get("name") != "custom_single"]
        if not filtered:
            filtered = [{
                "name": "default_compiler",
                "route": "orchestrator",
                "priority": 10,
                "confidence": 0.35,
                "reason": "comparison_requires_two_entities",
            }]
        else:
            for c in filtered:
                if c.get("name") == "default_compiler":
                    c["confidence"] = min(float(c.get("confidence", 0.55)), 0.35)
                    c["reason"] = "comparison_requires_two_entities"
                    break
            else:
                filtered.append({
                    "name": "default_compiler",
                    "route": "orchestrator",
                    "priority": 10,
                    "confidence": 0.35,
                    "reason": "comparison_requires_two_entities",
                })
        candidates = filtered
        decision.comparison_guard_status = "clarify"
    elif comparison_intent:
        decision.comparison_guard_status = "pass"

    candidates.sort(key=lambda c: (c["priority"], c["confidence"]), reverse=True)
    winner = candidates[0]
    conflicts = [c["name"] for c in candidates[1:] if c["priority"] == winner["priority"]]

    decision.route = winner["route"]
    decision.confidence = winner["confidence"]
    decision.reason_codes = [winner["reason"]]
    decision.conflicts = conflicts
    decision.candidates = candidates
    if decision.route == "custom_split":
        decision.matched_custom_events = matched[:2]
    elif decision.route in {"custom_single", "custom_segment"} and matched:
        decision.matched_custom_events = [matched[0]]
    return decision


