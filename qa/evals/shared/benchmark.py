"""
eval_benchmark.py — 100-question evaluation harness with full traces.

Evaluates on:
  - query correctness (compile + execution + optional column hints)
  - query routing / intent (resolver + orchestrator analysis_type)
  - optional **gold_qo** slot checks (per-case rubric)
  - per-**tag** aggregates (metric_trend, segment, custom_split, advanced, …)
  - summary relevance + blended answer relevance

Post-orchestration steps match **production** ``ui.pipeline.get_sql`` fixups
(see ``qa.eval_production``). Metric list includes ``ce_*`` custom events like chat.

Outputs a JSON report under qa/eval_results/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.eval_env import load_project_dotenv
from qa.eval_production import (
    apply_post_orchestrator_fixups,
    build_metrics_like_chat,
    configure_sql_guards_from_catalog,
    qo_snapshot,
)

_env_paths, _env_diag = load_project_dotenv(ROOT)

from core.sql.compilers import (
    compile_custom_event_retention,
    compile_custom_event_segment,
    compile_custom_event_single,
    compile_custom_event_split,
    compile_query,
)
from core.pipeline.orchestrator import orchestrate
from core.sql.query_object import QueryObject
from core.semantic.resolver_policy import resolve_query_policy


def _is_openai_transient_rate_limit(err: BaseException) -> bool:
    """HTTP 429 / quota / overload — safe to retry with backoff.
    Billing quota exhaustion (insufficient_quota) is NOT transient — fail fast."""
    s = f"{type(err).__name__} {err}".lower()
    if "insufficient_quota" in s:
        return False
    return any(
        x in s
        for x in (
            "429",
            "rate_limit",
            "rate limit",
            "too many requests",
            "tpm",
            "rpm",
            "requests per min",
            "tokens per min",
            "503",
            "502",
            "overloaded",
            "capacity",
            "timeout",
            "timed out",
        )
    )


def _orchestrate_with_retry(
    *,
    question: str,
    catalog: dict,
    sampled_values: dict,
    openai_api_key: str | None,
    history: list[dict],
    eval_provider: str | None = None,
    max_retries: int = 10,
) -> QueryObject:
    """Retry orchestrator on transient rate limits. eval_provider overrides LLM_PROVIDER."""
    from core.infra.llm import make_llm_client, resolve_model
    last_err: BaseException | None = None
    for attempt in range(max_retries):
        try:
            return orchestrate(
                question=question,
                catalog=catalog,
                sampled_values=sampled_values,
                openai_api_key=openai_api_key,
                history=history,
                hypothesis_doc=None,
                eval_provider=eval_provider,
            )
        except Exception as e:
            last_err = e
            if _is_openai_transient_rate_limit(e):
                delay = min(120.0, 8.0 * (2**attempt))
                time.sleep(delay)
                continue
            raise
    assert last_err is not None
    raise last_err


CATALOG_PATH = ROOT / "catalog.json"
DB_PATH = ROOT / "jupiter.duckdb"
OUT_DIR = ROOT / "qa" / "eval_results"


def _preflight_openai_key(key: str) -> None:
    """One lightweight API call so a bad key fails fast instead of after 100 cases."""
    try:
        from openai import OpenAI

        OpenAI(api_key=key, timeout=20.0).models.list()
    except Exception as e:
        msg = str(e).lower()
        if "401" in str(e) or "invalid_api_key" in msg or "incorrect api key" in msg:
            raise RuntimeError(
                "OpenAI returned 401 before the benchmark started — the key in your environment "
                "is not accepted (revoked, typo, or wrong project). "
                f"Update `{ROOT / '.env'}` with a fresh key from "
                "https://platform.openai.com/account/api-keys\n"
                f"Loader diagnostics: {_env_diag}"
            ) from e
        raise


@dataclass
class EvalCase:
    question: str
    expected_analysis_type: str | None = None
    expected_route: str | None = None
    expected_summary_tokens: list[str] | None = None
    expected_columns_any: list[str] | None = None
    # Taxonomy for dashboards and per-bucket pass rates (e.g. metric_trend, segment).
    tags: list[str] = field(default_factory=list)
    # Optional rubric: check orchestrator+fixup QO slots. Keys may include:
    #   analysis_type, event, event_b, metric_id, metric_variant,
    #   filters_contain: {col: value}, breakdown (str).
    gold_qo: dict[str, Any] | None = None
    # Optional SQL structural assertions — deterministic, never needs an LLM judge.
    # Catches compiler bugs that produce zero rows without a SQL syntax error.
    # Keys:
    #   required: [str]               — patterns that MUST appear in the full SQL
    #   forbidden: [str]              — patterns that must NOT appear anywhere in SQL
    #   required_in_cte: {name: [str]} — patterns required inside a named CTE block
    #   forbidden_in_cte: {name: [str]} — patterns forbidden inside a named CTE block
    gold_sql: dict[str, Any] | None = None
    # Optional result-value assertions — run against the actual DB result DataFrame.
    # Catches semantic errors (activation_rate > 100%, wrong counts, empty results).
    # Keys:
    #   col_bounds: {col: [min, max]}  — numeric column must be within [min, max]
    #   row_count_min: int             — at least N rows returned
    #   row_count_max: int             — at most N rows returned
    #   col_present: [str]             — all named columns must exist
    #   col_not_null: [str]            — named columns must have no NULL / NaN values
    gold_result: dict[str, Any] | None = None


@dataclass
class MultiTurnEvalCase:
    """
    A sequence of turns that tests context-preservation across a conversation.

    Each turn is a dict with:
      question: str                   — user message
      expected_analysis_type: str     — optional intent check
      gold_qo: dict | None            — optional QO slot checks
      gold_sql: dict | None           — optional SQL structural checks
      gold_result: dict | None        — optional result value checks

    History accumulates across turns — the QO from each turn is fed as history
    to the next, exactly as the production pipeline does.
    """
    turns: list[dict[str, Any]]
    tags: list[str] = field(default_factory=list)
    description: str = ""


def load_catalog_and_sampled() -> tuple[dict, dict[str, dict[str, list[str]]]]:
    catalog = json.loads(CATALOG_PATH.read_text())
    sampled: dict[str, dict[str, list[str]]] = {}
    if not DB_PATH.exists():
        return catalog, sampled
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).df()["table_name"].tolist()
        skip = {"user_id", "session_id", "event_id", "transaction_id", "device_id"}
        for tname in tables:
            sampled[tname] = {}
            cols_df = conn.execute(f"DESCRIBE {tname}").df()
            for _, row in cols_df.iterrows():
                cname, ctype = row["column_name"], row["column_type"]
                if ctype != "VARCHAR" or cname in skip:
                    continue
                try:
                    vals = conn.execute(
                        f"SELECT DISTINCT {cname} FROM {tname} WHERE {cname} IS NOT NULL LIMIT 20"
                    ).df()[cname].tolist()
                    sampled[tname][cname] = [str(v) for v in vals if v is not None]
                except Exception:
                    pass
    finally:
        conn.close()
    return catalog, sampled


def choose_sql(decision, qo: QueryObject, metrics: list[dict]) -> tuple[str, str | None]:
    """Match ``ui.pipeline.get_sql`` compile routing (custom routes + ``compile_query``)."""
    mce = list(getattr(decision, "matched_custom_events", None) or [])
    if decision.route == "custom_split" and mce:
        ce_b = mce[1] if len(mce) > 1 else None
        sql, metric_name = compile_custom_event_split(mce[0], ce_b, qo), None
    elif decision.route == "custom_single" and mce:
        if qo.analysis_type == "retention":
            sql, metric_name = compile_custom_event_retention(mce[0], qo), None
        elif qo.analysis_type == "behavioral_cohort":
            sql, metric_name = compile_query(qo, metrics)
        else:
            sql, metric_name = compile_custom_event_single(mce[0], qo), None
    elif decision.route == "custom_segment" and mce:
        sql, metric_name = compile_custom_event_segment(mce[0], qo), None
    elif decision.route == "custom_retention" and mce:
        sql, metric_name = compile_custom_event_retention(mce[0], qo), None
    else:
        sql, metric_name = compile_query(qo, metrics)

    if str(getattr(qo, "analysis_type", "") or "").strip().lower() == "diagnose":
        if sql and str(sql).strip() not in ("", "__analyst__"):
            sql = "__diagnose__"
    return sql, metric_name


def gate_allow_execute(decision, qo: QueryObject) -> tuple[bool, str]:
    conf = float(getattr(decision, "confidence", 0.0) or 0.0)
    route = getattr(decision, "route", "") or ""
    matched_ce = getattr(decision, "matched_custom_events", None) or []
    has_anchor = bool(
        getattr(qo, "metric_id", None)
        or getattr(qo, "event", None)
        or getattr(qo, "analysis_type", "") in {"funnel", "retention", "diagnose", "journey"}
        or (route in {"custom_split", "custom_single", "custom_segment"} and len(matched_ce) >= 1)
    )
    if conf >= 0.60:
        return True, "resolver_confidence"
    if has_anchor:
        return True, "strong_anchor_override"
    return False, "low_confidence"


def _norm_tokens(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "for", "in", "on", "of", "to", "show", "share",
        "me", "users", "user", "last", "this", "that", "what", "is", "are",
    }
    toks = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
    return {t for t in toks if t and t not in stop}


_MONTH_HINT_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)


_SUMMARY_STOP = {
    "show", "share", "me", "the", "a", "an", "for", "in", "on", "of", "to",
    "last", "this", "that", "what", "is", "are", "by", "and", "or", "vs",
    "versus", "between", "why", "did", "did", "how", "much", "many",
    "transacting", "active", "users", "user",
}


def _extract_subject_words(question: str) -> list[str]:
    """Pull the meaningful noun/metric words from a question for use in the summary."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", question.lower())
    return [w for w in words if w not in _SUMMARY_STOP and len(w) >= 3]


def build_summary(df: pd.DataFrame, question: str | None = None) -> str:
    """
    Generate a human-readable summary of query results for the LLM judge.
    Produces natural sentences rather than terse stat-dumps so the judge can
    correctly assess whether the data answers the question.
    """
    if df is None or df.empty:
        subject = " ".join(_extract_subject_words(question or "")[:5]) if question else ""
        return f"No data returned for: {subject or (question or '')[:80]}."

    cols = list(df.columns)
    n = len(df)
    num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    time_cols = [c for c in cols if str(c).lower() in {"date", "week", "month", "day", "period", "month_name"}]
    cat_cols  = [c for c in cols if c not in num_cols and c not in time_cols]
    pct_cols  = [c for c in cols if any(k in str(c).lower() for k in ("pct", "rate", "ratio"))]

    q = (question or "").lower()

    # ── Trend query: time + value (needs ≥2 rows to be a real trend) ───────────
    if time_cols and num_cols and n >= 2:
        t_col = time_cols[0]
        v_col = pct_cols[0] if pct_cols else num_cols[0]
        t_first = str(df[t_col].iloc[0])[:10]
        t_last  = str(df[t_col].iloc[-1])[:10]
        v_first = float(df[v_col].iloc[0])
        v_last  = float(df[v_col].iloc[-1])
        delta_pct = ((v_last - v_first) / max(abs(v_first), 1.0)) * 100
        arrow = "↑" if delta_pct > 2 else "↓" if delta_pct < -2 else "→"
        fmt = lambda v: f"{v:.1f}%" if pct_cols else f"{v:,.0f}"
        return (
            f"{v_col} from {t_first} to {t_last}: "
            f"{fmt(v_first)} {arrow} {fmt(v_last)} ({delta_pct:+.0f}%). "
            f"{n} data points."
        )

    # ── Breakdown: categorical dimension + value ────────────────────────────────
    if cat_cols and num_cols:
        dim = cat_cols[0]
        val = pct_cols[0] if pct_cols else num_cols[0]
        rows = df.head(6).to_dict(orient="records")
        fmt = lambda v: f"{float(v):.1f}%" if pct_cols else f"{float(v):,.0f}"
        parts = [f"{r.get(dim, '?')}: {fmt(r.get(val, 0))}" for r in rows]
        tail = f" ({n} groups total)" if n > 6 else ""
        return f"{val} by {dim} — {', '.join(parts)}{tail}."

    # ── Comparison: multiple value columns (custom_split result) ────────────────
    if len(num_cols) >= 2:
        rows = df.head(3).to_dict(orient="records")
        parts = []
        for r in rows:
            items = []
            for k, v in r.items():
                try:
                    items.append(f"{k}: {float(v):,.0f}")
                except (TypeError, ValueError):
                    items.append(f"{k}: {v}")
            parts.append(" | ".join(items))
        return ". ".join(parts) + "."

    # ── Single percentage ───────────────────────────────────────────────────────
    if pct_cols:
        val = float(df[pct_cols[0]].iloc[0])
        return f"{pct_cols[0].replace('_', ' ')}: {val:.1f}%."

    # ── Single scalar ───────────────────────────────────────────────────────────
    if num_cols:
        v_col = num_cols[0]
        val = float(df[v_col].iloc[0])
        if n == 1:
            return f"{v_col.replace('_', ' ')}: {val:,.0f}."
        return f"{v_col.replace('_', ' ')}: {val:,.0f} (first of {n} rows, cols: {', '.join(cols[:4])})."

    return f"Query returned {n} rows with columns: {', '.join(cols[:6])}."


def score_summary_relevance(question: str, summary: str, expected_tokens: list[str] | None = None) -> float:
    q_t = _norm_tokens(question)
    s_t = _norm_tokens(summary)
    if not q_t:
        return 0.0
    overlap = len(q_t & s_t) / max(len(q_t), 1)
    score = overlap
    if expected_tokens:
        hits = sum(1 for t in expected_tokens if t.lower() in summary.lower())
        score = max(score, hits / max(len(expected_tokens), 1))
    if "rate" in question.lower() and "%" in summary:
        score = min(1.0, score + 0.2)
    return float(max(0.0, min(1.0, score)))


# ── LLM-as-judge ─────────────────────────────────────────────────────────────

_JUDGE_SYSTEM = """\
You are an analytics answer quality judge. You will be given:
1. A user's analytics question
2. The data returned: column names, row count, date range (if applicable), and the most recent sample rows
3. A brief summary of the result

Rate the answer on three dimensions from 0.0 to 1.0. Use this scale carefully:
- relevance: Does the data TYPE match the question? Score by this rubric:
    0.0 = completely wrong entity (e.g., asked for users, got transactions)
    0.4 = right domain but wrong metric (e.g., asked for DAU, got MAU)
    0.7 = right metric, minor issue (e.g., missing requested filter)
    1.0 = directly answers the question
  IMPORTANT: Do NOT score 0.0 just because sample rows show a different date range — look at the date_range field for actual coverage. Do NOT score 0.0 because the summary is terse.

- completeness: Are the required dimensions present? Score:
    0.0 = key columns entirely absent (e.g., asked \"by platform\" but no platform column)
    0.5 = partial columns present
    1.0 = all expected dimensions/metrics present

- presentation: Is the data well-structured?
    0.0 = 0 rows returned (empty result)
    0.5 = data exists but shape is suboptimal
    1.0 = correct granularity, sorted sensibly, non-trivial values

Rules:
- A breakdown question (\"by platform\", \"by city\") needs a categorical dimension column for completeness≥0.7.
- A trend question (\"MOM\", \"last N months\", \"over time\") needs a date/month column for completeness≥0.7.
- Never score relevance=0.0 if the data has the right column types for the question.

Respond ONLY with valid JSON (no markdown, no explanation):
{\"relevance\": 0.0, \"completeness\": 0.0, \"presentation\": 0.0}
"""


def score_answer_llm_judge(
    question: str,
    columns: list[str],
    sample_rows: list[dict],
    summary: str,
    openai_api_key: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
) -> dict[str, float]:
    """
    Judge answer quality on three dimensions (relevance, completeness, presentation).

    Args:
        judge_provider: Provider to use for the judge (e.g. "openai"). None → JUDGE_LLM_PROVIDER
                        env var, falling back to the active LLM_PROVIDER.
        judge_model:    Specific model for the judge (e.g. "gpt-4o-mini"). None → JUDGE_MODEL
                        env var, falling back to the judge provider's fast model.

    Returns {"relevance": float, "completeness": float, "presentation": float}.
    Falls back to {"relevance": 0.5, "completeness": 0.5, "presentation": 0.5} on error.
    """
    from core.infra.llm import make_llm_client, resolve_model
    _default = {"relevance": 0.5, "completeness": 0.5, "presentation": 0.5}
    try:
        # Resolve judge provider and model
        eff_provider = judge_provider or os.environ.get("JUDGE_LLM_PROVIDER") or None
        eff_model = (
            judge_model
            or os.environ.get("JUDGE_MODEL")
            or resolve_model("fast", provider=eff_provider)
        )

        # Show the most recent rows (tail) so trend questions show the right time period.
        # Also surface date range so the judge doesn't penalise a Jan sample for a Jun question.
        recent_rows = sample_rows[-5:] if sample_rows else []
        date_range_str = ""
        if sample_rows:
            date_keys = [k for k in (sample_rows[0] or {}) if "date" in k.lower() or "month" in k.lower() or "time" in k.lower()]
            if date_keys:
                dates = [str(r.get(date_keys[0], "")) for r in sample_rows if r.get(date_keys[0])]
                if dates:
                    date_range_str = f"\nDate range in data: {dates[0]} to {dates[-1]}"
        data_block = (
            f"Columns: {columns}"
            f"\nTotal rows returned: {len(sample_rows)}"
            f"{date_range_str}"
            f"\nMost recent sample rows: {recent_rows}"
        )
        user_msg = (
            f"Question: \"{question}\"\n\n"
            f"Data returned:\n{data_block}\n\n"
            f"Summary: \"{summary}\"\n\n"
            "Your JSON rating:"
        )
        client = make_llm_client(provider=eff_provider)
        resp = client.chat.completions.create(
            model=eff_model,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=60,
        )
        raw = resp.choices[0].message.content.strip()
        parsed = json.loads(raw)
        return {
            "relevance":    float(max(0.0, min(1.0, parsed.get("relevance",    0.5)))),
            "completeness": float(max(0.0, min(1.0, parsed.get("completeness", 0.5)))),
            "presentation": float(max(0.0, min(1.0, parsed.get("presentation", 0.5)))),
        }
    except Exception:
        return _default


# ── Column-semantic data presentation score ───────────────────────────────────

def score_data_presentation(
    question: str,
    columns: list[str],
    sample_rows: list[dict],
    analysis_type: str,
) -> float:
    """
    Rule-based check: does the returned data have the right shape for the question type?
    0.0 = clearly wrong structure, 1.0 = ideal.
    """
    if not columns or not sample_rows:
        return 0.0

    col_set = {c.lower() for c in columns}
    q_lower = question.lower()
    score = 0.5  # baseline: data exists

    # Trend questions need a time column
    trend_keywords = {"mom", "over time", "trend", "monthly", "weekly", "daily", "last n months", "last 3", "last 6"}
    needs_time = any(kw in q_lower for kw in trend_keywords) or analysis_type in ("metric", "trend")
    time_cols = {"month", "week", "date", "day", "period", "time_period", "month_name"}
    if needs_time:
        score = 0.8 if col_set & time_cols else 0.2

    # Breakdown questions need a categorical dimension
    if any(kw in q_lower for kw in ("by platform", "by city", "by channel", "by state", "by region", "by cohort", "by age", "by occupation")):
        dim_hint = q_lower.split(" by ")[-1].split()[0] if " by " in q_lower else ""
        has_dim = dim_hint in col_set or any(dim_hint in c for c in col_set)
        score = 0.9 if has_dim else 0.3

    # Comparison questions need a cohort/segment dimension
    if any(kw in q_lower for kw in ("vs", "versus", "compare", "difference between", "split")):
        cohort_cols = {"cohort", "segment", "group", "type", "category"}
        score = 0.8 if col_set & cohort_cols else 0.4

    # Data should be non-trivial (at least 1 row with non-null values)
    if len(sample_rows) == 0:
        score = min(score, 0.1)

    return float(max(0.0, min(1.0, score)))


def score_query_correctness(
    sql_ok: bool,
    execution_ok: bool,
    columns: list[str],
    case: EvalCase,
) -> float:
    if not sql_ok or not execution_ok:
        return 0.0
    # Compiles + executes earns 0.7. The remaining 0.3 requires column evidence.
    # Never give the bonus for free — previously the else branch handed +0.3 to any running query,
    # making "it ran" equivalent to "it answered correctly" (pushed baseline to 1.0 here, ~0.81 AR).
    score = 0.7
    if case.expected_columns_any:
        if any(c in columns for c in case.expected_columns_any):
            score += 0.3
    return float(max(0.0, min(1.0, score)))


def score_routing(decision_route: str, analysis_type: str, case: EvalCase) -> float:
    score = 0.0
    if case.expected_route:
        score += 1.0 if decision_route == case.expected_route else 0.0
    else:
        score += 0.6
    if case.expected_analysis_type:
        score = (score + (1.0 if analysis_type == case.expected_analysis_type else 0.0)) / 2.0
    return float(max(0.0, min(1.0, score)))


def build_eval_suite(catalog: dict) -> list[EvalCase]:  # noqa: ARG001
    """30 hand-curated cases — no near-duplicates, one case per named invariant or category gap.

    Coverage:
      Activation    (4): scalar, channel-filtered numerator, MOM trend, by user dimension
      Retention     (4): D7 scalar, D30 scalar, D7 by user dimension, D7 MOM trend
      User lifecycle(2): basic, with platform breakdown
      Behavioral cohort(2): AND overlap with filter scoping, AND NOT (anti_cohort)
      Other types   (5): funnel, diagnose, time_between, stickiness, journey
      Segment/compare(3): list filter, basic breakdown, custom split
      Basic+calendar(2): DAU scalar, activation for specific month
      Adversarial   (8): typo, bad_window, ambiguous, out_of_catalog, negation_filter,
                         multi_intent, relative_time, underspecified
    """
    return [
        # ── GROUP 1: Activation (5 cases) ─────────────────────────────────────
        EvalCase(
            question="what is activation rate",
            expected_analysis_type="metric",
            tags=["activation", "sql_structure", "pct_users_cte"],
            gold_sql={
                # Compiler uses denom_cohort + numer_events (multi-window path).
                "required": ["WITH denom_cohort", "numer_events", "LEFT JOIN numer_events"],
            },
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "row_count_max": 1,
                "col_present": ["onboarding_completed_users", "transaction_reconciled_users_30d", "activation_rate_30d"],
            },
        ),
        EvalCase(
            question="what is UPI activation rate",
            expected_analysis_type="metric",
            tags=["activation", "sql_structure", "filter_in_numerator"],
            gold_qo={"metric_id": "activation_rate", "filters_contain": {"transaction_channel": "UPI"}},
            gold_sql={
                "required": ["WITH denom_cohort", "transaction_channel = 'UPI'"],
                "forbidden_in_cte": {"denom_cohort": ["transaction_channel"]},
            },
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "row_count_max": 1,
            },
        ),
        EvalCase(
            question="show activation rate MOM for last 6 months",
            expected_analysis_type="metric",
            tags=["activation", "sql_structure", "monthly_trend", "pct_users_cte"],
            gold_sql={
                "required": ["WITH denom_cohort", "numer_events", "cohort_month", "GROUP BY 1"],
            },
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "col_present": ["month", "activation_rate_30d"],
            },
        ),
        EvalCase(
            question="show activation rate by platform for last 90 days",
            expected_analysis_type="segment",
            tags=["activation", "segment", "breakdown"],
            gold_qo={"breakdown": "platform"},
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
            },
        ),
        EvalCase(
            question="show 48 hour UPI activation",
            expected_analysis_type="metric",
            tags=["activation", "sql_structure", "filter_in_numerator", "hours_window"],
            # 48h = 2-day window; only one activation metric exists so it must resolve to activation_rate.
            # The UPI filter must still land in the numerator only — same scoping invariant as the 30d case.
            gold_qo={"metric_id": "activation_rate", "filters_contain": {"transaction_channel": "UPI"}},
            gold_sql={
                "required": ["WITH denom_cohort", "transaction_channel = 'UPI'"],
                "forbidden_in_cte": {"denom_cohort": ["transaction_channel"]},
            },
        ),
        # ── GROUP 2: Retention (8 cases) ─────────────────────────────────────
        EvalCase(
            question="show D7 retention",
            expected_analysis_type="retention",
            tags=["retention", "sql_structure", "event_hydration"],
            gold_qo={"metric_id": "d7_retention"},
        ),
        EvalCase(
            question="show D30 retention",
            expected_analysis_type="retention",
            tags=["retention", "metric_variant"],
            gold_qo={"metric_id": "d30_retention"},
        ),
        EvalCase(
            question="share week 4 retention",
            expected_analysis_type="retention",
            tags=["retention", "metric_resolution", "natural_language", "no_catalog_match"],
            # Week 4 = days 22–28 (weekly window). Catalog has d1/d7/d30 only — no W4 metric.
            # Assert intent only; metric_id is left to orchestrator's best-effort resolution.
        ),
        EvalCase(
            question="show D7 retention by platform",
            expected_analysis_type="retention",
            tags=["retention", "breakdown"],
            gold_qo={"metric_id": "d7_retention", "breakdown": "platform"},
        ),
        EvalCase(
            question="share MOM retention",
            expected_analysis_type="retention",
            tags=["retention", "monthly_trend", "natural_language"],
            # Generic phrasing — should pick default retention metric (d7) with monthly granularity.
            gold_qo={"metric_id": "d7_retention"},
        ),
        EvalCase(
            question="show D7 retention MOM for last 6 months",
            expected_analysis_type="retention",
            tags=["retention", "monthly_trend"],
            gold_qo={"metric_id": "d7_retention"},
        ),
        EvalCase(
            question="retention trend of active users",
            expected_analysis_type="retention",
            tags=["retention", "monthly_trend", "cohort_filter", "natural_language"],
            # Tests: "active users" cohort + "trend" phrasing → retention, not metric/segment.
            # active_user is a catalog custom event; orchestrator may use event or metric path.
            gold_qo={"metric_id": "d7_retention"},
        ),
        EvalCase(
            question="share retention trend of transacting users",
            expected_analysis_type="retention",
            tags=["retention", "monthly_trend", "cohort_filter", "natural_language"],
            # Distinct from above: "transacting users" maps to transacting_user custom event.
            # Tests that the cohort scoping doesn't accidentally flip to a segment query.
            gold_qo={"metric_id": "d7_retention"},
        ),
        # ── GROUP 3: User lifecycle (2 cases) ─────────────────────────────────
        EvalCase(
            question="show me user lifecycle stages",
            expected_analysis_type="user_lifecycle",
            tags=["user_lifecycle", "sql_structure", "generic_event_guard"],
            gold_sql={
                "forbidden": ["event_name = 'app_opened'", "event_name = 'app_open'",
                              "event_name = 'session_start'"],
            },
        ),
        EvalCase(
            question="show user lifecycle stages by platform",
            expected_analysis_type="user_lifecycle",
            tags=["user_lifecycle", "breakdown"],
            gold_qo={"breakdown": "platform"},
        ),
        # ── GROUP 4: Behavioral cohort (2 cases) ─────────────────────────────
        # These two test distinct compiler invariants: filter scoping (AND) vs anti_cohort (AND NOT).
        EvalCase(
            question="How many users did onboarding and UPI transactions in Feb",
            expected_analysis_type="behavioral_cohort",
            tags=["behavioral_cohort", "sql_structure", "filter_scoping"],
            gold_qo={"event": "onboarding_completed", "event_b": "transaction_reconciled"},
            gold_sql={
                "forbidden_in_cte": {"did_a": ["transaction_channel"]},
                "required_in_cte":  {"did_b": ["transaction_channel"]},
            },
        ),
        EvalCase(
            question="How many users did onboarding and no UPI transactions in Feb",
            expected_analysis_type="behavioral_cohort",
            tags=["behavioral_cohort", "sql_structure", "anti_cohort"],
            gold_qo={
                "event": "onboarding_completed",
                "event_b": "transaction_reconciled",
                "metric_variant": "anti_cohort",
            },
            gold_sql={
                "required":         ["WHERE db.user_id IS NULL"],
                "forbidden":        ["also_transaction_reconciled"],
                "forbidden_in_cte": {"did_a": ["transaction_channel"]},
                "required_in_cte":  {"did_b": ["transaction_channel"]},
            },
        ),
        # ── GROUP 5: Specialist analysis types (5 cases) ──────────────────────
        EvalCase(
            question="show onboarding funnel for Jan",
            expected_analysis_type="funnel",
            tags=["funnel", "advanced"],
            gold_qo={"event": "onboarding_completed"},
        ),
        EvalCase(
            question="why did transacting users drop last month",
            expected_analysis_type="diagnose",
            tags=["diagnose", "advanced"],
        ),
        EvalCase(
            question="time between onboarding completed and transaction reconciled",
            expected_analysis_type="time_between",
            tags=["time_between", "advanced"],
            gold_qo={"event": "onboarding_completed", "event_b": "transaction_reconciled"},
        ),
        EvalCase(
            question="how sticky is our product",
            expected_analysis_type="stickiness",
            tags=["stickiness", "sql_structure", "generic_event_guard"],
            gold_sql={
                "forbidden": ["event_name = 'app_opened'", "event_name = 'app_open'",
                              "event_name = 'session_start'"],
            },
        ),
        EvalCase(
            question="show user journey",
            expected_analysis_type="journey",
            tags=["journey", "sql_structure", "validation_enforcement"],
            # Journey routes to __analyst__ — gold_sql assertions are unreachable.
        ),
        # ── GROUP 6: Segment and comparison (3 cases) ─────────────────────────
        EvalCase(
            question="show transacting users by platform for iOS and Android last month",
            expected_analysis_type="segment",
            tags=["segment", "sql_structure", "filter_rendering", "list_filter"],
            gold_sql={"forbidden": ["= '['", "= \"['"]},
        ),
        EvalCase(
            question="show transacting users by platform for Jan",
            expected_analysis_type="segment",
            expected_route="custom_segment",
            tags=["segment", "breakdown"],
            gold_qo={"analysis_type": "segment", "breakdown": "platform"},
        ),
        EvalCase(
            question="Share Jupiter active users vs Transacting users split for Jan",
            expected_route="custom_split",
            expected_columns_any=["cohort", "users"],
            tags=["custom_split", "comparison"],
        ),
        # ── GROUP 7: Basic metric and calendar filter (2 cases) ────────────────
        EvalCase(
            question="show DAU for last month",
            expected_analysis_type="metric",
            expected_route="orchestrator",
            tags=["metric_trend", "basic"],
        ),
        EvalCase(
            question="show activation rate for Jan",
            expected_analysis_type="metric",
            tags=["activation", "sql_structure", "filter_rendering"],
            gold_qo={"metric_id": "activation_rate"},
            gold_sql={"forbidden": ["AND date =", "date = '['"]},
            gold_result={
                "col_bounds": {"activation_rate_30d": [0, 100]},
                "row_count_min": 1,
                "row_count_max": 1,
                "col_present": ["activation_rate_30d"],
            },
        ),
        # ── GROUP 8: Adversarial — 8 distinct failure patterns ────────────────
        EvalCase(
            "shw dau lst week",
            expected_analysis_type="metric",
            tags=["adversarial", "typo"],
        ),
        EvalCase(
            "show MAU for last 3 days",
            tags=["adversarial", "bad_window"],
        ),
        EvalCase(
            "show me users",
            tags=["adversarial", "ambiguous"],
        ),
        EvalCase(
            "how many users opened the settings page last month",
            tags=["adversarial", "out_of_catalog"],
        ),
        EvalCase(
            "onboarding completion rate for non-referral users in Jan",
            expected_analysis_type="metric",
            tags=["adversarial", "negation_filter"],
        ),
        EvalCase(
            "compare activation rate and churn rate by platform for Q1",
            tags=["adversarial", "multi_intent"],
        ),
        EvalCase(
            "last quarter DAU trend",
            expected_analysis_type="metric",
            tags=["adversarial", "relative_time"],
        ),
        EvalCase(
            "what happened last month",
            tags=["adversarial", "underspecified"],
        ),
    ]


# Keep old name as alias so external tooling (trace_to_regression.py etc.) doesn't break.
def build_100_question_suite(catalog: dict) -> list[EvalCase]:
    return build_eval_suite(catalog)


# ── Cost estimation ───────────────────────────────────────────────────────────
# gpt-4o pricing (per token, approximate 2024 rates)
_COST_INPUT_PER_TOKEN  = 5.0  / 1_000_000   # $5 / 1M input tokens
_COST_OUTPUT_PER_TOKEN = 15.0 / 1_000_000   # $15 / 1M output tokens
# Approximate tokens per orchestrate call (system prompt + question + response)
_EST_PROMPT_TOKENS     = 2_200
_EST_COMPLETION_TOKENS = 250
_EST_COST_PER_CASE     = (
    _EST_PROMPT_TOKENS * _COST_INPUT_PER_TOKEN
    + _EST_COMPLETION_TOKENS * _COST_OUTPUT_PER_TOKEN
)


# Specialist types that are acceptable when the eval expects the parent "metric" type.
# e.g. orchestrator returns "retention" for a retention metric — that IS a metric query.
_INTENT_EQUIV: dict[str, set[str]] = {
    "metric":       {"metric", "retention", "funnel", "time_between", "behavioral_cohort"},
    "retention":    {"retention", "metric"},
    "funnel":       {"funnel", "metric"},
    "time_between": {"time_between", "metric"},
    "segment":      {"segment", "custom_segment"},
}


def score_intent_accuracy(qo_analysis_type: str, case: EvalCase) -> float:
    """1.0 when analysis_type matches expected (or an accepted equivalent), 0.5 if no expectation."""
    if not case.expected_analysis_type:
        return 0.5
    allowed = _INTENT_EQUIV.get(case.expected_analysis_type, {case.expected_analysis_type})
    return 1.0 if qo_analysis_type in allowed else 0.0


def score_gold_sql(sql: str, case: EvalCase) -> tuple[float, dict[str, Any]]:
    """
    Deterministic structural assertions on the generated SQL string.
    Catches compiler bugs that produce zero rows without a SQL syntax error.

    Returns (score in [0,1], detail dict). 1.0 when no gold_sql defined.
    """
    gold = case.gold_sql
    if not gold or not sql:
        return 1.0, {"skipped": True}

    checks: dict[str, Any] = {}
    n_ok = 0
    n_tot = 0

    def _cte_body(cte_name: str, full_sql: str) -> str:
        """Extract the text inside a named CTE block."""
        pattern = rf"{re.escape(cte_name)}\s+AS\s*\("
        m = re.search(pattern, full_sql, re.IGNORECASE)
        if not m:
            return ""
        start = m.end()
        depth = 1
        i = start
        while i < len(full_sql) and depth > 0:
            if full_sql[i] == "(":
                depth += 1
            elif full_sql[i] == ")":
                depth -= 1
            i += 1
        return full_sql[start: i - 1]

    # required / forbidden on the full SQL
    for pat in (gold.get("required") or []):
        n_tot += 1
        ok = pat in sql
        checks[f"required:{pat[:40]}"] = {"match": ok}
        if ok:
            n_ok += 1

    for pat in (gold.get("forbidden") or []):
        n_tot += 1
        ok = pat not in sql
        checks[f"forbidden:{pat[:40]}"] = {"match": ok, "found": not ok}
        if ok:
            n_ok += 1

    # required_in_cte / forbidden_in_cte
    for cte, patterns in (gold.get("required_in_cte") or {}).items():
        body = _cte_body(cte, sql)
        for pat in patterns:
            n_tot += 1
            ok = pat in body
            checks[f"cte:{cte}:required:{pat[:40]}"] = {"match": ok, "cte_found": bool(body)}
            if ok:
                n_ok += 1

    for cte, patterns in (gold.get("forbidden_in_cte") or {}).items():
        body = _cte_body(cte, sql)
        for pat in patterns:
            n_tot += 1
            ok = pat not in body
            checks[f"cte:{cte}:forbidden:{pat[:40]}"] = {"match": ok, "found_in_cte": not ok}
            if ok:
                n_ok += 1

    if n_tot == 0:
        return 1.0, checks
    return n_ok / n_tot, checks


def score_gold_qo(qo: QueryObject, case: EvalCase) -> tuple[float, dict[str, Any]]:
    """
    Structured rubric over final QO slots (after production fixups).
    Returns (score in [0,1], detail dict). 0.5 when no gold_qo defined.
    """
    gold = case.gold_qo
    if not gold:
        return 0.5, {"skipped": True}
    checks: dict[str, Any] = {}
    n_ok = 0
    n_tot = 0
    for key in ("analysis_type", "event", "metric_id", "event_b", "breakdown", "metric_variant"):
        if key not in gold:
            continue
        n_tot += 1
        exp = gold[key]
        got = getattr(qo, key, None)
        ok = exp == got
        checks[key] = {"expected": exp, "got": got, "match": ok}
        if ok:
            n_ok += 1
    fc = gold.get("filters_contain")
    if isinstance(fc, dict) and fc:
        n_tot += 1
        filt = dict(getattr(qo, "filters", None) or {})
        sub_ok = all(str(filt.get(k)) == str(v) for k, v in fc.items())
        checks["filters_contain"] = {"expected": fc, "got": filt, "match": sub_ok}
        if sub_ok:
            n_ok += 1
    if n_tot == 0:
        return 0.5, checks
    return n_ok / n_tot, checks


def score_gold_result(df: pd.DataFrame, case: "EvalCase") -> tuple[float, dict[str, Any]]:
    """
    Assert result-value properties on the executed DataFrame.
    Catches semantic errors that SQL execution alone cannot detect:
      - activation_rate > 100%
      - wrong column names
      - NULL values in numeric columns
      - empty results when data is expected

    Returns (score in [0,1], detail dict). 1.0 when no gold_result defined.
    """
    gold = getattr(case, "gold_result", None)
    if not gold:
        return 1.0, {"skipped": True}
    if df is None or df.empty:
        empty_checks = {"empty_result": {"match": False, "note": "DataFrame is empty"}}
        return 0.0, empty_checks

    checks: dict[str, Any] = {}
    n_ok = 0
    n_tot = 0

    # Row count bounds
    rmin = gold.get("row_count_min")
    rmax = gold.get("row_count_max")
    if rmin is not None:
        n_tot += 1
        ok = len(df) >= rmin
        checks["row_count_min"] = {"expected": f">={rmin}", "got": len(df), "match": ok}
        if ok:
            n_ok += 1
    if rmax is not None:
        n_tot += 1
        ok = len(df) <= rmax
        checks["row_count_max"] = {"expected": f"<={rmax}", "got": len(df), "match": ok}
        if ok:
            n_ok += 1

    # Column presence
    for col in (gold.get("col_present") or []):
        n_tot += 1
        ok = col in df.columns
        checks[f"col_present:{col}"] = {"match": ok}
        if ok:
            n_ok += 1

    # Numeric column bounds — the primary semantic correctness check
    for col, bounds in (gold.get("col_bounds") or {}).items():
        if col not in df.columns:
            n_tot += 1
            checks[f"col_bounds:{col}"] = {"match": False, "note": "column missing"}
            continue
        lo, hi = float(bounds[0]), float(bounds[1])
        vals = pd.to_numeric(df[col], errors="coerce").dropna()
        if vals.empty:
            n_tot += 1
            checks[f"col_bounds:{col}"] = {"match": False, "note": "all null"}
            continue
        min_val, max_val = float(vals.min()), float(vals.max())
        ok = min_val >= lo and max_val <= hi
        n_tot += 1
        checks[f"col_bounds:{col}"] = {
            "match": ok,
            "expected": f"[{lo}, {hi}]",
            "got_range": f"[{min_val}, {max_val}]",
        }
        if ok:
            n_ok += 1

    # Non-null columns
    for col in (gold.get("col_not_null") or []):
        if col not in df.columns:
            n_tot += 1
            checks[f"col_not_null:{col}"] = {"match": False, "note": "column missing"}
            continue
        n_null = int(pd.to_numeric(df[col], errors="coerce").isna().sum())
        n_tot += 1
        ok = n_null == 0
        checks[f"col_not_null:{col}"] = {"match": ok, "null_count": n_null}
        if ok:
            n_ok += 1

    if n_tot == 0:
        return 1.0, checks
    return n_ok / n_tot, checks


def _aggregate_by_tag(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up pass rate and scores by case tags (and ``_untagged``)."""
    buckets: dict[str, dict[str, float | int]] = {}
    for t in traces:
        tags = (t.get("expected") or {}).get("tags") or []
        if not tags:
            tags = ["_untagged"]
        scores = t.get("scores") or {}
        passed = bool(t.get("pass", False))
        for tag in tags:
            b = buckets.setdefault(
                str(tag),
                {"n": 0, "pass_count": 0, "sum_ar": 0.0, "sum_intent": 0.0, "sum_gold": 0.0},
            )
            b["n"] = int(b["n"]) + 1
            if passed:
                b["pass_count"] = int(b["pass_count"]) + 1
            b["sum_ar"] = float(b["sum_ar"]) + float(scores.get("answer_relevance", 0.0))
            b["sum_intent"] = float(b["sum_intent"]) + float(scores.get("intent_accuracy", 0.5))
            b["sum_gold"] = float(b["sum_gold"]) + float(scores.get("gold_qo_score", 0.5))
    out: dict[str, Any] = {}
    for tag, b in sorted(buckets.items()):
        n = max(int(b["n"]), 1)
        out[tag] = {
            "n": int(b["n"]),
            "pass_rate": round(int(b["pass_count"]) / n, 3),
            "avg_answer_relevance": round(float(b["sum_ar"]) / n, 3),
            "avg_intent_accuracy": round(float(b["sum_intent"]) / n, 3),
            "avg_gold_qo_score": round(float(b["sum_gold"]) / n, 3),
        }
    return out


def _load_last_benchmark(out_dir: Path) -> dict | None:
    """Return the aggregate block from the most recent prior run, or None."""
    runs = sorted(out_dir.glob("eval_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in runs:
        try:
            data = json.loads(path.read_text())
            if "aggregate" in data:
                return {"aggregate": data["aggregate"], "file": path.name}
        except Exception:
            continue
    return None


def _load_hard_questions(out_dir: Path, n: int = 50) -> list[str] | None:
    """
    Return the N question strings with the lowest answer_relevance from the
    most recent completed eval run, or None if no prior run exists.

    Used by --hard mode to focus iterations on the weakest cases.
    """
    runs = sorted(out_dir.glob("eval_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in runs:
        try:
            data = json.loads(path.read_text())
            cases = data.get("cases", [])
            if not cases:
                continue
            scored = [
                (c["question"], float(c.get("scores", {}).get("answer_relevance", 1.0)))
                for c in cases
                if c.get("question")
            ]
            # Deduplicate by question text (keep the lowest AR seen for that question).
            best: dict[str, float] = {}
            for q, ar in scored:
                if q not in best or ar < best[q]:
                    best[q] = ar
            hardest = sorted(best, key=lambda q: best[q])[:n]
            return hardest
        except Exception:
            continue
    return None


def build_multi_turn_suite() -> list[MultiTurnEvalCase]:
    """
    Multi-turn test sequences that test context-preservation across a conversation.
    Each sequence simulates a realistic follow-up pattern observed in production.
    These catch filter-inheritance bugs that single-turn evals completely miss.
    """
    return [
        # ── Activation rate filter inheritance ────────────────────────────────
        MultiTurnEvalCase(
            description="UPI filter must survive a granularity change follow-up",
            tags=["multi_turn", "filter_inheritance", "activation"],
            turns=[
                {
                    "question": "what is UPI activation rate",
                    "expected_analysis_type": "metric",
                    "gold_qo": {"metric_id": "activation_rate",
                                "filters_contain": {"transaction_channel": "UPI"}},
                    "gold_sql": {"required": ["transaction_channel = 'UPI'",
                                              "WITH denom_cohort"]},
                    "gold_result": {"col_bounds": {"activation_rate_30d": [0, 100]}},
                },
                {
                    "question": "share monthly trend",
                    "expected_analysis_type": "metric",
                    "gold_qo": {"metric_id": "activation_rate",
                                "filters_contain": {"transaction_channel": "UPI"}},
                    "gold_sql": {"required": ["transaction_channel = 'UPI'",
                                              "WITH denom_cohort", "cohort_month"]},
                    "gold_result": {"col_bounds": {"activation_rate_30d": [0, 100]},
                                    "col_present": ["month", "activation_rate_30d"]},
                },
            ],
        ),
        # ── Time window change preserves metric ───────────────────────────────
        MultiTurnEvalCase(
            description="Metric identity survives a time window change follow-up",
            tags=["multi_turn", "metric_inheritance", "activation"],
            turns=[
                {
                    "question": "show activation rate for Jan",
                    "expected_analysis_type": "metric",
                    "gold_qo": {"metric_id": "activation_rate"},
                },
                {
                    "question": "now show last 90 days",
                    "expected_analysis_type": "metric",
                    "gold_qo": {"metric_id": "activation_rate"},
                },
            ],
        ),
        # ── Retention follow-up ───────────────────────────────────────────────
        MultiTurnEvalCase(
            description="Retention metric identity and event must survive follow-up",
            tags=["multi_turn", "metric_inheritance", "retention"],
            turns=[
                {
                    "question": "show D7 retention",
                    "expected_analysis_type": "retention",
                    "gold_qo": {"metric_id": "d7_retention"},
                },
                {
                    "question": "show last 6 months trend",
                    "expected_analysis_type": "retention",
                    "gold_qo": {"metric_id": "d7_retention"},
                },
            ],
        ),
        # ── Breakdown not silently inherited ─────────────────────────────────
        MultiTurnEvalCase(
            description="Segment breakdown must not silently carry to metric follow-up",
            tags=["multi_turn", "breakdown_isolation"],
            turns=[
                {
                    "question": "show activation rate by platform",
                    "expected_analysis_type": "segment",
                    "gold_qo": {"breakdown": "platform"},
                },
                {
                    "question": "show overall activation rate",
                    "expected_analysis_type": "metric",
                    "gold_qo": {"breakdown": None},
                },
            ],
        ),
    ]


def run_multi_turn_eval_case(
    conn: duckdb.DuckDBPyConnection,
    case: MultiTurnEvalCase,
    catalog: dict,
    sampled: dict,
    metrics: list[dict],
    openai_api_key: str | None,
    *,
    eval_provider: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
) -> dict[str, Any]:
    """
    Run a multi-turn eval case. History accumulates across turns, exactly as
    production does. Returns a trace with per-turn results and an overall pass/fail.
    """
    from qa.eval_production import apply_post_orchestrator_fixups, qo_snapshot

    history: list[dict] = []
    turn_traces: list[dict] = []
    all_pass = True

    for turn_idx, turn in enumerate(case.turns):
        q = turn["question"]
        # Wrap turn spec as EvalCase for scoring helpers
        turn_case = EvalCase(
            question=q,
            expected_analysis_type=turn.get("expected_analysis_type"),
            gold_qo=turn.get("gold_qo"),
            gold_sql=turn.get("gold_sql"),
            gold_result=turn.get("gold_result"),
            tags=case.tags,
        )

        t = run_eval_case(
            conn, turn_case, catalog, sampled, metrics, history,
            openai_api_key,
            eval_provider=eval_provider,
            judge_provider=judge_provider,
            judge_model=judge_model,
        )
        turn_traces.append({"turn": turn_idx + 1, "question": q, **t})

        if not t.get("pass"):
            all_pass = False

        # Accumulate history from this turn's QO
        qo_data = t.get("stages", {}).get("qo_after_fixups") or t.get("stages", {}).get("orchestrator_raw") or {}
        if qo_data:
            history.append({"qo": qo_data})

    return {
        "description": case.description,
        "tags": case.tags,
        "turns": turn_traces,
        "pass": all_pass,
        "turn_count": len(case.turns),
    }


def _gold_qo_wrong_on_snapshot(snap: dict, gold: dict) -> bool:
    """True if any slot in gold_qo doesn't match the QO snapshot."""
    for key in ("analysis_type", "event", "metric_id", "event_b", "breakdown", "metric_variant"):
        if key not in gold:
            continue
        if gold[key] != snap.get(key):
            return True
    fc = gold.get("filters_contain")
    if isinstance(fc, dict) and fc:
        filt = snap.get("filters") or {}
        if not all(str(filt.get(k)) == str(v) for k, v in fc.items()):
            return True
    return False


def _determine_failure_stage(trace: dict, case: "EvalCase") -> str | None:
    """
    Identify which pipeline stage caused a failure. Returns None when the case passed.

    Attribution order (first match wins):
      fatal          → unhandled exception in the harness
      orchestrator   → wrong analysis_type or LLM returned clarify/out_of_scope unexpectedly
      fixup          → orchestrator was correct but fixup chain corrupted a slot
      compiler       → SQL not compiled, or gold_sql structural assertion failed
      execution      → SQL executed but threw an error
      execution_semantic → result values failed gold_result bounds/cols
      answer_quality → LLM judge found the answer irrelevant
    """
    if not trace.get("pass") is False and trace.get("pass"):
        return None  # passed — no failure to attribute

    if trace.get("fatal_error"):
        return "fatal"

    scores = trace.get("scores", {})
    stages = trace.get("stages", {})

    # Clarify/out_of_scope when we expected a real analysis type
    raw_at = (stages.get("orchestrator_raw") or {}).get("analysis_type", "")
    if raw_at in ("clarify", "out_of_scope"):
        if case.expected_analysis_type and case.expected_analysis_type not in ("clarify", "out_of_scope"):
            return "orchestrator"

    # Wrong analysis_type
    if case.expected_analysis_type and scores.get("intent_accuracy", 1.0) < 1.0:
        return "orchestrator"

    # Wrong QO slots — distinguish orchestrator vs fixup regression
    if case.gold_qo and scores.get("gold_qo_score", 1.0) < 1.0:
        raw = stages.get("orchestrator_raw") or {}
        post = stages.get("qo_after_fixups") or {}
        gold = case.gold_qo or {}
        raw_wrong = _gold_qo_wrong_on_snapshot(raw, gold)
        post_wrong = _gold_qo_wrong_on_snapshot(post, gold)
        if raw_wrong and post_wrong:
            return "orchestrator"
        if not raw_wrong and post_wrong:
            return "fixup"  # orchestrator was correct, fixup chain broke a slot
        return "orchestrator"  # raw_wrong and not post_wrong — fixup recovered it (weird partial)

    # No SQL compiled
    compile_sql = (stages.get("compile") or {}).get("sql")
    if not compile_sql:
        return "compiler"

    # SQL structural assertion failed
    if case.gold_sql and scores.get("gold_sql_score", 1.0) < 1.0:
        return "compiler"

    # Execution error
    if not (stages.get("execution") or {}).get("ok", True):
        return "execution"

    # Result semantic assertion failed
    if getattr(case, "gold_result", None) and scores.get("gold_result_score", 1.0) < 1.0:
        return "execution_semantic"

    # LLM judge thinks answer is wrong
    if scores.get("llm_judge_relevance", 1.0) < 0.5:
        return "answer_quality"

    return "unknown"


def run_eval_case(
    conn: duckdb.DuckDBPyConnection,
    case: EvalCase,
    catalog: dict,
    sampled: dict,
    metrics: list[dict],
    history: list[dict],
    openai_api_key: str | None,
    *,
    eval_provider: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
) -> dict[str, Any]:
    t_case_start = time.perf_counter()

    trace: dict[str, Any] = {
        "question": case.question,
        "expected": asdict(case),
        "stages": {},
        "scores": {},
    }

    try:
        qo = _orchestrate_with_retry(
            question=case.question,
            catalog=catalog,
            sampled_values=sampled,
            openai_api_key=openai_api_key,
            history=history,
            eval_provider=eval_provider,
        )
        trace["stages"]["orchestrator_raw"] = qo_snapshot(qo)

        if qo.analysis_type in ("clarify", "out_of_scope"):
            trace["stages"]["qo_after_fixups"] = None
            trace["stages"]["orchestrator"] = trace["stages"]["orchestrator_raw"]
            summary = (qo.clarify_message or "Could you rephrase that?").strip()
            trace["summary"] = summary
            ia = score_intent_accuracy(qo.analysis_type, case)
            gold_s, gold_d = score_gold_qo(qo, case)
            trace["scores"] = {
                "query_correctness": 0.0,
                "query_routing": 0.0,
                "summary_relevance": score_summary_relevance(case.question, summary, case.expected_summary_tokens),
                "answer_relevance": 0.0,
                "intent_accuracy": ia,
                "gold_qo_score": round(gold_s, 3),
                "gold_qo_detail": gold_d,
            }
            trace["pass"] = False
            trace["failure_stage"] = _determine_failure_stage(trace, case)
            trace["fixup_deltas"] = []
            trace["latency_ms"] = round((time.perf_counter() - t_case_start) * 1000)
            return trace

        apply_post_orchestrator_fixups(
            qo,
            question=case.question,
            catalog=catalog,
            sampled=sampled,
            metrics=metrics,
            history=history,
        )
        trace["stages"]["qo_after_fixups"] = qo_snapshot(qo)
        trace["stages"]["orchestrator"] = trace["stages"]["qo_after_fixups"]
        gold_s, gold_d = score_gold_qo(qo, case)

        decision = resolve_query_policy(case.question, qo, catalog, sampled)
        trace["stages"]["resolver"] = {
            "route": decision.route,
            "confidence": decision.confidence,
            "reasons": decision.reason_codes,
            "candidates": decision.candidates,
            "comparison_intent": getattr(decision, "comparison_intent", False),
            "comparison_entities": getattr(decision, "comparison_entities", []),
            "comparison_guard_status": getattr(decision, "comparison_guard_status", "not_applicable"),
        }

        allow, allow_reason = gate_allow_execute(decision, qo)
        trace["stages"]["gate"] = {"allow_execute": allow, "reason": allow_reason}
        if not allow:
            summary = "Blocked for clarification."
            trace["summary"] = summary
            ia = score_intent_accuracy(qo.analysis_type, case)
            trace["scores"] = {
                "query_correctness": 0.0,
                "query_routing": score_routing(decision.route, qo.analysis_type, case),
                "summary_relevance": score_summary_relevance(case.question, summary, case.expected_summary_tokens),
                "answer_relevance": 0.0,
                "intent_accuracy": ia,
                "gold_qo_score": round(gold_s, 3),
                "gold_qo_detail": gold_d,
            }
            trace["pass"] = False
            trace["failure_stage"] = _determine_failure_stage(trace, case)
            trace["fixup_deltas"] = getattr(qo, "_fixup_deltas", [])
            trace["latency_ms"] = round((time.perf_counter() - t_case_start) * 1000)
            return trace

        sql, metric_name = choose_sql(decision, qo, metrics)
        trace["stages"]["compile"] = {"sql": sql, "metric_name": metric_name}
        if not sql:
            summary = "No SQL compiled."
            trace["summary"] = summary
            ia = score_intent_accuracy(qo.analysis_type, case)
            trace["scores"] = {
                "query_correctness": 0.0,
                "query_routing": score_routing(decision.route, qo.analysis_type, case),
                "summary_relevance": score_summary_relevance(case.question, summary, case.expected_summary_tokens),
                "answer_relevance": 0.0,
                "intent_accuracy": ia,
                "gold_qo_score": round(gold_s, 3),
                "gold_qo_detail": gold_d,
            }
            trace["pass"] = False
            trace["failure_stage"] = "compiler"
            trace["fixup_deltas"] = getattr(qo, "_fixup_deltas", [])
            trace["latency_ms"] = round((time.perf_counter() - t_case_start) * 1000)
            return trace

        # Specialist routes: __analyst__ (retention/funnel/time_between) and __diagnose__
        # are intentional compile outcomes — score by intent accuracy, not SQL execution.
        if sql in ("__analyst__", "__diagnose__"):
            ia = score_intent_accuracy(qo.analysis_type, case)
            qr = score_routing(decision.route, qo.analysis_type, case)
            ar = float(max(0.0, min(1.0, (0.5 * ia + 0.5 * qr))))
            trace["summary"] = f"Routed to specialist path: {sql}"
            trace["stages"]["execution"] = {
                "ok": True,
                "specialist_route": sql,
                "row_count": 0,
                "columns": [],
                "sample_rows": [],
            }
            trace["scores"] = {
                "query_correctness": 1.0 if ia >= 1.0 else 0.5,
                "query_routing": round(qr, 3),
                "summary_relevance": 0.5,
                "answer_relevance": round(ar, 3),
                "intent_accuracy": round(ia, 3),
                "gold_qo_score": round(gold_s, 3),
                "gold_qo_detail": gold_d,
                "components": {
                    "compile_ok": True,
                    "execute_ok": True,
                    "intent_match": ia >= 1.0 if case.expected_analysis_type else None,
                    "routing_match": (decision.route == case.expected_route) if case.expected_route else None,
                    "gold_qo_match": (gold_s >= 0.999 if case.gold_qo else None),
                },
            }
            trace["pass"] = ar >= 0.75
            trace["failure_stage"] = _determine_failure_stage(trace, case) if not trace["pass"] else None
            trace["fixup_deltas"] = getattr(qo, "_fixup_deltas", [])
            trace["latency_ms"] = round((time.perf_counter() - t_case_start) * 1000)
            return trace

        try:
            df = conn.execute(sql).df()
            exec_ok = True
            exec_err = ""
        except Exception as e:
            df = pd.DataFrame()
            exec_ok = False
            exec_err = str(e)
        trace["stages"]["execution"] = {
            "ok": exec_ok,
            "error": exec_err,
            "row_count": int(len(df)),
            "columns": list(df.columns),
            "sample_rows": df.head(5).to_dict(orient="records"),
        }

        cols = list(df.columns)
        sample_rows = df.head(5).to_dict(orient="records")
        summary = build_summary(df, question=case.question)
        trace["summary"] = summary

        try:
            from core.viz.charts_plotly import evidence_chart_plotly
            fig = evidence_chart_plotly(
                qo.analysis_type or "",
                df,
                chart_title=None,
                qo_semantics=getattr(qo, "_query_semantics", None),
            )
            trace["chart_json"] = fig.to_json() if fig is not None else None
        except Exception:
            trace["chart_json"] = None

        ia = score_intent_accuracy(qo.analysis_type, case)
        qc = score_query_correctness(bool(sql), exec_ok, cols, case)
        qr = score_routing(decision.route, qo.analysis_type, case)

        # SQL structural assertions — deterministic, catches compiler bugs.
        sql_s, sql_d = score_gold_sql(sql, case)
        trace["stages"]["gold_sql"] = sql_d

        # Result value assertions — catches semantic errors (rate > 100%, wrong counts).
        result_s, result_d = score_gold_result(df, case)
        trace["stages"]["gold_result"] = result_d

        # Legacy keyword SR — kept for diagnostics but not used in AR formula.
        sr_keyword = score_summary_relevance(case.question, summary, case.expected_summary_tokens)

        # LLM judge: relevance + completeness + presentation (replaces keyword SR).
        judge = score_answer_llm_judge(
            question=case.question,
            columns=cols,
            sample_rows=sample_rows,
            summary=summary,
            openai_api_key=openai_api_key,
            judge_provider=judge_provider,
            judge_model=judge_model,
        )
        # Rule-based data-presentation check layered on top of LLM judge.
        dp = score_data_presentation(case.question, cols, sample_rows, qo.analysis_type)
        # No floor — judge scores are taken as-is. A floor inflates scores for
        # confident-but-wrong SQL; correct queries don't need it.
        j_rel  = judge["relevance"]
        j_comp = judge["completeness"]
        # AR formula: 30% query correctness, 10% routing, 15% LLM relevance,
        #             10% LLM completeness, 10% QO slot accuracy (gold_qo),
        #             10% SQL structural, 10% result values, 5% presentation.
        # gold_qo: 0.5 default when not defined → neutral; 1.0 = slots correct; 0.0 = wrong slots.
        ar = float(max(0.0, min(1.0, (
            0.30 * qc
            + 0.10 * qr
            + 0.15 * j_rel
            + 0.10 * j_comp
            + 0.10 * gold_s
            + 0.10 * sql_s
            + 0.10 * result_s
            + 0.05 * dp
        ))))
        trace["scores"] = {
            "query_correctness": round(qc, 3),
            "query_routing": round(qr, 3),
            "summary_relevance": round(sr_keyword, 3),   # legacy, diagnostic only
            "llm_judge_relevance": round(judge["relevance"], 3),
            "llm_judge_completeness": round(judge["completeness"], 3),
            "llm_judge_presentation": round(judge["presentation"], 3),
            "data_presentation": round(dp, 3),
            "gold_sql_score": round(sql_s, 3),
            "gold_sql_detail": sql_d,
            "gold_result_score": round(result_s, 3),
            "gold_result_detail": result_d,
            "answer_relevance": round(ar, 3),
            "intent_accuracy": round(ia, 3),
            "gold_qo_score": round(gold_s, 3),
            "gold_qo_detail": gold_d,
            "components": {
                "compile_ok": bool(sql),
                "execute_ok": exec_ok,
                "intent_match": ia >= 1.0 if case.expected_analysis_type else None,
                "routing_match": (
                    (decision.route == case.expected_route)
                    if case.expected_route
                    else None
                ),
                "gold_qo_match": (gold_s >= 0.999 if case.gold_qo else None),
                "gold_sql_match": (sql_s >= 0.999 if case.gold_sql else None),
                "gold_result_match": (result_s >= 0.999 if getattr(case, "gold_result", None) else None),
            },
        }
        # A case fails immediately if any hard assertion (gold_sql, gold_result) fails,
        # regardless of LLM judge score. Hard failures are deterministic bugs, not taste.
        hard_fail = (
            (case.gold_sql and sql_s < 1.0)
            or (getattr(case, "gold_result", None) and result_s < 1.0)
        )
        trace["hard_fail"] = hard_fail
        trace["pass"] = (ar >= 0.75) and not hard_fail
        trace["failure_stage"] = _determine_failure_stage(trace, case) if not trace["pass"] else None
        trace["fixup_deltas"] = getattr(qo, "_fixup_deltas", [])
    except Exception as e:
        trace["fatal_error_type"] = type(e).__name__
        trace["fatal_error"] = f"{type(e).__name__}: {e}"
        trace["scores"] = {
            "query_correctness": 0.0,
            "query_routing": 0.0,
            "summary_relevance": 0.0,
            "answer_relevance": 0.0,
            "intent_accuracy": 0.0,
            "gold_qo_score": 0.0,
            "gold_qo_detail": {"error": True},
        }
        trace["pass"] = False

    trace["latency_ms"] = round((time.perf_counter() - t_case_start) * 1000)
    return trace


def run_benchmark(
    output_name: str | None = None,
    *,
    skip_preflight: bool = False,
    case_delay_sec: float = 0.0,
    eval_provider: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
    hard: bool = False,
    hard_n: int = 50,
) -> Path:
    """
    Run the eval benchmark.

    Args:
        hard:           If True, run only the hardest hard_n questions (lowest AR
                        from the most recent completed eval run). Good for fast
                        iteration on specific fixes without running all 100.
        hard_n:         Number of hardest questions to run in hard mode (default 50).
        eval_provider:  Provider for the orchestrator calls in eval. Defaults to
                        EVAL_LLM_PROVIDER env var, then LLM_PROVIDER.
        judge_provider: Provider for the LLM judge. Defaults to JUDGE_LLM_PROVIDER env
                        var, then eval_provider, then LLM_PROVIDER.
        judge_model:    Specific model for the LLM judge (e.g. "gpt-4o-mini"). Defaults
                        to JUDGE_MODEL env var, then judge_provider's fast model.
    """
    from core.infra.llm import resolve_model, make_llm_client

    catalog, sampled = load_catalog_and_sampled()
    configure_sql_guards_from_catalog(catalog)
    metrics = build_metrics_like_chat(catalog)
    questions = build_eval_suite(catalog)

    if hard:
        hard_qs = _load_hard_questions(OUT_DIR, hard_n)
        if hard_qs:
            hard_set = set(hard_qs)
            questions = [c for c in questions if c.question in hard_set]
            print(
                f"[hard mode] Running {len(questions)} hardest cases (from last eval run)",
                file=sys.stderr,
            )
        else:
            print("[hard mode] No prior eval found — running full suite", file=sys.stderr)

    # Resolve eval provider (orchestrator calls during benchmark)
    eff_eval_provider = eval_provider or os.environ.get("EVAL_LLM_PROVIDER") or None

    # Resolve judge provider + model
    eff_judge_provider = (
        judge_provider or os.environ.get("JUDGE_LLM_PROVIDER") or eff_eval_provider or None
    )
    eff_judge_model = (
        judge_model
        or os.environ.get("JUDGE_MODEL")
        or resolve_model("fast", provider=eff_judge_provider)
    )

    # For the orchestrator key: None → make_llm_client resolves from provider's env var.
    # For OpenAI eval provider, verify the key up-front so bad keys fail fast.
    key: str | None = None
    active_provider = eff_eval_provider or os.environ.get("LLM_PROVIDER", "groq")
    if active_provider == "openai":
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is missing. Set it in your .env or set EVAL_LLM_PROVIDER "
                "to a non-OpenAI provider."
            )
        if not skip_preflight:
            _preflight_openai_key(key)

    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        traces = []
        for i, case in enumerate(questions):
            # Each case gets a fresh history — benchmark questions are independent.
            t = run_eval_case(
                conn, case, catalog, sampled, metrics, [], key,
                eval_provider=eff_eval_provider,
                judge_provider=eff_judge_provider,
                judge_model=eff_judge_model,
            )
            traces.append(t)
            # Pace API calls to reduce 429s (last case: no sleep).
            if case_delay_sec > 0 and i + 1 < len(questions):
                time.sleep(case_delay_sec)
    finally:
        conn.close()

    n = len(traces)
    fatal_errors = [str(t.get("fatal_error", "")) for t in traces if t.get("fatal_error")]
    invalid_key_errors = [e for e in fatal_errors if "invalid_api_key" in e or "Incorrect API key" in e]
    conn_errors = [e for e in fatal_errors if "Connection error" in e]

    # Latency percentiles
    latencies = sorted(t.get("latency_ms", 0) for t in traces if t.get("latency_ms"))
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)] if latencies else 0
    avg_lat = round(sum(latencies) / len(latencies)) if latencies else 0

    # Intent accuracy (cases with expected_analysis_type only)
    ia_scores = [t["scores"].get("intent_accuracy", 0.5) for t in traces
                 if t.get("expected", {}).get("expected_analysis_type")]
    avg_intent_accuracy = round(sum(ia_scores) / len(ia_scores), 3) if ia_scores else None

    gold_scores = [float(t.get("scores", {}).get("gold_qo_score", 0.5)) for t in traces]

    # Stage failure attribution — count failures by stage across all traces
    stage_counts: dict[str, int] = {}
    for t in traces:
        if not t.get("pass"):
            stage = t.get("failure_stage") or "unknown"
            stage_counts[stage] = stage_counts.get(stage, 0) + 1

    # Deterministic coverage — fraction of cases with at least one gold assertion
    n_with_gold = sum(
        1 for c in questions
        if c.gold_qo or c.gold_sql or getattr(c, "gold_result", None)
    )

    # Fixup activity — how often each pass fires (has a delta) across all traces
    fixup_fire_counts: dict[str, int] = {}
    for t in traces:
        for d in (t.get("fixup_deltas") or []):
            name = d.get("fixup", "?")
            fixup_fire_counts[name] = fixup_fire_counts.get(name, 0) + 1

    agg = {
        "n_cases": n,
        "pass_count": sum(1 for t in traces if t.get("pass")),
        "pass_rate": round(sum(1 for t in traces if t.get("pass")) / max(n, 1), 3),
        "hard_fail_count": sum(1 for t in traces if t.get("hard_fail")),
        "avg_query_correctness": round(sum(t["scores"]["query_correctness"] for t in traces) / max(n, 1), 3),
        "avg_query_routing": round(sum(t["scores"]["query_routing"] for t in traces) / max(n, 1), 3),
        "avg_summary_relevance": round(sum(t["scores"]["summary_relevance"] for t in traces) / max(n, 1), 3),
        "avg_llm_judge_relevance": round(sum(t["scores"].get("llm_judge_relevance", 0.5) for t in traces) / max(n, 1), 3),
        "avg_llm_judge_completeness": round(sum(t["scores"].get("llm_judge_completeness", 0.5) for t in traces) / max(n, 1), 3),
        "avg_data_presentation": round(sum(t["scores"].get("data_presentation", 0.5) for t in traces) / max(n, 1), 3),
        "avg_answer_relevance": round(sum(t["scores"]["answer_relevance"] for t in traces) / max(n, 1), 3),
        "avg_intent_accuracy": avg_intent_accuracy,
        "avg_gold_qo_score": round(sum(gold_scores) / max(n, 1), 3),
        "stage_failure_counts": stage_counts,
        "deterministic_coverage": round(n_with_gold / max(n, 1), 3),
        "gold_assertion_stats": {
            "gold_qo_cases":     sum(1 for c in questions if c.gold_qo),
            "gold_sql_cases":    sum(1 for c in questions if c.gold_sql),
            "gold_result_cases": sum(1 for c in questions if getattr(c, "gold_result", None)),
        },
        "fixup_fire_counts": fixup_fire_counts,
        "by_tag": _aggregate_by_tag(traces),
        "latency_avg_ms": avg_lat,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "estimated_cost_usd": round(n * _EST_COST_PER_CASE, 4),
    }

    # Trend comparison vs. previous run (loaded before we write the current one)
    trend: dict = {}
    last_run = _load_last_benchmark(OUT_DIR)
    if last_run:
        prev = last_run["aggregate"]
        trend = {
            "vs_previous_run": last_run["file"],
            "answer_relevance_delta": round(
                agg["avg_answer_relevance"] - prev.get("avg_answer_relevance", 0), 3
            ),
            "pass_count_delta": agg["pass_count"] - prev.get("pass_count", 0),
            "latency_p95_delta_ms": agg["latency_p95_ms"] - prev.get("latency_p95_ms", 0),
            "intent_accuracy_delta": (
                round((avg_intent_accuracy or 0) - (prev.get("avg_intent_accuracy") or 0), 3)
                if avg_intent_accuracy is not None and prev.get("avg_intent_accuracy") is not None
                else None
            ),
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(DB_PATH),
        "run_metadata": {
            "infra_ok": len(fatal_errors) == 0,
            "fatal_error_count": len(fatal_errors),
            "invalid_api_key_error_count": len(invalid_key_errors),
            "connection_error_count": len(conn_errors),
            "case_delay_sec": case_delay_sec,
            "hard_mode": hard,
            "hard_n": hard_n if hard else None,
            "eval_provider": eff_eval_provider,
            "judge_provider": eff_judge_provider,
            "judge_model": eff_judge_model,
            "env_load": {"paths": _env_paths, "diagnostics": _env_diag},
        },
        "aggregate": agg,
        "trend": trend,
        "cases": traces,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / (output_name or f"eval_{stamp}.json")
    out.write_text(json.dumps(payload, indent=2, default=str))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 100-question benchmark with full traces.")
    parser.add_argument("--output", type=str, default=None, help="Output filename under qa/eval_results/")
    parser.add_argument("--api-key", type=str, default=None, help="OpenAI API key override for this run")
    parser.add_argument("--print-env-diag", action="store_true", help="Print which .env files were loaded (no secret values)")
    parser.add_argument("--env-only", action="store_true", help="Print env diagnostics and exit (no benchmark)")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip OpenAI models.list() auth check (not recommended)",
    )
    parser.add_argument(
        "--case-delay",
        type=float,
        default=float(os.environ.get("EVAL_CASE_DELAY_SEC", "2.5")),
        metavar="SEC",
        help="Seconds to sleep between cases (default: 2.5 or EVAL_CASE_DELAY_SEC). Reduces OpenAI rate limits.",
    )
    parser.add_argument(
        "--hard",
        action="store_true",
        help="Run only the N hardest questions from the last eval run (lowest answer_relevance).",
    )
    parser.add_argument(
        "--hard-n",
        type=int,
        default=50,
        metavar="N",
        help="Number of hardest questions to run in --hard mode (default: 50).",
    )
    args = parser.parse_args()
    if args.api_key:
        os.environ["OPENAI_API_KEY"] = args.api_key.strip().strip('"').strip("'")
    if args.print_env_diag or args.env_only:
        print(_env_diag, file=sys.stderr)
    if args.env_only:
        return
    out = run_benchmark(
        args.output,
        skip_preflight=args.skip_preflight,
        case_delay_sec=max(0.0, args.case_delay),
        hard=args.hard,
        hard_n=args.hard_n,
    )

    # Print a structured summary to stdout so you can see key stats immediately
    import json as _json
    data = _json.loads(out.read_text())
    agg = data.get("aggregate", {})
    print(f"\n{'='*60}")
    print(f"  EVAL RESULTS  — {out.name}")
    print(f"{'='*60}")
    print(f"  Pass rate       : {agg.get('pass_rate', 0):.1%}  ({agg.get('pass_count')}/{agg.get('n_cases')} cases)")
    print(f"  Avg AR          : {agg.get('avg_answer_relevance', 0):.3f}")
    print(f"  Hard failures   : {agg.get('hard_fail_count', 0)}")
    print(f"  Intent accuracy : {agg.get('avg_intent_accuracy', 'N/A')}")
    print(f"  Gold QO score   : {agg.get('avg_gold_qo_score', 0):.3f}")
    print(f"  Deterministic coverage: {agg.get('deterministic_coverage', 0):.1%}")
    stage_fails = agg.get("stage_failure_counts", {})
    if stage_fails:
        print(f"\n  Failures by stage:")
        for stage, cnt in sorted(stage_fails.items(), key=lambda x: -x[1]):
            print(f"    {stage:<25} {cnt}")
    fixup_fires = agg.get("fixup_fire_counts", {})
    if fixup_fires:
        print(f"\n  Fixup activation (times fired across {agg.get('n_cases')} cases):")
        for name, cnt in sorted(fixup_fires.items(), key=lambda x: -x[1])[:10]:
            print(f"    {name:<35} {cnt}")
    print(f"{'='*60}")
    trend = data.get("trend", {})
    if trend:
        delta = trend.get("pass_count_delta", 0)
        sign = "+" if delta >= 0 else ""
        print(f"  vs prev run: {sign}{delta} cases, AR delta={trend.get('answer_relevance_delta', 0):+.3f}")
    print(f"\nSaved to: {out}")


if __name__ == "__main__":
    main()

