"""
qa/evals/semantic_layer.py — Semantic layer evaluation.

Evaluates:
  1. Metric resolution accuracy — does the QO pick the right metric_id?
  2. Synonym coverage — do natural-language aliases resolve to the canonical metric?
  3. Deprecated metric detection — are deprecated metrics rejected?
  4. Catalog coverage — what fraction of catalog metrics appear in eval traces?

Usage:
    from qa.evals.semantic_layer import (
        score_metric_resolution,
        eval_synonym_resolution,
        compute_catalog_coverage,
        batch_eval_semantic_layer,
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Synonym map for common natural-language aliases ───────────────────────────
# Each entry: canonical_metric_id → list of natural-language aliases
# These cover patterns seen in QA traces; extend as new synonyms appear.
KNOWN_SYNONYMS: dict[str, list[str]] = {
    "activation_rate":   ["activation", "activated", "activate rate", "upi activation",
                          "first transaction", "onboarded and transacted"],
    "d7_retention":      ["d7 retention", "7-day retention", "day 7 retention",
                          "week 1 retention", "D7"],
    "d30_retention":     ["d30 retention", "30-day retention", "month 1 retention",
                          "day 30 retention", "D30"],
    "dau":               ["daily active users", "daily actives", "DAU", "active users today"],
    "mau":               ["monthly active users", "monthly actives", "MAU"],
    "wau":               ["weekly active users", "weekly actives", "WAU"],
    "stickiness":        ["dau/mau", "stickiness ratio", "how sticky", "product stickiness"],
    "conversion_rate":   ["conversion", "converted", "checkout conversion",
                          "purchase rate", "buy rate"],
    "churn_rate":        ["churn", "churned", "churning users", "lost users"],
    "nps":               ["net promoter", "nps score", "promoter score"],
}


@dataclass
class MetricResolutionResult:
    metric_id_got: str | None
    metric_id_expected: str | None
    score: float
    match_type: str  # "exact" | "acceptable_variant" | "mismatch" | "not_expected"
    detail: dict[str, Any] = field(default_factory=dict)


# Acceptable cross-metric equivalences (e.g. returning a variant of the same
# logical metric is not counted as wrong).
_METRIC_EQUIVALENCES: dict[str, set[str]] = {
    "activation_rate":   {"activation_rate", "activation_rate_v2", "activation_rate_30d"},
    "d7_retention":      {"d7_retention", "retention_d7"},
    "d30_retention":     {"d30_retention", "retention_d30"},
    "dau":               {"dau", "daily_active_users"},
    "mau":               {"mau", "monthly_active_users"},
    "conversion_rate":   {"conversion_rate", "conversion_rate_v2", "purchase_rate"},
}


def score_metric_resolution(
    expected_metric_id: str | None,
    got_metric_id: str | None,
) -> MetricResolutionResult:
    """
    Score whether the orchestrator resolved to the correct metric_id.

    Score rubric:
        1.0 — exact match
        0.8 — known acceptable variant of the same logical metric
        0.0 — wrong metric or None when expected
    """
    if expected_metric_id is None:
        return MetricResolutionResult(
            metric_id_got=got_metric_id,
            metric_id_expected=None,
            score=0.5,  # neutral — no expectation
            match_type="not_expected",
        )

    if got_metric_id == expected_metric_id:
        return MetricResolutionResult(
            metric_id_got=got_metric_id,
            metric_id_expected=expected_metric_id,
            score=1.0,
            match_type="exact",
        )

    # Check acceptable variants
    equiv_set = _METRIC_EQUIVALENCES.get(expected_metric_id, {expected_metric_id})
    if got_metric_id in equiv_set:
        return MetricResolutionResult(
            metric_id_got=got_metric_id,
            metric_id_expected=expected_metric_id,
            score=0.8,
            match_type="acceptable_variant",
            detail={"variant_group": sorted(equiv_set)},
        )

    return MetricResolutionResult(
        metric_id_got=got_metric_id,
        metric_id_expected=expected_metric_id,
        score=0.0,
        match_type="mismatch",
        detail={"got": got_metric_id, "expected": expected_metric_id},
    )


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def eval_synonym_resolution(
    catalog: dict,
    traces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Evaluate whether natural-language metric synonyms resolve correctly.

    1. Builds a lookup from catalog metric names + descriptions.
    2. For each alias in KNOWN_SYNONYMS, checks if the canonical metric
       exists in the catalog.
    3. If traces are provided, additionally checks that trace QOs resolved
       to the expected metric for questions containing known aliases.

    Returns a structured report.
    """
    # Build catalog metric index: id → {name, description, status}
    catalog_metrics: dict[str, dict] = {}
    for tname, tdata in (catalog or {}).items():
        if not isinstance(tdata, dict) or str(tname).startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []) or []:
            mid = m.get("id", "")
            if mid:
                catalog_metrics[mid] = {
                    "name": m.get("name", ""),
                    "description": m.get("description", ""),
                    "status": m.get("status") or "approved",
                }

    results: list[dict] = []
    for canonical_id, aliases in KNOWN_SYNONYMS.items():
        in_catalog = canonical_id in catalog_metrics
        meta = catalog_metrics.get(canonical_id, {})
        status = meta.get("status", "unknown")
        entry = {
            "canonical_id": canonical_id,
            "in_catalog": in_catalog,
            "status": status,
            "alias_count": len(aliases),
            "aliases": aliases,
        }
        if traces:
            # Check how often traces with a question containing the alias actually
            # resolved to the right metric_id.
            hits, total = 0, 0
            for alias in aliases:
                norm_alias = _normalize(alias)
                for t in traces:
                    q = _normalize(t.get("question", ""))
                    if norm_alias not in q:
                        continue
                    qo = (t.get("stages", {}).get("qo_after_fixups") or
                          t.get("stages", {}).get("orchestrator_raw") or {})
                    got_id = qo.get("metric_id")
                    total += 1
                    if got_id == canonical_id or got_id in _METRIC_EQUIVALENCES.get(canonical_id, set()):
                        hits += 1
            entry["trace_resolution_rate"] = round(hits / total, 3) if total else None
            entry["trace_n"] = total
        results.append(entry)

    n_in_catalog = sum(1 for r in results if r["in_catalog"])
    return {
        "n_synonyms_checked": len(results),
        "n_canonical_in_catalog": n_in_catalog,
        "catalog_coverage_pct": round(n_in_catalog / max(len(results), 1), 3),
        "per_metric": results,
    }


def find_deprecated_metric_usage(
    catalog: dict,
    traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Identify traces where the orchestrator resolved to a deprecated metric.

    Returns a list of {question, metric_id, status} for deprecated usages.
    """
    deprecated_ids: set[str] = set()
    for tname, tdata in (catalog or {}).items():
        if not isinstance(tdata, dict) or str(tname).startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []) or []:
            if m.get("status") in ("deprecated", "disabled", "removed"):
                deprecated_ids.add(m.get("id", ""))

    deprecated_usage = []
    for t in traces:
        qo = (t.get("stages", {}).get("qo_after_fixups") or
              t.get("stages", {}).get("orchestrator_raw") or {})
        mid = qo.get("metric_id") or ""
        if mid in deprecated_ids:
            deprecated_usage.append({
                "question": t.get("question", ""),
                "metric_id": mid,
                "note": "deprecated metric resolved",
            })
    return deprecated_usage


def compute_catalog_coverage(
    catalog: dict,
    traces: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    What fraction of catalog metrics appear in at least one eval trace?

    Low coverage → eval suite doesn't exercise large parts of the semantic layer.
    """
    all_metric_ids: set[str] = set()
    for tname, tdata in (catalog or {}).items():
        if not isinstance(tdata, dict) or str(tname).startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []) or []:
            mid = m.get("id", "")
            if mid and m.get("status") not in ("deprecated", "disabled"):
                all_metric_ids.add(mid)

    seen_ids: set[str] = set()
    for t in traces:
        qo = (t.get("stages", {}).get("qo_after_fixups") or
              t.get("stages", {}).get("orchestrator_raw") or {})
        mid = qo.get("metric_id") or ""
        if mid:
            seen_ids.add(mid)

    covered = all_metric_ids & seen_ids
    uncovered = all_metric_ids - seen_ids

    return {
        "total_catalog_metrics": len(all_metric_ids),
        "covered_in_eval": len(covered),
        "coverage_pct": round(len(covered) / max(len(all_metric_ids), 1), 3),
        "uncovered_metrics": sorted(uncovered),
        "covered_metrics": sorted(covered),
    }


def batch_eval_semantic_layer(
    catalog: dict,
    traces: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Run all semantic layer evaluations over a full eval run's traces.

    Returns a combined report suitable for including in the eval dashboard.
    """
    # Score metric resolution for traces with gold_qo.metric_id defined
    resolution_scores: list[float] = []
    resolution_failures: list[dict] = []

    for t in traces:
        gold_qo = (t.get("expected") or {}).get("gold_qo") or {}
        expected_mid = gold_qo.get("metric_id")
        if not expected_mid:
            continue
        qo = (t.get("stages", {}).get("qo_after_fixups") or
              t.get("stages", {}).get("orchestrator_raw") or {})
        got_mid = qo.get("metric_id")
        r = score_metric_resolution(expected_mid, got_mid)
        resolution_scores.append(r.score)
        if r.score < 1.0:
            resolution_failures.append({
                "question": t.get("question", ""),
                "expected": expected_mid,
                "got": got_mid,
                "match_type": r.match_type,
            })

    synonym_report = eval_synonym_resolution(catalog, traces)
    coverage = compute_catalog_coverage(catalog, traces)
    deprecated = find_deprecated_metric_usage(catalog, traces)

    return {
        "metric_resolution": {
            "n_cases_with_gold_metric": len(resolution_scores),
            "avg_score": round(sum(resolution_scores) / max(len(resolution_scores), 1), 3),
            "failures": resolution_failures,
        },
        "synonym_resolution": synonym_report,
        "catalog_coverage": coverage,
        "deprecated_metric_usage": deprecated,
    }
