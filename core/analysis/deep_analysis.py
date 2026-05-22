"""
deep_analysis.py — Parallel multi-query investigation engine.

Implements Dot-style Deep Analysis: run 3–4 targeted sub-queries in parallel,
each going through the full pipeline (orchestrate → compile → DuckDB → investigate),
then hand all results to build_deep_story_arc() for synthesis.

The key insight: each sub-query is INDEPENDENT, so we run them concurrently
via ThreadPoolExecutor, cutting total latency to roughly one query's worth.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from core.pipeline.orchestrator import orchestrate
from core.analysis.analyst import investigate, AnalystReport
from core.agents.hypothesis_agent import HypothesisDoc


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class SubQueryResult:
    sub_question: str
    rationale: str
    priority: int = 99
    # Filled on success
    analysis_type: str = ""
    key_finding: str = ""
    sql: str = ""
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    narrative: str = ""
    next_steps: list[str] = field(default_factory=list)
    # Filled on failure
    error: str = ""
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped


# ── Single-query runner ───────────────────────────────────────────────────────

# Analysis types that deep analysis sub-queries may not produce reliably.
# diagnose has its own separate pipeline; forecast needs special handling.
_SKIP_TYPES = {"diagnose", "forecast", "clarify", "out_of_scope"}


def _dedupe_investigation_queries(queries: list[dict]) -> list[dict]:
    """Remove duplicate sub-questions (case/whitespace-insensitive) while preserving order."""
    if not queries:
        return queries
    seen: set[str] = set()
    out = []
    for q in queries:
        sq = (q.get("sub_question") or "").strip()
        key = re.sub(r"[^a-z0-9]+", " ", sq.lower()).strip()[:600]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def run_single_investigation(
    sub_question: str,
    rationale: str,
    priority: int,
    catalog: dict,
    sampled_values: dict,
    db_path: str,
    openai_api_key: Optional[str] = None,
    analysis_hint: str = "",
    parent_qo_dict: Optional[dict] = None,
) -> SubQueryResult:
    """
    Run the full analytics pipeline for one sub-question:
      orchestrate → investigate (compile + DuckDB + analyst) → SubQueryResult.

    parent_qo_dict: the parent question's QueryObject as a dict; passed as fake
    history so sub-investigations inherit the parent time window when their own
    sub-question text has no explicit time reference.

    Fails gracefully — always returns a SubQueryResult even on errors.
    """
    result = SubQueryResult(
        sub_question=sub_question,
        rationale=rationale,
        priority=priority,
    )
    try:
        # Step 1: Orchestrate — LLM fills QueryObject slots
        hint_doc = HypothesisDoc(
            question=sub_question,
            analysis_type_hint=analysis_hint,
        ) if analysis_hint else None
        # Pass parent QO as fake history so time window is inherited when
        # the sub-question has no explicit time reference of its own.
        parent_history = [{"question": "", "qo": parent_qo_dict}] if parent_qo_dict else None
        qo = orchestrate(
            question=sub_question,
            catalog=catalog,
            sampled_values=sampled_values,
            openai_api_key=openai_api_key,
            hypothesis_doc=hint_doc,
            history=parent_history,
        )

        if qo.analysis_type in _SKIP_TYPES:
            result.skipped = True
            return result

        result.analysis_type = qo.analysis_type

        # Step 2: Investigate — compile + run SQL + analyst plan + story arc
        # (investigate() handles all of this internally)
        event_sv = sampled_values.get("events", {})
        user_sv  = sampled_values.get("users", {})

        report: AnalystReport = investigate(
            question=sub_question,
            qo=qo,
            db_path=db_path,
            openai_key=openai_api_key,
            catalog=catalog,
            event_sampled_values=event_sv,
            user_sampled_values=user_sv,
            skip_narrative=True,  # deep synthesis handles narrative in one pass
        )

        # Step 3: Extract key finding from the first successful investigation step
        for inv in report.investigations:
            if not inv.error and not inv.df.empty:
                result.key_finding = inv.insight or ""
                result.df          = inv.df
                result.sql         = inv.sql
                break

        result.narrative  = report.narrative or report.executive_summary or ""
        result.next_steps = report.next_steps or []

    except Exception as exc:
        result.error = str(exc)

    return result


# ── Parallel runner ───────────────────────────────────────────────────────────

def run_parallel_investigations(
    investigation_queries: list[dict],
    catalog: dict,
    sampled_values: dict,
    db_path: str,
    openai_api_key: Optional[str] = None,
    on_complete: Optional[Callable[[int, SubQueryResult], None]] = None,
    parent_qo_dict: Optional[dict] = None,
) -> list[SubQueryResult]:
    """
    Run all sub-queries in parallel using a thread pool.

    investigation_queries: list of dicts from generate_deep_plan(), each with:
        sub_question, analysis_hint, rationale, priority

    on_complete: optional callback(index, result) called as each future resolves.
                 NOTE: Streamlit is NOT thread-safe — do not write to st.* here.
                 Use this only for non-UI side-effects (e.g., logging).

    parent_qo_dict: the parent question's QueryObject as a dict; used to seed
                    time-window inheritance for sub-investigations that lack their
                    own explicit time reference.

    Returns results sorted by priority (lowest number first), with successes first.
    """
    queries = sorted(
        [q for q in investigation_queries if q.get("sub_question")],
        key=lambda q: int(q.get("priority", 99)),
    )
    queries = _dedupe_investigation_queries(queries)[:6]  # cap at 6 parallel queries

    if not queries:
        return []

    results: list[SubQueryResult | None] = [None] * len(queries)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        future_to_idx = {
            pool.submit(
                run_single_investigation,
                q["sub_question"],
                q.get("rationale", ""),
                int(q.get("priority", 99)),
                catalog,
                sampled_values,
                db_path,
                openai_api_key,
                q.get("analysis_hint", ""),
                parent_qo_dict,
            ): i
            for i, q in enumerate(queries)
        }

        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                res = future.result()
            except Exception as exc:
                res = SubQueryResult(
                    sub_question=queries[idx]["sub_question"],
                    rationale=queries[idx].get("rationale", ""),
                    priority=int(queries[idx].get("priority", 99)),
                    error=str(exc),
                )
            results[idx] = res
            if on_complete:
                on_complete(idx, res)

    # Filter None (shouldn't happen), sort: successful first, then by priority
    final = [r for r in results if r is not None]
    final.sort(key=lambda r: (not r.ok, r.priority))
    return final
