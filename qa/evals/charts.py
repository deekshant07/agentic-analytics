"""
qa/evals/charts.py — Chart type and quality evaluation.

Scores chart_json outputs from eval traces against expected chart types
for each analysis_type and column structure. Parses Plotly figure JSON.

Usage:
    from qa.evals.charts import score_chart, EXPECTED_CHART_TYPES

    result = score_chart(
        analysis_type="segment",
        df_columns=["platform", "users"],
        chart_json=trace["chart_json"],
    )
    # result.score, result.expected, result.got, result.detail
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ── Expected chart types per analysis_type ────────────────────────────────────
# Each entry: set of acceptable Plotly trace types, plus an optional set of
# semantic hints (bar_horizontal, bar_grouped, etc.) for stricter checks.
#
# Plotly trace types: "bar", "scatter", "pie", "heatmap", "sankey", "funnel"
# "scatter" with mode="lines" is a line chart (Plotly reuses the type).
EXPECTED_CHART_TYPES: dict[str, dict[str, Any]] = {
    "metric": {
        "accepted": {"bar", "scatter"},  # trend → scatter/line; single → bar
        "preferred": "scatter",
        "note": "Trend queries → line (scatter); single-period → bar",
    },
    "segment": {
        "accepted": {"bar"},
        "preferred": "bar",
        "note": "Categorical breakdowns are always bar charts",
    },
    "funnel": {
        "accepted": {"bar", "funnel"},
        "preferred": "bar",
        "note": "Funnel steps shown as horizontal bar or funnel trace",
    },
    "retention": {
        "accepted": {"heatmap", "scatter"},
        "preferred": "heatmap",
        "note": "Retention heatmap preferred; survival lines use scatter",
    },
    "behavioral_cohort": {
        "accepted": {"bar"},
        "preferred": "bar",
        "note": "Cohort size comparisons use bar",
    },
    "stickiness": {
        "accepted": {"bar", "scatter"},
        "preferred": "bar",
        "note": "DAU/MAU ratio; bar or line",
    },
    "user_lifecycle": {
        "accepted": {"pie", "bar"},
        "preferred": "pie",
        "note": "Lifecycle stage composition → pie/donut; breakdown → bar",
    },
    "diagnose": {
        "accepted": {"bar", "scatter", "pie"},
        "preferred": None,
        "note": "Diagnose produces root-cause charts — any type is acceptable",
    },
    "journey": {
        "accepted": {"sankey"},
        "preferred": "sankey",
        "note": "User journey → Sankey diagram",
    },
    "time_between": {
        "accepted": {"bar", "scatter"},
        "preferred": "bar",
        "note": "Time-to-event distribution → bar histogram or scatter",
    },
    "custom_split": {
        "accepted": {"bar"},
        "preferred": "bar",
        "note": "Comparison between two cohorts → grouped bar",
    },
}

# Catch-all for unknown analysis types — any trace is acceptable
_DEFAULT_ACCEPTED: set[str] = {"bar", "scatter", "pie", "heatmap", "sankey"}


@dataclass
class ChartScoreResult:
    score: float
    expected_types: set[str]
    got_types: list[str]
    preferred: str | None
    analysis_type: str
    detail: dict[str, Any] = field(default_factory=dict)


def _extract_trace_types(chart_json: str | dict | None) -> list[str]:
    """Parse Plotly figure JSON and return the list of trace types present."""
    if not chart_json:
        return []
    try:
        if isinstance(chart_json, str):
            fig = json.loads(chart_json)
        else:
            fig = chart_json
        data = fig.get("data", [])
        return [t.get("type", "unknown") for t in data if isinstance(t, dict)]
    except Exception:
        return []


def _has_time_axis(chart_json: str | dict | None) -> bool:
    """Return True if any axis label or tickformat hints at a time series."""
    if not chart_json:
        return False
    try:
        raw = chart_json if isinstance(chart_json, str) else json.dumps(chart_json)
        return any(kw in raw.lower() for kw in ("date", "month", "week", "period", "time"))
    except Exception:
        return False


def _has_categorical_axis(columns: list[str]) -> bool:
    """Return True if columns suggest a categorical breakdown."""
    cat_hints = {"platform", "channel", "city", "region", "state", "segment",
                 "cohort", "type", "category", "source", "medium", "country"}
    return any(c.lower() in cat_hints for c in columns)


def score_chart(
    analysis_type: str,
    df_columns: list[str],
    chart_json: str | dict | None,
    *,
    strict: bool = False,
) -> ChartScoreResult:
    """
    Score chart correctness for a single eval trace.

    Args:
        analysis_type: The QO analysis_type (e.g. "metric", "segment").
        df_columns: Column names from the executed DataFrame.
        chart_json: Plotly figure JSON string or dict (from trace["chart_json"]).
        strict: If True, require the preferred chart type (not just any accepted type).

    Returns:
        ChartScoreResult with score in [0.0, 1.0].

    Scoring rubric:
        0.0 — no chart generated (chart_json is None/empty)
        0.5 — chart generated but wrong type
        0.8 — acceptable type (in accepted set, not the preferred)
        1.0 — preferred type or all types in accepted set
    """
    spec = EXPECTED_CHART_TYPES.get(analysis_type)
    accepted = spec["accepted"] if spec else _DEFAULT_ACCEPTED
    preferred = spec["preferred"] if spec else None

    got_types = _extract_trace_types(chart_json)

    if not chart_json:
        return ChartScoreResult(
            score=0.0,
            expected_types=accepted,
            got_types=[],
            preferred=preferred,
            analysis_type=analysis_type,
            detail={"reason": "no_chart_generated"},
        )

    if not got_types:
        return ChartScoreResult(
            score=0.0,
            expected_types=accepted,
            got_types=[],
            preferred=preferred,
            analysis_type=analysis_type,
            detail={"reason": "chart_json_unparseable"},
        )

    got_set = set(got_types)
    matches_accepted = bool(got_set & accepted)
    matches_preferred = preferred in got_set if preferred else matches_accepted

    if strict:
        score = 1.0 if matches_preferred else (0.5 if matches_accepted else 0.0)
    else:
        score = 1.0 if matches_preferred else (0.8 if matches_accepted else 0.5)

    return ChartScoreResult(
        score=score,
        expected_types=accepted,
        got_types=got_types,
        preferred=preferred,
        analysis_type=analysis_type,
        detail={
            "matches_preferred": matches_preferred,
            "matches_accepted": matches_accepted,
            "has_time_axis": _has_time_axis(chart_json),
            "categorical_columns": _has_categorical_axis(df_columns),
            "spec_note": spec["note"] if spec else "unknown analysis_type",
        },
    )


def score_chart_from_trace(trace: dict[str, Any]) -> ChartScoreResult | None:
    """
    Convenience wrapper: extract everything needed from an eval trace dict.

    Returns None if the trace has no execution stage (specialist routes).
    """
    execution = trace.get("stages", {}).get("execution") or {}
    if not execution.get("ok") and not execution.get("specialist_route"):
        return None

    analysis_type = (
        (trace.get("stages", {}).get("orchestrator") or {}).get("analysis_type") or ""
    )
    df_columns = execution.get("columns", [])
    chart_json = trace.get("chart_json")
    return score_chart(analysis_type, df_columns, chart_json)


def batch_score_charts(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Score charts across all eval traces and return aggregate stats.

    Returns:
        {
            "n_scored": int,
            "n_no_chart": int,
            "avg_chart_score": float,
            "by_analysis_type": {type: {"n": int, "avg": float}},
            "wrong_type_cases": [{"question": str, "got": list, "expected": set}],
        }
    """
    results: list[tuple[dict, ChartScoreResult]] = []
    no_chart_count = 0

    for t in traces:
        r = score_chart_from_trace(t)
        if r is None:
            continue
        if not t.get("chart_json"):
            no_chart_count += 1
        results.append((t, r))

    if not results:
        return {"n_scored": 0, "n_no_chart": no_chart_count, "avg_chart_score": 0.0,
                "by_analysis_type": {}, "wrong_type_cases": []}

    scores = [r.score for _, r in results]
    avg = sum(scores) / len(scores)

    by_type: dict[str, dict] = {}
    for _, r in results:
        bt = by_type.setdefault(r.analysis_type, {"n": 0, "sum": 0.0})
        bt["n"] += 1
        bt["sum"] += r.score

    wrong = [
        {
            "question": t.get("question", ""),
            "analysis_type": r.analysis_type,
            "got": r.got_types,
            "expected": list(r.expected_types),
        }
        for t, r in results
        if r.score < 0.8
    ]

    return {
        "n_scored": len(results),
        "n_no_chart": no_chart_count,
        "avg_chart_score": round(avg, 3),
        "by_analysis_type": {
            k: {"n": v["n"], "avg": round(v["sum"] / v["n"], 3)}
            for k, v in sorted(by_type.items())
        },
        "wrong_type_cases": wrong,
    }
