"""
hypothesis_agent.py — Hypothesis-first problem framing.

Before touching any data, a real analyst asks "what COULD explain this?"
and forms testable, falsifiable hypotheses. Every downstream query then
tests a hypothesis rather than just fetching data.

Four root-cause categories (from the ai-analyst-lab taxonomy):
  product_change   — something we shipped broke or improved the metric
  technical_issue  — data pipeline failure, tracking bug, infra incident
  external_factor  — market event, competitor action, seasonality
  mix_shift        — user population composition changed, not behaviour

The output is injected into the orchestrator so the analysis is directed,
not exploratory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from core.infra.llm import call_llm, make_llm_client, LLM_STRONG, LLM_FAST
from core.infra.tracer import track


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Hypothesis:
    category: str           # "product_change" | "technical_issue" | "external_factor" | "mix_shift"
    statement: str          # "If X happened, then Y should show Z"
    expected_signal: str    # specific metric / dimension / event to check
    priority: int           # 1 = most likely, check first


@dataclass
class HypothesisDoc:
    question: str
    analysis_type_hint: str             # recommended analysis_type for orchestrator
    hypotheses: list[Hypothesis] = field(default_factory=list)
    investigation_context: str = ""     # one-line summary injected into orchestrator
    investigation_queries: list[dict] = field(default_factory=list)
    # Each dict: {sub_question, analysis_hint, rationale, priority}


# ── Catalog summary builder ───────────────────────────────────────────────────

def _catalog_summary(catalog: dict) -> str:
    events, metrics = [], []
    for tname, tdata in catalog.items():
        if tname.startswith("__"):
            continue
        for evt in list(tdata.get("event_semantics", {}).keys())[:30]:
            events.append(evt)
        for m in tdata.get("suggested_metrics", []):
            metrics.append(f"{m.get('id','')} — {m.get('name','')}")
    lines = []
    if events:
        lines.append("Available events: " + ", ".join(events))
    if metrics:
        lines.append("Pre-built metrics: " + ", ".join(metrics[:20]))

    # Rich business context — helps hypothesis agent distinguish seasonal patterns
    # from anomalies and form more accurate external_factor hypotheses.
    bctx = catalog.get("__business_context__", {}) or {}
    if bctx.get("industry"):
        lines.append(f"Industry: {bctx['industry']}")
    if bctx.get("company_name"):
        lines.append(f"Company: {bctx['company_name']}")
    if bctx.get("known_seasonality"):
        lines.append(f"Known seasonality patterns: {bctx['known_seasonality']}")
    if bctx.get("recent_campaigns"):
        lines.append(f"Recent campaigns / marketing events: {bctx['recent_campaigns']}")
    if bctx.get("product_context"):
        lines.append(f"Product context: {bctx['product_context']}")
    if bctx.get("notes"):
        lines.append(f"Business notes: {bctx['notes']}")

    # Business events uploaded by the user (PRDs, campaigns, incidents, strategy docs).
    # Surfaced here so the LLM can form targeted product_change / external_factor
    # hypotheses tied to real dates, specific metrics, and known directions.
    business_events = bctx.get("business_events") or []

    # Conflict detection: warn when two events predict opposite metric directions
    # in the same time window — the LLM needs to know so it can flag ambiguity.
    if len(business_events) >= 2:
        try:
            from core.semantic.doc_ingestor import find_metric_conflicts
            conflicts = find_metric_conflicts(business_events)
            if conflicts:
                lines.append("\n⚠ Conflicting business signals — flag these when relevant:")
                for c in conflicts[:4]:
                    overlap = ""
                    if c.get("overlap_start") or c.get("overlap_end"):
                        parts = [p for p in [c.get("overlap_start"), c.get("overlap_end")] if p]
                        overlap = f" (overlap: {'–'.join(parts)})"
                    lines.append(
                        f"  [{c['metric']}] '{c['event_a_title']}' predicts {c['direction_a']}, "
                        f"'{c['event_b_title']}' predicts {c['direction_b']}{overlap}"
                    )
        except Exception:
            pass  # advisory only — never block hypothesis generation

    if business_events:
        lines.append("\nUploaded business context (use to form specific hypotheses):")
        for evt in business_events[-12:]:  # cap at 12 to stay within prompt budget
            etype     = evt.get("type", "general")
            title     = evt.get("title", "")
            date_from = evt.get("date_from", "")
            date_to   = evt.get("date_to", "")
            summary   = evt.get("summary", "")

            date_part = f"from {date_from}" + (f" to {date_to}" if date_to else "") if date_from else ""
            header = f"  [{etype}] {title}" + (f" · {date_part}" if date_part else "")
            lines.append(header)

            # Per-metric impacts give the LLM precise signals to form falsifiable hypotheses
            metric_impacts = evt.get("metric_impacts") or []
            if metric_impacts:
                impact_strs = []
                for m in metric_impacts[:4]:
                    metric    = m.get("metric", "")
                    direction = m.get("direction", "unknown")
                    magnitude = m.get("magnitude") or ""
                    note      = m.get("note") or ""
                    s = f"{metric} {direction}"
                    if magnitude:
                        s += f" {magnitude}"
                    if note:
                        s += f" ({note})"
                    impact_strs.append(s)
                lines.append(f"    Metric impacts: {' | '.join(impact_strs)}")

            # Type-specific metadata surfaces the most hypothesis-relevant facts
            meta = evt.get("type_metadata") or {}
            if etype == "feature_launch":
                if meta.get("rollout_pct") is not None:
                    lines.append(f"    Rollout: {meta['rollout_pct']}%"
                                 + (" (experiment)" if meta.get("is_experiment") else " (full launch)"))
                if meta.get("risk_flags"):
                    lines.append(f"    Risks: {'; '.join(meta['risk_flags'][:2])}")
            elif etype == "incident":
                if meta.get("root_cause"):
                    lines.append(f"    Root cause: {meta['root_cause']}")
                if meta.get("affected_tables"):
                    lines.append(f"    Affected tables: {', '.join(meta['affected_tables'])}")
                recoverable = meta.get("is_data_recoverable")
                if recoverable is not None:
                    lines.append(f"    Data recoverable: {recoverable}")
            elif etype == "campaign":
                if meta.get("channels"):
                    lines.append(f"    Channels: {', '.join(meta['channels'][:3])}")

            if summary:
                lines.append(f"    {summary[:180]}")

    return "\n".join(lines)


def _history_block(history: list[dict]) -> str:
    if not history:
        return ""
    lines = ["Recent session context (last 3 turns):"]
    for turn in history[-3:]:
        q       = turn.get("question", "")
        at      = turn.get("qo", {}).get("analysis_type", "")
        ev      = turn.get("qo", {}).get("event", "")
        verdict = turn.get("memory", {}).get("hypothesis_verdict", "")
        summary = turn.get("memory", {}).get("summary", "")
        entry   = f"  • \"{q}\" → {at} / {ev}"
        if verdict:
            entry += f" (verdict: {verdict})"
        if summary:
            entry += f"\n    Summary: {summary[:150]}"
        lines.append(entry)
    return "\n".join(lines) + "\n\n"


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """You are a senior data analyst. Before any data is queried, your job is to
frame the question as 2–3 testable, falsifiable hypotheses.

Each hypothesis MUST:
- Belong to exactly one category:
    product_change   — a feature/release/experiment caused the change
    technical_issue  — tracking bug, pipeline failure, data quality issue
    external_factor  — seasonality, market event, competitor, external shock
    mix_shift        — population composition changed (not behaviour per se)
- Be falsifiable: state what data signal would CONFIRM or REJECT it
- Name the specific metric, dimension, or event that would serve as evidence
- Be ranked 1–3 by likelihood (1 = most likely given the question)

You must cover at least 2 of the 4 categories across your hypotheses.

{catalog_block}

{history_block}Respond ONLY with JSON — no markdown, no explanation:
{{
  "analysis_type_hint": "metric|segment|funnel|funnel_compare|journey|retention|behavioral_cohort|time_between|same_month_anchor|diagnose",
  "investigation_context": "<one sentence: what this analysis is really trying to determine>",
  "hypotheses": [
    {{
      "category": "product_change|technical_issue|external_factor|mix_shift",
      "statement": "<If X happened then Y should show Z>",
      "expected_signal": "<specific metric, event, or dimension to check>",
      "priority": 1
    }}
  ]
}}"""


# ── Main entry point ──────────────────────────────────────────────────────────

@track(name="hypothesis_agent", tags=["agents"], capture_input=False, capture_output=False)
def generate_hypotheses(
    question: str,
    catalog: dict,
    history: Optional[list[dict]] = None,
    openai_api_key: Optional[str] = None,
) -> HypothesisDoc:
    """
    Generate structured hypotheses for a user question before any SQL is run.
    Returns a HypothesisDoc whose investigation_context is injected into the
    orchestrator so every downstream query tests a hypothesis.

    Fails gracefully — returns an empty HypothesisDoc on any error so it
    never blocks the main analysis pipeline.
    """
    client = make_llm_client(openai_api_key)

    system = _SYSTEM.format(
        catalog_block=_catalog_summary(catalog),
        history_block=_history_block(history or []),
    )

    try:
        resp = call_llm(
            client,
            call_site="hypothesis_agent.generate_hypotheses",
            model=LLM_FAST,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": question},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
    except Exception as exc:
        from core.infra.logger import log_llm_call as _log
        _log(call_site="hypothesis_agent.generate_hypotheses", model=LLM_FAST, error=str(exc), latency_ms=0)
        return HypothesisDoc(question=question, analysis_type_hint="")

    hypotheses = sorted(
        [
            Hypothesis(
                category=h.get("category", ""),
                statement=h.get("statement", ""),
                expected_signal=h.get("expected_signal", ""),
                priority=int(h.get("priority", 99)),
            )
            for h in data.get("hypotheses", [])
            if isinstance(h, dict)
        ],
        key=lambda h: h.priority,
    )

    return HypothesisDoc(
        question=question,
        analysis_type_hint=data.get("analysis_type_hint", ""),
        hypotheses=hypotheses,
        investigation_context=data.get("investigation_context", ""),
    )


# ── Deep Analysis plan generator ──────────────────────────────────────────────

_DEEP_PLAN_SYSTEM = """You are a senior data analyst. Decompose a complex analytics
question into 4–6 targeted sub-investigations that collectively answer it from
DIFFERENT analytical angles. Always generate at least 5 sub-investigations for growth,
decline, or lifecycle questions.

HARD RULES:
- Each sub-question must be SPECIFIC and independently answerable with one SQL query
- Use ONLY these analysis types: metric, segment, funnel, retention, behavioral_cohort,
  time_between, user_lifecycle — never diagnose, forecast, or out_of_scope
- Sub-questions must reference ONLY real events and dimensions from the catalog below
- Do NOT use time_between unless the question explicitly asks about speed or time-to-convert
- Prioritise 1 = most essential to answering the main question
- Cover at least 3 distinct angles — never repeat the same analysis_hint twice
- For growth or decline questions: ALWAYS include a [behavioral_cohort] sub-investigation
  that splits new users (first event ≤30 days ago) vs returning users to identify which
  population is driving the change (mix-shift check)
- If the catalog block includes "Known seasonality patterns" or "Recent campaigns /
  marketing events", add one [metric] or [segment] sub-investigation that tests whether
  the observed change is consistent with that seasonal/campaign baseline or anomalous

QUESTION-TYPE PLAYBOOKS — select the matching template and follow it:

▶ WHY DID [METRIC] INCREASE / GROW:
  1. [metric]            — "What is the month-by-month trend of [metric] and total active users over [period]?"
  2. [metric]            — "How did new user acquisition (onboarding completions) change over [period]?"
  3. [retention]         — "What percentage of users who completed [action] in January returned to do another [action] in February and March? Show 30-day monthly retention rate."
  4. [funnel]            — "Did the conversion rate from onboarding to [action] improve over [period]?"
  5. [segment]           — "How does the growth in [metric] break down month-on-month by user demographics (age bucket, income bucket)? Show user count distribution per month."
  6. [user_lifecycle]    — "What is the current distribution of users across lifecycle stages (New, Active, At Risk, Churned) and how has it shifted over [period]?"
  7. [behavioral_cohort] — "Is this growth driven by new users (first event in last 30 days) or by returning users engaging more — which population drove the increase?"

▶ WHY DID [METRIC] DECREASE / DROP / DECLINE:
  1. [metric]            — "What is the trend of [metric] and total active users over [period]?"
  2. [funnel]            — "Where in the onboarding-to-[action] funnel are users dropping off?"
  3. [retention]         — "What percentage of users who completed [action] in January returned in February and March? Show 30-day monthly retention rate — is it declining?"
  4. [segment]           — "Which platform or channel drove the decline in [metric]?"
  5. [segment]           — "How does the decline in [metric] break down by user demographics (age bucket, income bucket) month-on-month?"
  6. [user_lifecycle]    — "How has the lifecycle stage distribution (New, Active, At Risk, Churned) shifted over [period] — is the At Risk or Churned bucket growing?"
  7. [behavioral_cohort] — "Did the decline come primarily from new user acquisition dropping, or from existing users reducing activity — which population changed most?"

▶ RETENTION / ARE USERS COMING BACK:
  1. [retention]         — "What percentage of users who completed [event] in month 1 returned to do it again within 30 days? Show 30-day retention rate by monthly cohort."
  2. [retention]         — "How does 30-day retention compare across monthly cohorts — are newer cohorts retaining better or worse?"
  3. [behavioral_cohort] — "How many users completed [event] but never returned to do it again?"
  4. [metric]            — "Is the overall retention rate improving or declining month-over-month?"

▶ CONVERSION / FUNNEL:
  1. [funnel]            — "What is the step-by-step conversion rate through the [funnel] funnel?"
  2. [segment]           — "Which platform or channel has the lowest conversion rate in the funnel?"
  3. [funnel]            — "Is overall funnel conversion improving or declining month-over-month?"
  4. [behavioral_cohort] — "How many users started [funnel step 1] but never completed [funnel step 2]?"

▶ COMPARE PERIOD A vs PERIOD B:
  1. [metric]            — "What is the [metric] count in [period A] vs [period B], month by month?"
  2. [metric]            — "How did new user acquisition differ between [period A] and [period B]?"
  3. [funnel]            — "Did conversion rates through the funnel change between the two periods?"
  4. [segment]           — "Which platform or segment drove the difference between the two periods?"

▶ USER ACQUISITION / GROWTH:
  1. [metric]            — "What is the monthly new user acquisition trend (onboarding completions)?"
  2. [funnel]            — "What is the onboarding completion rate and where do users drop off?"
  3. [behavioral_cohort] — "Of newly acquired users, how many completed a transaction within 30 days?"
  4. [segment]           — "Which acquisition channel is driving the most new users?"

▶ ENGAGEMENT / STICKINESS:
  1. [metric]            — "What is the trend of daily and monthly active users over the period?"
  2. [retention]         — "What is the D7 and D30 retention rate for active users?"
  3. [metric]            — "How frequently do active users perform [action] per month on average?"
  4. [segment]           — "Which user segment or platform shows the highest engagement?"

▶ SEGMENTATION / BREAKDOWN:
  1. [segment]           — "How does [metric] break down by [dimension 1] over [period]?"
  2. [segment]           — "How does [metric] break down by [dimension 2] over [period]?"
  3. [metric]            — "What is the overall trend of [metric] for context?"
  4. [funnel]            — "Does funnel conversion differ significantly across the top segments?"

▶ GENERAL / DIAGNOSTIC (no clear pattern above):
  1. [metric]            — trend over time
  2. [segment]           — breakdown by primary dimension in catalog
  3. [funnel or retention] — downstream behaviour of the cohort
  4. [behavioral_cohort] — which users are NOT converting / returning

{catalog_block}

{playbook_block}

Respond ONLY with JSON — no markdown:
{{
  "investigation_context": "<one sentence: what we are trying to determine overall>",
  "investigation_queries": [
    {{
      "sub_question": "<specific question answerable by one SQL query, using real catalog events>",
      "analysis_hint": "metric|segment|funnel|retention|behavioral_cohort|time_between|user_lifecycle",
      "rationale": "<why this angle matters for the main question>",
      "priority": 1
    }}
  ]
}}"""


def generate_deep_plan(
    question: str,
    catalog: dict,
    openai_api_key: Optional[str] = None,
    playbook=None,
) -> HypothesisDoc:
    """
    Generate a Deep Analysis investigation plan: 3–4 targeted sub-questions
    that collectively answer the main question from different analytical angles.
    Called only when the user explicitly triggers Deep Analysis mode.
    Falls back to an empty HypothesisDoc on any error.
    """
    client = make_llm_client(openai_api_key)

    playbook_block = playbook.to_prompt_block() if playbook is not None else ""
    system = _DEEP_PLAN_SYSTEM.format(
        catalog_block=_catalog_summary(catalog),
        playbook_block=playbook_block,
    )

    try:
        resp = call_llm(
            client,
            call_site="hypothesis_agent.generate_deep_plan",
            model=LLM_STRONG,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": question},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
    except Exception:
        return HypothesisDoc(question=question, analysis_type_hint="")

    raw_queries = data.get("investigation_queries", [])
    investigation_queries = sorted(
        [q for q in raw_queries if isinstance(q, dict) and q.get("sub_question")],
        key=lambda q: int(q.get("priority", 99)),
    )[:6]

    return HypothesisDoc(
        question=question,
        analysis_type_hint="",
        investigation_context=data.get("investigation_context", ""),
        investigation_queries=investigation_queries,
    )
