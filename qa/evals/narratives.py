"""
qa/evals/narratives.py — Narrative / insight quality evaluation.

Extends the base LLM judge in eval_benchmark.py with:
  1. Faithfulness — do numeric claims in the narrative match the data?
  2. Hallucination detection — claims not grounded in the SQL result
  3. Actionability — does the insight suggest what to do next?
  4. Causal claim validation — "because", "due to" claims have data support

Usage:
    from qa.evals.narratives import score_narrative_faithfulness, judge_narrative_full

    result = score_narrative_faithfulness(narrative, df_sample)
    full = judge_narrative_full(question, narrative, df_columns, df_sample)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


# ── Rule-based faithfulness scoring ──────────────────────────────────────────

_NUMBER_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*(%|percent|percentage|x|times)?\b", re.IGNORECASE)
_CAUSAL_RE = re.compile(r"\b(because|due to|driven by|led to|caused by|as a result of|owing to)\b", re.IGNORECASE)
_ACTIONABLE_RE = re.compile(
    r"\b(should|recommend|suggest|consider|investigate|focus|prioritize|improve|"
    r"optimize|review|monitor|address|reduce|increase|track)\b",
    re.IGNORECASE,
)


def _extract_numbers_from_text(text: str) -> list[float]:
    """Pull numeric values from a narrative string."""
    nums = []
    for m in _NUMBER_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            nums.append(float(raw))
        except ValueError:
            pass
    return nums


def _data_numeric_values(df_sample: list[dict]) -> set[float]:
    """Extract all numeric values from data rows, rounded to 1 decimal."""
    vals: set[float] = set()
    for row in df_sample:
        for v in row.values():
            try:
                fv = float(v)
                vals.add(round(fv, 1))
                # Also add approximate variants (±5%) to handle rounding
                vals.add(round(fv * 1.05, 1))
                vals.add(round(fv * 0.95, 1))
            except (TypeError, ValueError):
                pass
    return vals


def score_narrative_faithfulness(
    narrative: str,
    df_sample: list[dict],
    tolerance: float = 0.10,
) -> dict[str, Any]:
    """
    Rule-based faithfulness check: numeric claims in the narrative should
    be traceable to the actual query result.

    Score:
        1.0 — no numeric claims, or all claims supported by data
        0.5 — some claims unverifiable (data may have been summarised)
        0.0 — claims clearly contradict data values

    Returns dict with score, grounded_numbers, ungrounded_numbers.
    """
    if not narrative or not df_sample:
        return {"score": 0.5, "reason": "no_data_to_verify", "grounded": [], "ungrounded": []}

    claimed_nums = _extract_numbers_from_text(narrative)
    if not claimed_nums:
        return {"score": 1.0, "reason": "no_numeric_claims", "grounded": [], "ungrounded": []}

    data_vals = _data_numeric_values(df_sample)
    grounded, ungrounded = [], []

    for num in claimed_nums:
        # A number is grounded if it appears in the data within tolerance
        match = any(abs(num - dv) / max(abs(dv), 1.0) <= tolerance for dv in data_vals)
        if match:
            grounded.append(num)
        else:
            ungrounded.append(num)

    total = len(claimed_nums)
    if total == 0:
        score = 1.0
    elif len(grounded) / total >= 0.8:
        score = 1.0
    elif len(grounded) / total >= 0.5:
        score = 0.6
    else:
        score = 0.3

    return {
        "score": round(score, 3),
        "total_numeric_claims": total,
        "grounded": grounded,
        "ungrounded": ungrounded,
        "grounded_rate": round(len(grounded) / total, 3),
    }


def score_causal_claims(narrative: str, df_sample: list[dict]) -> dict[str, Any]:
    """
    Flag causal claims ("because", "due to", etc.) in the narrative.
    A causal claim is flagged if the data has fewer than 2 rows (insufficient
    evidence for root-cause attribution).

    Returns {"has_causal_claims": bool, "score": float, "flags": list}.
    """
    has_causal = bool(_CAUSAL_RE.search(narrative or ""))
    if not has_causal:
        return {"has_causal_claims": False, "score": 1.0, "flags": []}

    # Causal claims need multi-row or multi-column data to be supported
    n_rows = len(df_sample)
    n_cols = len(df_sample[0]) if df_sample else 0
    has_support = n_rows >= 2 and n_cols >= 2

    flags = [m.group(0) for m in _CAUSAL_RE.finditer(narrative)]
    score = 0.7 if has_support else 0.3

    return {
        "has_causal_claims": True,
        "causal_phrases": flags,
        "data_support": has_support,
        "score": score,
        "note": (
            "Causal claim has data support (multi-row breakdown)"
            if has_support
            else "Causal claim without supporting breakdown data — potential hallucination"
        ),
    }


def score_actionability(narrative: str) -> float:
    """
    Simple rule-based actionability score: does the insight recommend action?
    0.0 — purely descriptive, no recommendation
    1.0 — contains at least one actionable recommendation
    """
    if not narrative:
        return 0.0
    return 1.0 if _ACTIONABLE_RE.search(narrative) else 0.0


# ── LLM judge for full narrative quality ─────────────────────────────────────

_NARRATIVE_JUDGE_SYSTEM = """\
You are an expert data analyst evaluating the quality of AI-generated analytics insights.

You will be given:
1. The user's original question
2. The data returned (columns, row count, sample rows)
3. The AI-generated narrative/insight

Score the narrative on FOUR dimensions, each from 0.0 to 1.0:

faithfulness:
  1.0 = all numeric claims match the data exactly
  0.7 = minor rounding differences, no invented numbers
  0.3 = numbers mentioned that don't appear in data
  0.0 = narrative contradicts the data

hallucination:
  1.0 = narrative is fully grounded in the data shown
  0.7 = minor unsupported claims but no contradictions
  0.3 = significant claims not derivable from data
  0.0 = narrative invents entities, segments, or trends not in data

actionability:
  1.0 = clear, specific next step or recommendation
  0.7 = directional guidance present
  0.3 = vague suggestion
  0.0 = purely descriptive, no actionable guidance

clarity:
  1.0 = clear, concise, non-technical language
  0.7 = mostly clear with minor jargon
  0.3 = confusing or overly technical
  0.0 = incomprehensible

Rules:
- Do NOT penalize faithfulness for the AI summarizing or rounding reasonably.
- DO penalize hallucination when the narrative mentions trends, segments, or causes
  not present in the data columns.
- Causal claims ("because", "due to") with no breakdown dimension in data = hallucination 0.5.

Respond ONLY with valid JSON:
{"faithfulness": 0.0, "hallucination": 1.0, "actionability": 0.0, "clarity": 0.0}
"""


def judge_narrative_full(
    question: str,
    narrative: str,
    df_columns: list[str],
    df_sample: list[dict],
    *,
    openai_api_key: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
) -> dict[str, float]:
    """
    LLM-as-judge for narrative quality across four dimensions.

    Combines rule-based faithfulness with LLM judgment for hallucination,
    actionability, and clarity. Falls back to rule-based scores on error.

    Returns {"faithfulness", "hallucination", "actionability", "clarity",
             "avg_score", "rule_faithfulness", "rule_causal", "rule_actionability"}.
    """
    # Rule-based components — always computed (no LLM required)
    rule_faith = score_narrative_faithfulness(narrative, df_sample)
    rule_causal = score_causal_claims(narrative, df_sample)
    rule_action = score_actionability(narrative)

    rule_scores = {
        "rule_faithfulness": rule_faith["score"],
        "rule_causal_score": rule_causal["score"],
        "rule_actionability": rule_action,
        "rule_has_causal_claims": rule_causal["has_causal_claims"],
    }

    # Attempt LLM judge
    _default_llm = {
        "faithfulness": rule_faith["score"],
        "hallucination": 0.5,
        "actionability": rule_action,
        "clarity": 0.5,
    }

    try:
        import os
        from core.infra.llm import make_llm_client, resolve_model

        eff_provider = judge_provider or os.environ.get("JUDGE_LLM_PROVIDER") or None
        eff_model = (
            judge_model
            or os.environ.get("JUDGE_MODEL")
            or resolve_model("fast", provider=eff_provider)
        )

        recent_rows = df_sample[-5:] if df_sample else []
        data_block = (
            f"Columns: {df_columns}\n"
            f"Total rows: {len(df_sample)}\n"
            f"Sample rows: {recent_rows}"
        )
        user_msg = (
            f'Question: "{question}"\n\n'
            f"Data:\n{data_block}\n\n"
            f'Narrative: "{narrative}"\n\n'
            "Your JSON rating:"
        )
        client = make_llm_client(provider=eff_provider)
        resp = client.chat.completions.create(
            model=eff_model,
            messages=[
                {"role": "system", "content": _NARRATIVE_JUDGE_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=80,
        )
        raw = resp.choices[0].message.content.strip()
        parsed = json.loads(raw)
        llm_scores = {
            "faithfulness":  float(max(0.0, min(1.0, parsed.get("faithfulness",  rule_faith["score"])))),
            "hallucination": float(max(0.0, min(1.0, parsed.get("hallucination", 0.5)))),
            "actionability": float(max(0.0, min(1.0, parsed.get("actionability", rule_action)))),
            "clarity":       float(max(0.0, min(1.0, parsed.get("clarity",       0.5)))),
        }
    except Exception:
        llm_scores = _default_llm

    avg = round(sum(llm_scores.values()) / max(len(llm_scores), 1), 3)
    return {**llm_scores, "avg_score": avg, **rule_scores}


def score_narratives_from_traces(
    traces: list[dict[str, Any]],
    *,
    judge_provider: str | None = None,
    judge_model: str | None = None,
    skip_llm: bool = False,
) -> dict[str, Any]:
    """
    Score narrative quality across all traces that have a summary field.

    Args:
        skip_llm: If True, use only rule-based scores (faster, no API cost).

    Returns aggregate stats + per-trace breakdowns.
    """
    results = []
    for t in traces:
        narrative = t.get("summary") or ""
        if not narrative or narrative.startswith("No data returned"):
            continue
        execution = t.get("stages", {}).get("execution") or {}
        df_columns = execution.get("columns", [])
        df_sample = execution.get("sample_rows", [])
        question = t.get("question", "")

        if skip_llm:
            rf = score_narrative_faithfulness(narrative, df_sample)
            rc = score_causal_claims(narrative, df_sample)
            ra = score_actionability(narrative)
            scores = {
                "rule_faithfulness": rf["score"],
                "rule_causal_score": rc["score"],
                "rule_actionability": ra,
                "avg_score": round((rf["score"] + rc["score"] + ra) / 3, 3),
                "faithfulness": rf["score"],
                "hallucination": 0.5,
                "actionability": ra,
                "clarity": 0.5,
            }
        else:
            scores = judge_narrative_full(
                question, narrative, df_columns, df_sample,
                judge_provider=judge_provider,
                judge_model=judge_model,
            )

        results.append({
            "question": question,
            "narrative": narrative[:200],
            "scores": scores,
        })

    if not results:
        return {"n_scored": 0, "avg_faithfulness": 0.0, "avg_hallucination": 0.0,
                "avg_actionability": 0.0, "avg_clarity": 0.0, "cases": []}

    n = len(results)
    return {
        "n_scored": n,
        "avg_faithfulness":  round(sum(r["scores"].get("faithfulness",  0) for r in results) / n, 3),
        "avg_hallucination": round(sum(r["scores"].get("hallucination", 0) for r in results) / n, 3),
        "avg_actionability": round(sum(r["scores"].get("actionability", 0) for r in results) / n, 3),
        "avg_clarity":       round(sum(r["scores"].get("clarity",       0) for r in results) / n, 3),
        "cases": results,
    }
