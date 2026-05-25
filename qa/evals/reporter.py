"""
qa/evals/reporter.py — Unified eval report generation.

Generates markdown and structured reports from eval result JSON files.
Designed for: CI/CD output, PR comments, daily digest emails.

Usage:
    from qa.evals.reporter import generate_markdown_report, compare_runs

    report = generate_markdown_report(Path("qa/eval_results/eval_20260525_120000.json"))
    print(report)

    diff = compare_runs(
        Path("qa/eval_results/eval_20260524_120000.json"),
        Path("qa/eval_results/eval_20260525_120000.json"),
    )
    print(diff)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.1%}"


def _score(v: float | None, decimals: int = 3) -> str:
    if v is None:
        return "N/A"
    return f"{v:.{decimals}f}"


def _delta_str(delta: float, positive_is_good: bool = True) -> str:
    """Format a delta with sign and colour hint (markdown bold for regressions)."""
    sign = "+" if delta >= 0 else ""
    s = f"{sign}{delta:.3f}"
    if positive_is_good and delta < -0.02:
        return f"**{s}** ⚠️"
    if not positive_is_good and delta > 0.02:
        return f"**{s}** ⚠️"
    return s


# ── Single-run report ─────────────────────────────────────────────────────────

def generate_markdown_report(eval_path: Path) -> str:
    """
    Generate a markdown report from a single eval result JSON.

    Includes: summary table, stage failure breakdown, per-tag pass rates,
    top failures, fixup activity.
    """
    data = _load(eval_path)
    agg = data.get("aggregate", {})
    meta = data.get("run_metadata", {})
    trend = data.get("trend", {})
    cases = data.get("cases", [])

    lines: list[str] = []
    lines.append(f"# Eval Report — `{eval_path.name}`\n")
    lines.append(f"Generated: `{data.get('generated_at', 'unknown')}`  ")
    lines.append(f"Provider: `{meta.get('eval_provider', 'default')}`\n")

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append("## Summary\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Pass rate | **{_pct(agg.get('pass_rate'))}** ({agg.get('pass_count')}/{agg.get('n_cases')} cases) |")
    lines.append(f"| Avg answer relevance | {_score(agg.get('avg_answer_relevance'))} |")
    lines.append(f"| Avg intent accuracy | {_score(agg.get('avg_intent_accuracy'))} |")
    lines.append(f"| Avg gold QO score | {_score(agg.get('avg_gold_qo_score'))} |")
    lines.append(f"| Hard failures | {agg.get('hard_fail_count', 0)} |")
    lines.append(f"| Deterministic coverage | {_pct(agg.get('deterministic_coverage'))} |")
    lines.append(f"| Latency p50 / p95 | {agg.get('latency_p50_ms', 0)} ms / {agg.get('latency_p95_ms', 0)} ms |")
    lines.append(f"| Estimated cost | ${agg.get('estimated_cost_usd', 0):.4f} |")

    if trend:
        delta = trend.get("answer_relevance_delta", 0)
        count_delta = trend.get("pass_count_delta", 0)
        sign = "+" if count_delta >= 0 else ""
        lines.append(f"\n**vs previous run** (`{trend.get('vs_previous_run', '?')}`): "
                     f"AR delta={delta:+.3f}, cases={sign}{count_delta}")

    # ── Stage failures ────────────────────────────────────────────────────────
    stage_counts = agg.get("stage_failure_counts", {})
    if stage_counts:
        lines.append("\n## Failures by Pipeline Stage\n")
        lines.append("| Stage | Count |")
        lines.append("|-------|-------|")
        for stage, cnt in sorted(stage_counts.items(), key=lambda x: -x[1]):
            lines.append(f"| `{stage}` | {cnt} |")

    # ── Per-tag ───────────────────────────────────────────────────────────────
    by_tag = agg.get("by_tag", {})
    if by_tag:
        lines.append("\n## Pass Rate by Tag\n")
        lines.append("| Tag | N | Pass rate | Avg AR | Avg intent |")
        lines.append("|-----|---|-----------|--------|------------|")
        for tag, tv in sorted(by_tag.items()):
            lines.append(
                f"| `{tag}` | {tv['n']} | {_pct(tv.get('pass_rate'))} "
                f"| {_score(tv.get('avg_answer_relevance'))} "
                f"| {_score(tv.get('avg_intent_accuracy'))} |"
            )

    # ── Top failures ──────────────────────────────────────────────────────────
    failed = [t for t in cases if not t.get("pass")]
    failed_sorted = sorted(failed, key=lambda t: t.get("scores", {}).get("answer_relevance", 1.0))
    if failed_sorted:
        lines.append(f"\n## Top Failures ({len(failed)} total)\n")
        lines.append("| Question | Stage | AR | Intent |")
        lines.append("|----------|-------|-----|--------|")
        for t in failed_sorted[:15]:
            q = (t.get("question") or "")[:60]
            stage = t.get("failure_stage") or "unknown"
            ar = _score(t.get("scores", {}).get("answer_relevance"))
            ia = _score(t.get("scores", {}).get("intent_accuracy"))
            lines.append(f"| {q} | `{stage}` | {ar} | {ia} |")

    # ── Fixup activity ────────────────────────────────────────────────────────
    fixup_fires = agg.get("fixup_fire_counts", {})
    if fixup_fires:
        lines.append("\n## Fixup Activation\n")
        lines.append("| Fixup | Times fired |")
        lines.append("|-------|-------------|")
        for name, cnt in sorted(fixup_fires.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"| `{name}` | {cnt} |")

    # ── Gold assertion stats ──────────────────────────────────────────────────
    gas = agg.get("gold_assertion_stats", {})
    if gas:
        lines.append("\n## Gold Assertion Coverage\n")
        lines.append(f"- Gold QO cases: {gas.get('gold_qo_cases', 0)}")
        lines.append(f"- Gold SQL cases: {gas.get('gold_sql_cases', 0)}")
        lines.append(f"- Gold result cases: {gas.get('gold_result_cases', 0)}")

    return "\n".join(lines)


# ── Two-run comparison ────────────────────────────────────────────────────────

def compare_runs(old_path: Path, new_path: Path) -> str:
    """
    Generate a markdown diff report comparing two eval runs.

    Highlights regressions (bold + warning emoji) and improvements.
    """
    old = _load(old_path)
    new = _load(new_path)
    oa, na = old.get("aggregate", {}), new.get("aggregate", {})

    lines: list[str] = []
    lines.append(f"# Eval Comparison\n")
    lines.append(f"- **Before**: `{old_path.name}` (pass {_pct(oa.get('pass_rate'))})")
    lines.append(f"- **After**: `{new_path.name}` (pass {_pct(na.get('pass_rate'))})\n")

    # ── Scalar comparison ─────────────────────────────────────────────────────
    lines.append("## Metric Changes\n")
    lines.append("| Metric | Before | After | Delta |")
    lines.append("|--------|--------|-------|-------|")

    scalar_metrics = [
        ("pass_rate",              True,  _pct),
        ("avg_answer_relevance",   True,  _score),
        ("avg_intent_accuracy",    True,  _score),
        ("avg_gold_qo_score",      True,  _score),
        ("hard_fail_count",        False, str),
        ("latency_p95_ms",         False, str),
    ]
    for key, positive_good, fmt in scalar_metrics:
        ov = oa.get(key)
        nv = na.get(key)
        if ov is None and nv is None:
            continue
        try:
            delta = float(nv or 0) - float(ov or 0)
        except (TypeError, ValueError):
            delta = 0.0
        lines.append(f"| {key} | {fmt(ov)} | {fmt(nv)} | {_delta_str(delta, positive_good)} |")

    # ── Stage failure comparison ──────────────────────────────────────────────
    o_stage = oa.get("stage_failure_counts", {})
    n_stage = na.get("stage_failure_counts", {})
    all_stages = sorted(set(o_stage) | set(n_stage))
    if all_stages:
        lines.append("\n## Stage Failure Changes\n")
        lines.append("| Stage | Before | After | Delta |")
        lines.append("|-------|--------|-------|-------|")
        for stage in all_stages:
            ov = int(o_stage.get(stage, 0))
            nv = int(n_stage.get(stage, 0))
            delta = nv - ov
            d_str = f"+{delta}" if delta > 0 else str(delta)
            flag = " ⚠️" if delta > 2 else (" ✅" if delta < -1 else "")
            lines.append(f"| `{stage}` | {ov} | {nv} | {d_str}{flag} |")

    # ── Per-tag comparison ────────────────────────────────────────────────────
    o_tags = oa.get("by_tag", {})
    n_tags = na.get("by_tag", {})
    all_tags = sorted(set(o_tags) | set(n_tags))
    if all_tags:
        lines.append("\n## Tag Pass Rate Changes\n")
        lines.append("| Tag | Before | After | Delta |")
        lines.append("|-----|--------|-------|-------|")
        for tag in all_tags:
            o_pr = o_tags.get(tag, {}).get("pass_rate")
            n_pr = n_tags.get(tag, {}).get("pass_rate")
            if o_pr is None and n_pr is None:
                continue
            try:
                delta = float(n_pr or 0) - float(o_pr or 0)
            except (TypeError, ValueError):
                delta = 0.0
            lines.append(
                f"| `{tag}` | {_pct(o_pr)} | {_pct(n_pr)} | {_delta_str(delta, True)} |"
            )

    # ── Newly passing / newly failing ─────────────────────────────────────────
    old_cases = {t["question"]: t.get("pass") for t in old.get("cases", [])}
    new_cases = {t["question"]: t.get("pass") for t in new.get("cases", [])}

    newly_passing = [q for q in new_cases if new_cases[q] and not old_cases.get(q)]
    newly_failing  = [q for q in new_cases if not new_cases[q] and old_cases.get(q)]

    if newly_passing:
        lines.append(f"\n## Newly Passing ({len(newly_passing)})\n")
        for q in newly_passing[:10]:
            lines.append(f"- ✅ {q}")

    if newly_failing:
        lines.append(f"\n## Newly Failing ({len(newly_failing)}) ⚠️\n")
        for q in newly_failing[:10]:
            lines.append(f"- ❌ {q}")

    return "\n".join(lines)


# ── Multi-run trend table ─────────────────────────────────────────────────────

def generate_trend_table(eval_dir: Path, n_runs: int = 10) -> str:
    """
    Generate a markdown table showing key metrics across the last N eval runs.
    Useful for tracking improvement over time in CI artifacts.
    """
    from qa.evals.drift import load_eval_history
    history = load_eval_history(eval_dir, n_most_recent=n_runs)
    if not history:
        return "_No eval runs found._"

    lines = ["## Eval Trend (last runs)\n"]
    lines.append("| Run | Pass rate | Avg AR | Intent | Gold QO | p95 ms |")
    lines.append("|-----|-----------|--------|--------|---------|--------|")

    for run in history:
        agg = run["aggregate"]
        lines.append(
            f"| `{run['file'][:30]}` "
            f"| {_pct(agg.get('pass_rate'))} "
            f"| {_score(agg.get('avg_answer_relevance'))} "
            f"| {_score(agg.get('avg_intent_accuracy'))} "
            f"| {_score(agg.get('avg_gold_qo_score'))} "
            f"| {agg.get('latency_p95_ms', '?')} |"
        )

    return "\n".join(lines)
