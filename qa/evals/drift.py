"""
qa/evals/drift.py — Score drift and regression detection across eval runs.

Compares consecutive eval_*.json result files and alerts when:
  - overall pass_rate drops below threshold
  - avg_answer_relevance drops by more than delta_threshold
  - per-tag pass_rate regresses
  - stage_failure_counts shift significantly
  - latency p95 spikes

Usage:
    from qa.evals.drift import detect_drift, load_eval_history, DriftAlert

    history = load_eval_history(Path("qa/eval_results"))
    alerts = detect_drift(history, n_runs=5)
    for alert in alerts:
        print(alert)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Alert thresholds ──────────────────────────────────────────────────────────
DRIFT_THRESHOLDS = {
    "pass_rate_drop":          0.05,   # alert if pass_rate falls by >5pp vs rolling avg
    "answer_relevance_drop":   0.03,   # alert if avg_answer_relevance falls by >0.03
    "intent_accuracy_drop":    0.05,   # alert if avg_intent_accuracy falls by >5pp
    "gold_qo_score_drop":      0.05,
    "latency_p95_spike_ms":    2000,   # alert if p95 latency rises by >2s
    "hard_fail_spike":         3,      # alert if hard_fail_count rises by >3
    "tag_pass_rate_drop":      0.10,   # per-tag: alert if pass_rate falls by >10pp
}


@dataclass
class DriftAlert:
    severity: str           # "warning" | "critical"
    metric: str
    current_value: float | int
    baseline_value: float | int
    delta: float | int
    threshold: float | int
    run_file: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"[{self.severity.upper()}] {self.metric}: "
            f"{self.current_value} vs baseline {self.baseline_value:.3f} "
            f"(Δ={self.delta:+.3f}, threshold={self.threshold}) — {self.run_file}"
        )


def load_eval_history(
    eval_dir: Path,
    pattern: str = "eval_*.json",
    n_most_recent: int = 20,
) -> list[dict[str, Any]]:
    """
    Load the N most recent eval result JSON files, sorted oldest → newest.

    Each entry in the returned list is:
        {"file": filename, "aggregate": {...}, "generated_at": ..., "cases": [...]}
    """
    paths = sorted(
        eval_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
    )[-n_most_recent:]

    history = []
    for p in paths:
        try:
            data = json.loads(p.read_text())
            agg = data.get("aggregate")
            if not agg:
                continue
            history.append({
                "file": p.name,
                "generated_at": data.get("generated_at", ""),
                "aggregate": agg,
                "cases": data.get("cases", []),
            })
        except Exception:
            continue
    return history


def _rolling_baseline(
    history: list[dict[str, Any]],
    metric_path: str,
    n_runs: int = 3,
) -> float | None:
    """
    Compute rolling average of a metric over the last n_runs (excluding current).
    metric_path is dot-notation: "aggregate.avg_answer_relevance"
    """
    if len(history) < 2:
        return None

    def _get(d: dict, path: str) -> float | None:
        parts = path.split(".")
        val = d
        for p in parts:
            if not isinstance(val, dict):
                return None
            val = val.get(p)
        try:
            return float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    prior_runs = history[-(n_runs + 1):-1]
    vals = [v for r in prior_runs if (v := _get(r, metric_path)) is not None]
    return sum(vals) / len(vals) if vals else None


def detect_drift(
    history: list[dict[str, Any]],
    n_baseline_runs: int = 3,
) -> list[DriftAlert]:
    """
    Compare the most recent eval run against a rolling baseline of n_baseline_runs.

    Returns a list of DriftAlert objects (empty = no regressions detected).
    """
    if len(history) < 2:
        return []

    current = history[-1]
    agg = current["aggregate"]
    fname = current["file"]
    alerts: list[DriftAlert] = []

    # ── Scalar metrics ────────────────────────────────────────────────────────
    scalar_checks = [
        ("pass_rate",            "aggregate.pass_rate",            "pass_rate_drop",          "warning"),
        ("avg_answer_relevance", "aggregate.avg_answer_relevance", "answer_relevance_drop",   "warning"),
        ("avg_intent_accuracy",  "aggregate.avg_intent_accuracy",  "intent_accuracy_drop",    "warning"),
        ("avg_gold_qo_score",    "aggregate.avg_gold_qo_score",    "gold_qo_score_drop",      "warning"),
    ]

    for metric_name, path, threshold_key, severity in scalar_checks:
        current_val = _rolling_baseline([current], path.replace("aggregate.", "aggregate."), 1)
        # Re-extract directly
        keys = path.split(".")
        val: Any = current
        for k in keys:
            val = (val or {}).get(k)
        if val is None:
            continue
        current_float = float(val)

        baseline = _rolling_baseline(history, path, n_baseline_runs)
        if baseline is None:
            continue

        threshold = DRIFT_THRESHOLDS[threshold_key]
        drop = baseline - current_float  # positive = regression
        if drop > threshold:
            alerts.append(DriftAlert(
                severity=severity,
                metric=metric_name,
                current_value=round(current_float, 4),
                baseline_value=round(baseline, 4),
                delta=round(-drop, 4),
                threshold=threshold,
                run_file=fname,
            ))

    # ── Latency spike ─────────────────────────────────────────────────────────
    current_p95 = agg.get("latency_p95_ms")
    baseline_p95 = _rolling_baseline(history, "aggregate.latency_p95_ms", n_baseline_runs)
    if current_p95 is not None and baseline_p95 is not None:
        spike = float(current_p95) - float(baseline_p95)
        threshold = DRIFT_THRESHOLDS["latency_p95_spike_ms"]
        if spike > threshold:
            alerts.append(DriftAlert(
                severity="warning",
                metric="latency_p95_ms",
                current_value=int(current_p95),
                baseline_value=int(baseline_p95),
                delta=int(spike),
                threshold=threshold,
                run_file=fname,
            ))

    # ── Hard fail spike ───────────────────────────────────────────────────────
    current_hf = agg.get("hard_fail_count", 0)
    baseline_hf = _rolling_baseline(history, "aggregate.hard_fail_count", n_baseline_runs)
    if current_hf is not None and baseline_hf is not None:
        spike = float(current_hf) - float(baseline_hf)
        threshold = DRIFT_THRESHOLDS["hard_fail_spike"]
        if spike > threshold:
            alerts.append(DriftAlert(
                severity="critical",
                metric="hard_fail_count",
                current_value=int(current_hf),
                baseline_value=round(baseline_hf, 1),
                delta=round(spike, 1),
                threshold=threshold,
                run_file=fname,
            ))

    # ── Per-tag pass rate regression ──────────────────────────────────────────
    current_by_tag = agg.get("by_tag", {})
    threshold_tag = DRIFT_THRESHOLDS["tag_pass_rate_drop"]

    for tag, tag_agg in current_by_tag.items():
        if tag.startswith("_"):
            continue
        current_tag_pr = tag_agg.get("pass_rate")
        if current_tag_pr is None:
            continue

        # Build baseline for this tag from prior runs
        prior_tag_prs = []
        for run in history[-(n_baseline_runs + 1):-1]:
            tag_data = run["aggregate"].get("by_tag", {}).get(tag, {})
            pr = tag_data.get("pass_rate")
            if pr is not None:
                prior_tag_prs.append(float(pr))

        if not prior_tag_prs:
            continue

        baseline_tag_pr = sum(prior_tag_prs) / len(prior_tag_prs)
        drop = baseline_tag_pr - float(current_tag_pr)
        if drop > threshold_tag:
            alerts.append(DriftAlert(
                severity="warning",
                metric=f"tag[{tag}].pass_rate",
                current_value=round(float(current_tag_pr), 3),
                baseline_value=round(baseline_tag_pr, 3),
                delta=round(-drop, 3),
                threshold=threshold_tag,
                run_file=fname,
                detail={"tag": tag, "n": tag_agg.get("n", 0)},
            ))

    return alerts


def score_trend(
    history: list[dict[str, Any]],
    metric: str = "avg_answer_relevance",
) -> dict[str, Any]:
    """
    Compute trend statistics for a scalar metric across all history runs.

    Returns {"values": [float], "files": [str], "slope": float, "direction": str}.
    Direction: "improving" | "degrading" | "stable".
    """
    vals, files = [], []
    for run in history:
        v = run["aggregate"].get(metric)
        if v is not None:
            vals.append(float(v))
            files.append(run["file"])

    if len(vals) < 2:
        return {"values": vals, "files": files, "slope": 0.0, "direction": "insufficient_data"}

    # Simple linear slope via least squares
    n = len(vals)
    x_mean = (n - 1) / 2
    y_mean = sum(vals) / n
    numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vals))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = numerator / denominator if denominator else 0.0

    direction = "improving" if slope > 0.002 else ("degrading" if slope < -0.002 else "stable")

    return {
        "metric": metric,
        "values": [round(v, 4) for v in vals],
        "files": files,
        "slope": round(slope, 5),
        "direction": direction,
        "latest": round(vals[-1], 4),
        "earliest": round(vals[0], 4),
        "total_delta": round(vals[-1] - vals[0], 4),
    }


def stage_failure_shift(
    history: list[dict[str, Any]],
    n_baseline_runs: int = 3,
) -> list[dict[str, Any]]:
    """
    Detect which pipeline stages gained or lost failures in the latest run
    vs. the rolling baseline. Returns sorted list of {stage, delta, severity}.
    """
    if len(history) < 2:
        return []

    current_counts = history[-1]["aggregate"].get("stage_failure_counts", {})
    prior_counts: dict[str, list[int]] = {}
    for run in history[-(n_baseline_runs + 1):-1]:
        for stage, cnt in run["aggregate"].get("stage_failure_counts", {}).items():
            prior_counts.setdefault(stage, []).append(int(cnt))

    shifts = []
    all_stages = set(current_counts) | set(prior_counts)
    for stage in all_stages:
        curr = int(current_counts.get(stage, 0))
        prior_vals = prior_counts.get(stage, [0])
        baseline = sum(prior_vals) / len(prior_vals)
        delta = curr - baseline
        if abs(delta) >= 1:
            shifts.append({
                "stage": stage,
                "current": curr,
                "baseline": round(baseline, 1),
                "delta": round(delta, 1),
                "severity": "critical" if delta > 3 else ("warning" if delta > 1 else "info"),
            })

    return sorted(shifts, key=lambda x: -abs(x["delta"]))


def run_full_drift_report(eval_dir: Path, n_baseline_runs: int = 3) -> dict[str, Any]:
    """
    Load history and run all drift checks. Returns a complete drift report dict.
    """
    history = load_eval_history(eval_dir)
    if not history:
        return {"error": "no eval runs found", "alerts": []}

    alerts = detect_drift(history, n_baseline_runs)
    ar_trend = score_trend(history, "avg_answer_relevance")
    pr_trend = score_trend(history, "pass_rate")
    stage_shifts = stage_failure_shift(history, n_baseline_runs)

    return {
        "latest_run": history[-1]["file"],
        "n_runs_loaded": len(history),
        "n_alerts": len(alerts),
        "alerts": [
            {
                "severity": a.severity,
                "metric": a.metric,
                "current": a.current_value,
                "baseline": a.baseline_value,
                "delta": a.delta,
                "threshold": a.threshold,
            }
            for a in alerts
        ],
        "trends": {
            "answer_relevance": ar_trend,
            "pass_rate": pr_trend,
        },
        "stage_failure_shifts": stage_shifts,
        "by_tag_latest": history[-1]["aggregate"].get("by_tag", {}),
    }
