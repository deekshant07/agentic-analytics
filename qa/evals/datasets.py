"""
qa/evals/datasets.py — Dataset generation and management utilities.

Provides:
  1. Synthetic query generation from catalog metrics
  2. Adversarial case patterns (edge cases, ambiguity, negation, typos)
  3. Gold dataset loader/saver for JSON-backed case libraries
  4. Multi-turn sequence templates

Usage:
    from qa.evals.datasets import (
        generate_synthetic_from_catalog,
        generate_adversarial_cases,
        load_gold_dataset,
        save_gold_dataset,
    )
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ── Dataset directory ─────────────────────────────────────────────────────────
DATASETS_DIR = Path(__file__).parent / "datasets"


# ── Synthetic query templates ─────────────────────────────────────────────────
# Each template is (question_template, expected_analysis_type, tags)
# {metric_name} and {metric_id} are substituted from catalog.

_METRIC_TREND_TEMPLATES = [
    ("show {metric_name} for last month",          "metric",  ["metric_trend", "basic"]),
    ("what is {metric_name}",                      "metric",  ["metric_trend", "basic"]),
    ("show {metric_name} MOM for last 6 months",   "metric",  ["metric_trend", "monthly"]),
    ("show {metric_name} trend",                   "metric",  ["metric_trend", "trend"]),
    ("how has {metric_name} changed over time",    "metric",  ["metric_trend", "trend"]),
    ("{metric_name} for Q1",                       "metric",  ["metric_trend", "relative_time"]),
    ("show {metric_name} week over week",          "metric",  ["metric_trend", "weekly"]),
]

_SEGMENT_TEMPLATES = [
    ("show {metric_name} by platform",                   "segment", ["segment", "breakdown"]),
    ("show {metric_name} by channel",                    "segment", ["segment", "breakdown"]),
    ("show {metric_name} by city for last month",        "segment", ["segment", "breakdown"]),
    ("{metric_name} broken down by platform",            "segment", ["segment", "breakdown"]),
    ("compare {metric_name} across platforms",           "segment", ["segment", "comparison"]),
]

_ADVERSARIAL_BASE = [
    # Typos
    ("shw {metric_name} lst week",               "metric",  ["adversarial", "typo"]),
    ("{metric_name} trnds",                      "metric",  ["adversarial", "typo"]),
    # Ambiguous
    ("show me users",                            None,      ["adversarial", "ambiguous"]),
    ("what happened last month",                 None,      ["adversarial", "underspecified"]),
    # Bad time windows
    ("show {metric_name} for last 3 days",       None,      ["adversarial", "bad_window"]),
    ("show MAU for last 2 days",                 None,      ["adversarial", "bad_window"]),
    # Negation
    ("show {metric_name} for non-referral users", "metric", ["adversarial", "negation_filter"]),
    # Out of catalog
    ("how many users visited the settings page", None,      ["adversarial", "out_of_catalog"]),
    # Multi-intent
    ("compare activation and churn by platform", None,      ["adversarial", "multi_intent"]),
]


@dataclass
class SyntheticEvalCase:
    """A generated eval case — mirrors eval_benchmark.EvalCase for compatibility."""
    question: str
    expected_analysis_type: str | None = None
    expected_route: str | None = None
    tags: list[str] = field(default_factory=list)
    gold_qo: dict[str, Any] | None = None
    gold_sql: dict[str, Any] | None = None
    source: str = "synthetic"


def _approved_metrics(catalog: dict) -> list[dict]:
    """Extract all approved metrics from catalog with id, name, description."""
    metrics = []
    for tname, tdata in (catalog or {}).items():
        if not isinstance(tdata, dict) or str(tname).startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []) or []:
            if m.get("status") in ("approved", None, ""):
                metrics.append({
                    "id":   m.get("id", ""),
                    "name": m.get("name", ""),
                    "desc": m.get("description", ""),
                })
    return metrics


def generate_synthetic_from_catalog(
    catalog: dict,
    *,
    templates: str = "all",
    max_per_metric: int = 3,
    seed: int = 42,
) -> list[SyntheticEvalCase]:
    """
    Generate synthetic eval cases from catalog metrics by filling templates.

    Args:
        templates: "trend" | "segment" | "all"
        max_per_metric: Max cases generated per metric (to avoid explosion).
        seed: Random seed for reproducible subsampling.

    Returns list of SyntheticEvalCase — import as EvalCase-compatible dicts via
    `[asdict(c) for c in cases]`.
    """
    rng = random.Random(seed)
    metrics = _approved_metrics(catalog)
    if not metrics:
        return []

    tmpl_sets: list[list] = []
    if templates in ("trend", "all"):
        tmpl_sets.append(_METRIC_TREND_TEMPLATES)
    if templates in ("segment", "all"):
        tmpl_sets.append(_SEGMENT_TEMPLATES)

    all_tmpls = [t for s in tmpl_sets for t in s]

    cases: list[SyntheticEvalCase] = []
    for metric in metrics:
        mid  = metric["id"]
        name = metric["name"]
        if not mid or not name:
            continue

        selected = rng.sample(all_tmpls, min(max_per_metric, len(all_tmpls)))
        for tmpl, analysis_type, tags in selected:
            q = tmpl.replace("{metric_name}", name).replace("{metric_id}", mid)
            gold_qo: dict | None = {"metric_id": mid} if analysis_type == "metric" else None
            if analysis_type == "segment" and gold_qo is None:
                gold_qo = {"metric_id": mid}
            cases.append(SyntheticEvalCase(
                question=q,
                expected_analysis_type=analysis_type,
                tags=tags + ["synthetic"],
                gold_qo=gold_qo,
            ))

    return cases


def generate_adversarial_cases(catalog: dict | None = None) -> list[SyntheticEvalCase]:
    """
    Generate adversarial eval cases targeting known failure patterns.

    If catalog is provided, metric-specific adversarial cases are also included.
    """
    cases: list[SyntheticEvalCase] = []

    # Catalog-agnostic adversarials
    static_cases = [
        ("show me users",                          None,      ["adversarial", "ambiguous"]),
        ("what happened last month",               None,      ["adversarial", "underspecified"]),
        ("show MAU for last 2 days",               None,      ["adversarial", "bad_window"]),
        ("how many users visited the settings page", None,    ["adversarial", "out_of_catalog"]),
        ("compare activation and churn by platform", None,   ["adversarial", "multi_intent"]),
        ("show me everything",                     None,      ["adversarial", "underspecified"]),
        ("who are our best users",                 None,      ["adversarial", "ambiguous"]),
        ("why is revenue down",                    "diagnose", ["adversarial", "diagnose"]),
    ]
    for q, atype, tags in static_cases:
        cases.append(SyntheticEvalCase(question=q, expected_analysis_type=atype, tags=tags))

    # Metric-templated adversarials (need a catalog)
    if catalog:
        metrics = _approved_metrics(catalog)
        if metrics:
            m = metrics[0]  # use first approved metric as representative
            for tmpl, atype, tags in _ADVERSARIAL_BASE:
                q = tmpl.replace("{metric_name}", m["name"]).replace("{metric_id}", m["id"])
                cases.append(SyntheticEvalCase(question=q, expected_analysis_type=atype, tags=tags))

    return cases


# ── Gold dataset persistence ──────────────────────────────────────────────────

def load_gold_dataset(name: str, datasets_dir: Path = DATASETS_DIR) -> list[dict[str, Any]]:
    """
    Load a named gold dataset from `datasets/<name>.json`.

    Returns list of case dicts (compatible with eval_benchmark.EvalCase).
    Empty list if the file doesn't exist.
    """
    path = datasets_dir / f"{name}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_gold_dataset(
    name: str,
    cases: list[dict[str, Any]],
    datasets_dir: Path = DATASETS_DIR,
) -> Path:
    """
    Persist a gold dataset to `datasets/<name>.json`.

    Merges with existing cases (deduplicates by question text).
    Returns the saved path.
    """
    datasets_dir.mkdir(parents=True, exist_ok=True)
    path = datasets_dir / f"{name}.json"

    existing = load_gold_dataset(name, datasets_dir)
    existing_questions = {c["question"] for c in existing if isinstance(c, dict)}

    new_cases = [c for c in cases if c.get("question") not in existing_questions]
    all_cases = existing + new_cases

    path.write_text(json.dumps(all_cases, indent=2))
    return path


def cases_from_failed_traces(
    traces: list[dict[str, Any]],
    min_ar: float = 0.5,
) -> list[dict[str, Any]]:
    """
    Extract failed eval traces as candidate gold dataset entries.

    These become hard cases — they document known failure modes and can be
    converted to regression tests once the bug is fixed.

    Only includes traces with answer_relevance < min_ar (the worst failures).
    """
    failed = []
    for t in traces:
        ar = float(t.get("scores", {}).get("answer_relevance", 1.0))
        if ar >= min_ar:
            continue
        expected = t.get("expected", {}) or {}
        failed.append({
            "question":                 t.get("question", ""),
            "expected_analysis_type":   expected.get("expected_analysis_type"),
            "expected_route":           expected.get("expected_route"),
            "tags":                     expected.get("tags", []) + ["from_failure"],
            "gold_qo":                  expected.get("gold_qo"),
            "gold_sql":                 expected.get("gold_sql"),
            "failure_stage":            t.get("failure_stage"),
            "answer_relevance":         round(ar, 3),
            "failure_reason":           t.get("fatal_error") or t.get("failure_stage"),
        })
    return sorted(failed, key=lambda x: x["answer_relevance"])


def generate_multi_turn_templates(catalog: dict) -> list[dict[str, Any]]:
    """
    Generate multi-turn conversation templates from catalog metric pairs.

    Each template documents a realistic follow-up pattern. These can be
    fed into eval_benchmark.MultiTurnEvalCase.
    """
    metrics = _approved_metrics(catalog)[:5]  # limit to first 5 to avoid explosion
    templates = []

    for m in metrics:
        mid, name = m["id"], m["name"]
        if not mid or not name:
            continue

        # Pattern 1: filter then trend
        templates.append({
            "description": f"{name} filter then trend follow-up",
            "tags": ["multi_turn", "filter_inheritance", "synthetic"],
            "turns": [
                {
                    "question": f"show {name} for iOS",
                    "expected_analysis_type": "segment",
                    "gold_qo": {"metric_id": mid},
                },
                {
                    "question": "now show the monthly trend",
                    "expected_analysis_type": "metric",
                    "gold_qo": {"metric_id": mid},
                },
            ],
        })

        # Pattern 2: metric then breakdown
        templates.append({
            "description": f"{name} then breakdown follow-up",
            "tags": ["multi_turn", "breakdown_follow_up", "synthetic"],
            "turns": [
                {
                    "question": f"what is {name}",
                    "expected_analysis_type": "metric",
                    "gold_qo": {"metric_id": mid},
                },
                {
                    "question": "break it down by platform",
                    "expected_analysis_type": "segment",
                    "gold_qo": {"metric_id": mid, "breakdown": "platform"},
                },
            ],
        })

    return templates
