"""
story_architect.py — Context → Tension → Resolution narrative arc.

A real analyst doesn't present a list of findings — they tell a story.
The structure is always:

  CONTEXT     → Baseline. What were we expecting? What does normal look like?
  TENSION     → What changed, by how much, why? Which hypothesis does the data support?
  RESOLUTION  → Root cause (if diagnosable). Business impact. Concrete next action.

This agent replaces the flat synthesize() call with structured narrative beats.
Each beat states an insight (not a chart description), cites exact evidence, and
answers "so what?" — following Storytelling with Data principles.

The output narrative is aware of:
  • Hypotheses (from hypothesis_agent) — tells you which one the data supports
  • Validation grade (from validator) — calibrates confidence language

LLM call count: 1 (replaces the existing synthesize() call)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.infra.llm import call_llm, make_llm_client, LLM_STRONG
from core.infra.tracer import track
from core.sql.query_object import QueryObject

# Sentinel returned when JSON parse / LLM call fails. Diagnose must treat this as
# empty narrative so the plain-language fallback can run.
STORY_ARC_FAILURE_SUMMARY = "Unable to construct narrative arc."


def _parse_llm_json_object(raw: str) -> dict:
    """Parse JSON from an LLM reply; tolerate markdown fences and trailing prose."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty LLM response")
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I | re.M)
        s = re.sub(r"\s*```\s*$", "", s)
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i = s.find("{")
        j = s.rfind("}")
        if i >= 0 and j > i:
            return json.loads(s[i : j + 1])
        raise


# ── Data structures ───────────────────────────────────────────────────────────

# Deep Analysis report structure (multi-query synthesis)
@dataclass
class DeepSection:
    title: str         # the sub-question that was investigated
    finding: str       # key insight sentence
    key_number: str    # most important number / metric
    rationale: str     # why this angle was investigated


@dataclass
class DeepStoryArc:
    headline: str                              # single quantified top finding
    executive_summary: str                     # 2-3 sentences covering main finding + root cause
    sections: list[DeepSection] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    hypothesis_verdict: str = "inconclusive"

@dataclass
class NarrativeBeat:
    phase: str        # "context" | "tension" | "resolution"
    headline: str     # takeaway statement — NOT "Chart showing X"
    evidence: str     # exact numbers from investigation results
    implication: str  # so what? why does this matter?


@dataclass
class StoryArc:
    executive_summary: str              # 3–5 sentences: question + finding + insight + action
    beats: list[NarrativeBeat] = field(default_factory=list)
    full_narrative: str = ""            # flowing prose — rendered in the UI
    next_steps: list[str] = field(default_factory=list)
    hypothesis_verdict: str = ""        # which hypothesis the data supports (or "inconclusive")
    confidence_label: str = ""


# ── Investigation summariser ──────────────────────────────────────────────────

def _summarise_investigations(plan: list, max_rows: int = 8) -> str:
    blocks = []
    for inv in plan:
        if inv.error or inv.df.empty:
            continue
        preview = inv.df.head(max_rows).to_string(index=False, max_cols=8)
        blocks.append(
            f"[{inv.name.upper()}] Purpose: {inv.purpose}\n"
            f"{preview}\n"
            f"Key finding: {inv.insight or '(see data above)'}"
        )
    return "\n\n".join(blocks) if blocks else "(no investigation data available)"


# ── Event semantic context ────────────────────────────────────────────────────

# Maps (journey, stage) → the correct analytical framing and forbidden terms.
# Prevents the LLM from applying "retention" language to acquisition events, etc.
_EVENT_FRAMING: dict[tuple[str, str], tuple[str, str]] = {
    ("onboarding", "started"):      ("USER ACQUISITION (top of funnel)", "retention, re-engagement, churn"),
    ("onboarding", "completed"):    ("USER ACQUISITION / ACTIVATION", "retention, re-engagement, churn"),
    ("onboarding", "intermediate"): ("ACTIVATION FUNNEL", "retention, churn"),
    ("transaction", "started"):     ("TRANSACTION INTENT / CONVERSION FUNNEL", "acquisition, onboarding"),
    ("transaction", "completed"):   ("MONETISATION / CONVERSION", "acquisition, onboarding"),
    ("transaction", "failed"):      ("TRANSACTION FAILURE / QUALITY", "acquisition, retention"),
    ("verification", "started"):    ("COMPLIANCE / ACTIVATION FUNNEL", "retention, churn"),
    ("verification", "completed"):  ("COMPLIANCE ACTIVATION", "retention, churn"),
    ("engagement", "observed"):     ("ENGAGEMENT / RETENTION", "acquisition, onboarding"),
    ("authentication", "started"):  ("PRODUCT USAGE / SESSION", "acquisition"),
    ("authentication", "completed"): ("PRODUCT USAGE / SESSION", "acquisition"),
}


def _event_context_block(qo: "QueryObject", catalog: "Optional[dict]") -> str:
    """
    Inject event-stage framing so the LLM uses the correct analytical vocabulary.
    For example, an onboarding completion drop is an ACQUISITION problem, not a
    retention problem — these users never fully joined the product.
    """
    if not catalog or not qo.event:
        return ""
    event_meta = (
        catalog.get("events", {})
               .get("event_semantics", {})
               .get(qo.event, {})
    )
    journey = event_meta.get("journey", "")
    stage   = event_meta.get("stage", "")
    if not journey and not stage:
        return ""

    framing, forbidden = _EVENT_FRAMING.get(
        (journey, stage),
        (f"{journey.upper()} / {stage.upper()}", ""),
    )

    lines = [
        f"EVENT CONTEXT:",
        f"  Event '{qo.event}' → journey={journey}, stage={stage}",
        f"  Analytical frame: {framing}",
    ]
    if forbidden:
        lines.append(f"  FORBIDDEN TERMS for this event: {forbidden}")
        lines.append(f"  (e.g. if onboarding dropped, say 'fewer users completed onboarding' NOT 'retention declined')")
    return "\n".join(lines)


# ── Context injectors ─────────────────────────────────────────────────────────

def _hypothesis_block(hyp_doc) -> str:
    if not hyp_doc or not hyp_doc.hypotheses:
        return ""
    lines = ["Hypotheses formed before querying:"]
    for h in hyp_doc.hypotheses:
        lines.append(f"  H{h.priority} [{h.category}]: {h.statement}")
        lines.append(f"    Signal to check: {h.expected_signal}")
    lines.append(f"  Investigation focus: {hyp_doc.investigation_context}")
    return "\n".join(lines)


def _validation_block(val) -> str:
    if not val:
        return ""
    lines = [f"Validation result: {val.summary}"]
    if val.sanity_flag == "SUSPICIOUS":
        lines.append(f"  ⚠ Sanity check SUSPICIOUS: {val.sanity_reason}")
    for w in val.arithmetic_warnings:
        icon = "✗" if w.severity == "error" else "⚠"
        lines.append(f"  {icon} {w.check}: {w.detail}")
    return "\n".join(lines)


def _narrative_thread_block(narrative_thread: Optional[str]) -> str:
    """
    Inject prior-session findings so the narrative can build continuity,
    detect repeating patterns, and avoid restating conclusions the user already knows.
    """
    if not narrative_thread:
        return ""
    return (
        "SESSION NARRATIVE THREAD (prior findings in this session — "
        "build on these verdicts, detect patterns, avoid repeating conclusions the user already knows):\n"
        + narrative_thread
    )


def _business_context_block(catalog: Optional[dict]) -> str:
    """
    Inject business context (industry, known seasonality, recent campaigns, product events)
    so the narrative can distinguish expected seasonal variation from real anomalies,
    and tailor prescriptive actions to the company's actual levers.
    """
    if not catalog:
        return ""
    bctx = catalog.get("__business_context__", {}) or {}
    lines = []
    if bctx.get("industry"):
        lines.append(f"  Industry: {bctx['industry']}")
    if bctx.get("company_name"):
        lines.append(f"  Company: {bctx['company_name']}")
    if bctx.get("known_seasonality"):
        lines.append(f"  Known seasonality: {bctx['known_seasonality']}")
    if bctx.get("recent_campaigns"):
        lines.append(f"  Recent campaigns/events: {bctx['recent_campaigns']}")
    if bctx.get("product_context"):
        lines.append(f"  Product context: {bctx['product_context']}")
    if bctx.get("notes"):
        lines.append(f"  Business notes: {bctx['notes']}")
    # Custom events give intent context
    custom_events = bctx.get("custom_events") or []
    if custom_events:
        ce_names = [ce.get("name", "") for ce in custom_events if ce.get("name")][:5]
        if ce_names:
            lines.append(f"  Key business events tracked: {', '.join(ce_names)}")

    # Structured business events uploaded by the user (PRDs, campaigns, incidents).
    # Use these to anchor narratives to real product changes and explain metric moves
    # in terms of what actually happened in the business. Per-metric impacts let the
    # narrative distinguish "this feature was expected to lift DAU but hurt fraud_rate".
    business_events = bctx.get("business_events") or []
    if business_events:
        lines.append("  Uploaded business context:")
        for evt in business_events[-8:]:
            etype     = evt.get("type", "general")
            title     = evt.get("title", "")
            date_from = evt.get("date_from", "")
            date_to   = evt.get("date_to", "")
            summary   = evt.get("summary", "")

            date_part = f" ({date_from}" + (f"–{date_to}" if date_to else "") + ")" if date_from else ""
            lines.append(f"    [{etype}] {title}{date_part}")

            # Per-metric impacts — the story can reference exact expected directions per metric
            metric_impacts = evt.get("metric_impacts") or []
            if metric_impacts:
                parts = []
                for m in metric_impacts[:5]:
                    s = m.get("metric", "")
                    d = m.get("direction", "")
                    mag = m.get("magnitude") or ""
                    if d:
                        s += f" {d}"
                    if mag:
                        s += f" {mag}"
                    parts.append(s)
                lines.append(f"      Metric impacts: {' | '.join(parts)}")

            # Type-specific metadata for richer narrative framing
            meta = evt.get("type_metadata") or {}
            if etype == "feature_launch":
                pct = meta.get("rollout_pct")
                is_exp = meta.get("is_experiment")
                if pct is not None:
                    lines.append(f"      Rollout: {pct}%" +
                                 (" (experiment)" if is_exp else " (full launch)"))
                if meta.get("success_criteria"):
                    lines.append(f"      Success criteria: {'; '.join(meta['success_criteria'][:2])}")
            elif etype == "incident":
                if meta.get("root_cause"):
                    lines.append(f"      Root cause: {meta['root_cause']}")
                recoverable = meta.get("is_data_recoverable")
                if recoverable is not None:
                    lines.append(f"      Data recoverable: {recoverable}")
            elif etype == "campaign":
                if meta.get("channels"):
                    lines.append(f"      Channels: {', '.join(meta['channels'][:3])}")
                if meta.get("expected_lift"):
                    lines.append(f"      Expected lift: {meta['expected_lift']}")

            if summary:
                lines.append(f"      {summary[:180]}")

    if not lines:
        return ""
    return "BUSINESS CONTEXT (use to distinguish seasonality from anomalies, tailor actions):\n" + "\n".join(lines)


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """You are a senior analyst and storyteller. Transform investigation results
into a structured narrative arc following "Storytelling with Data" principles.

NARRATIVE ARC — three phases, in order:
  CONTEXT     → What is the baseline? What was expected?
  TENSION     → What changed? How bad? Which hypothesis does the data support?
  RESOLUTION  → Root cause (if visible). Business impact. One concrete action.

RULES:
- Headlines state the insight, not the chart type ("iOS conversion dropped 4pp" not "Platform breakdown chart")
- Every claim must cite exact numbers from the investigation results
- No hedging: never use "appears to", "seems like", "may suggest", "it seems"
- USE THE CORRECT ANALYTICAL VOCABULARY for the event stage (see EVENT CONTEXT below).
  Misusing "retention" for an acquisition event, or "acquisition" for an engagement
  event is factually wrong — the forbidden terms list is mandatory.
- SESSION THREAD COHORT: When SESSION NARRATIVE THREAD includes "Cohort / metric context"
  from prior turns, keep vocabulary aligned with that cohort (e.g. transacting users vs a
  generic "active user") unless the user explicitly pivots to a different population.
- Confidence language must match the validation grade:
    A/B grade → definitive ("DAU dropped 12%")
    C grade   → qualified ("Data suggests DAU dropped, though one check raised a warning")
    D grade   → explicit caveat ("These findings should be verified before acting")
- Recommendations are proportional to confidence — don't recommend drastic action on C/D grade findings
- Do NOT include dollar amounts or revenue projections in the narrative — focus on user impact and directional guidance
- DATA AVAILABILITY: If the validation block contains a "data_gap" error (current period = 0 users),
  the narrative MUST begin with: "No events were found in the queried time window. This is a data
  availability issue — your database has no records for this period. Verify your data pipeline before
  drawing any conclusions." Do NOT use words like 'drop', 'collapse', 'decline', or 'fell' to describe
  a zero-data result — it is not a real metric change.

NEXT STEPS — must be PRESCRIPTIVE ACTIONS, not just follow-up questions:
- Each next step should be an action a PM or analyst can take TODAY
- Use action verbs: "Investigate X by doing Y", "Increase spend on Z where conversion improved",
  "Fix the drop-off at step N by auditing the UX flow", "Run an A/B test on X for segment Y"
- At least one step should be an investigation action, one should be a product/business action
- Do NOT generate steps like "Show me DAU" or "What is retention?" — those are lazy queries, not actions

{event_context_block}

{business_context_block}

{narrative_thread_block}

{hypothesis_block}

{validation_block}

Respond ONLY with JSON:
{{
  "executive_summary": "<3–5 sentences: question answered + key number + root cause/driver + recommended action>",
  "beats": [
    {{
      "phase": "context",
      "headline": "<insight statement>",
      "evidence": "<exact numbers>",
      "implication": "<so what?>"
    }},
    {{
      "phase": "tension",
      "headline": "...",
      "evidence": "...",
      "implication": "..."
    }},
    {{
      "phase": "resolution",
      "headline": "...",
      "evidence": "...",
      "implication": "..."
    }}
  ],
  "full_narrative": "<flowing prose, 5–8 sentences, exact numbers, no hedging, ends with a concrete prescriptive action>",
  "next_steps": [
    "<prescriptive action: Investigate X by doing Y>",
    "<prescriptive action: Fix / Improve / Test Z>",
    "<prescriptive action: Focus on segment W where the data shows opportunity>"
  ],
  "hypothesis_verdict": "<which hypothesis (product_change/technical_issue/external_factor/mix_shift) the data most strongly supports, or 'inconclusive'>"
}}"""


# ── Main entry point ──────────────────────────────────────────────────────────

@track(name="story_architect", tags=["agents"], capture_input=False, capture_output=False)
def build_story_arc(
    question: str,
    qo: QueryObject,
    plan: list,
    hypothesis_doc=None,
    validation=None,
    catalog: Optional[dict] = None,
    openai_api_key: Optional[str] = None,
    narrative_thread: Optional[str] = None,
    stream_callback: Optional[Callable[[str], None]] = None,
) -> StoryArc:
    """
    Build a Context → Tension → Resolution narrative arc from investigation results.

    Replaces analyst.synthesize() — same call site, richer output.
    Accepts optional hypothesis_doc and validation objects from
    the other specialist agents; degrades gracefully when any are None.

    stream_callback: if provided, called with each text token as it arrives from
    the LLM. Useful for Streamlit — pass a function that calls
    ``st.empty().markdown(accumulated_text)`` to show live progress.
    """
    client = make_llm_client(openai_api_key)

    investigations_text = _summarise_investigations(plan)

    system = _SYSTEM.format(
        event_context_block=_event_context_block(qo, catalog),
        business_context_block=_business_context_block(catalog),
        narrative_thread_block=_narrative_thread_block(narrative_thread),
        hypothesis_block=_hypothesis_block(hypothesis_doc),
        validation_block=_validation_block(validation),
    )

    # Granularity framing so the LLM interprets per-period numbers correctly
    gran = qo.time_granularity or "day"
    if qo.analysis_type == "behavioral_cohort":
        framing = (
            "FRAME: **Behavioral anti-cohort** — each period counts users who did the primary event "
            "but **never** the second event in the window (bucketed by first primary-event time). "
            "[COHORT_COUNT] = non-converters over time. [NON_CONVERTER_PROFILE] = platform mix of **non-converters**. "
            "[CONVERTER_PLATFORM_TREND] (if present) = MoM platform **volume for users who did the second event** — "
            "it is **not** the non-converter cohort; never describe it as 'non-converters transacting more'."
        )
    elif qo.analysis_type == "retention":
        win = int(getattr(qo, "retention_window_days", None) or 7)
        inv_names = [getattr(i, "name", "") for i in (plan or [])]
        gran = (getattr(qo, "time_granularity", None) or "day").lower()
        bd = str(getattr(qo, "breakdown", None) or "").strip()
        if bd and gran == "month":
            framing = (
                f"FRAME: **Month-over-month {win}-day retention by {bd.replace('_', ' ')}** — "
                f"each row is cohort month × {bd}; **retention_pct** is the share who returned "
                f"in the **first {win} days** (dimension taken from each user's first event). "
                f"Do NOT describe user counts as retention — only **retention_pct**. "
                f"**Recent cohort months may be immature** — do not call low rates a crisis "
                f"without noting the {win}-day window has not finished."
            )
        elif gran == "month":
            framing = (
                f"FRAME: **Month-over-month {win}-day retention** — each row is one cohort month; "
                f"**retention_pct** = share who returned in the **first {win} days** after that "
                f"month's cohort entry. This is NOT an m1/m2/m3 period matrix. "
                f"**Recent calendar months may be immature** (the {win}-day window has not "
                f"finished) — never describe low or 0% rates there as a product crisis without "
                f"saying the cohort is still maturing."
            )
        else:
            framing = (
                f"FRAME: **{win}-day retention (first {win} days)** — retention_pct is the share "
                f"of cohort users who returned at least once in the **first {win} days** after "
                f"cohort entry (not days {win}–{win * 2}). Do NOT mention other windows "
                f"unless retention_survival_curve appears in the investigation list below."
            )
        if "retention_survival_curve" not in inv_names:
            framing += " Survival/multi-window data was not run — never reference other day windows."
    elif gran == "month":
        framing = "FRAME: month-over-month trend — describe direction month by month using per-month figures only."
    elif gran == "week":
        framing = "FRAME: week-over-week trend — describe week-by-week direction, do not quote cumulative totals."
    else:
        framing = "FRAME: daily trend — use period comparison for net direction, trend for daily patterns."

    if getattr(qo, "date_from", None) and getattr(qo, "date_to", None):
        time_window_line = (
            f"Explicit date window: {qo.date_from} → {qo.date_to} "
            f"(end date is exclusive where the compiler uses half-open bounds). "
            f"Also rolling context: ~{qo.time_range_days} days when relevant."
        )
    else:
        time_window_line = f"Time window: last {qo.time_range_days} days"

    prompt = f"""{framing}

User question: "{question}"
Analysis type: {qo.analysis_type}
Primary metric/event: {qo.event or qo.metric_id or 'N/A'}
Filters: {qo.filters or 'none'}
{time_window_line}
If the user named multiple months or periods to compare, cite each period with the same metric definition.

=== INVESTIGATION RESULTS ===
{investigations_text}

Build the narrative arc now."""

    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ]

    try:
        if stream_callback is not None:
            # Stream tokens to the callback so the UI can show live progress.
            # Accumulate the full JSON response, then parse once complete.
            stream = client.chat.completions.create(
                model=LLM_STRONG,
                temperature=0.3,
                response_format={"type": "json_object"},
                messages=messages,
                stream=True,
            )
            raw = ""
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    raw += delta
                    stream_callback(delta)
            data = _parse_llm_json_object(raw)
        else:
            resp = call_llm(
                client,
                call_site="story_architect.build_story_arc",
                model=LLM_STRONG,
                temperature=0.3,
                response_format={"type": "json_object"},
                messages=messages,
            )
            content = (resp.choices[0].message.content or "").strip()
            data = _parse_llm_json_object(content)
    except Exception as exc:
        from core.infra.logger import log_llm_call as _log
        _log(call_site="story_architect.build_story_arc", model=LLM_STRONG, error=str(exc), latency_ms=0)
        return StoryArc(
            executive_summary=STORY_ARC_FAILURE_SUMMARY,
            full_narrative="",
            confidence_label=getattr(validation, "confidence_label", "") if validation else "",
        )

    beats = [
        NarrativeBeat(
            phase=b.get("phase", ""),
            headline=b.get("headline", ""),
            evidence=b.get("evidence", ""),
            implication=b.get("implication", ""),
        )
        for b in data.get("beats", [])
        if isinstance(b, dict)
    ]

    next_steps = data.get("next_steps", [])
    if isinstance(next_steps, list):
        next_steps = [str(s) for s in next_steps[:3]]
    else:
        next_steps = []

    return StoryArc(
        executive_summary=data.get("executive_summary", ""),
        beats=beats,
        full_narrative=data.get("full_narrative", ""),
        next_steps=next_steps,
        hypothesis_verdict=data.get("hypothesis_verdict", "inconclusive"),
        confidence_label=getattr(validation, "confidence_label", "") if validation else "",
    )


# ── Deep Analysis synthesis ───────────────────────────────────────────────────

_DEEP_SYSTEM = """You are a senior analyst synthesizing findings from multiple
parallel data investigations into a single cohesive report.

The investigations were run to answer the main question: "{question}"

Here are the findings from each investigation:
{findings_block}

{business_context_block}

{narrative_thread_block}

RULES FOR THIS REPORT:
- If a SESSION NARRATIVE THREAD block is present, anchor definitions and vocabulary to the
  user's recent metric/cohort (custom_event, metric_name, primary_event) unless the current
  question clearly switches topics. Do not invent a generic "active user = ≥1 event" assumption
  when the thread shows a different cohort (e.g. transacting users).
- If the user asked to compare specific periods (e.g. Feb vs Mar vs Apr), the executive summary
  must reference each named period with parallel metrics — do not omit a requested month.
- Each "sections" entry must have a **unique** "title". Never duplicate the same sub-question
  twice; merge redundant angles into one section.

STRICT DATA RULES — MANDATORY:
- Investigations labelled "⚠ DATA GAP" or "✗ FAILED" must NOT appear in "sections".
  Move them to "assumptions" as: "Investigation N returned no data — [sub-question] could not be answered."
  Never draw a conclusion, state a finding, or quote a number from a DATA GAP result.
- Only investigations labelled "✓ SUCCESS" may appear in "sections".

CONTRADICTION RULE — MANDATORY:
- Before writing the executive_summary, check whether any two SUCCESS findings conflict
  (e.g. MAU growing vs new users declining). If they do, the executive_summary MUST
  explicitly reconcile them: explain what both can be true simultaneously (e.g. "MAU grew
  from reactivation of existing users, not new acquisition, since onboarding completions fell").
  Never present contradictory findings side-by-side without explaining the reconciliation.

- "assumptions" must reflect what was actually queried (time window, cohort) — not placeholder text.

Respond ONLY with JSON:
{{
  "headline": "<single quantified finding — the #1 takeaway, e.g. 'Checkout conversion dropped 18% in 30 days, concentrated on Android'>",
  "executive_summary": "<2–3 sentences: main finding + the most important driver/root cause + recommended action>",
  "sections": [
    {{
      "title": "<sub-question that was investigated>",
      "finding": "<key insight from this investigation — state a fact, not 'the chart shows'>",
      "key_number": "<the single most important number, rate, or change from this investigation>",
      "rationale": "<why this angle mattered for answering the main question>"
    }}
  ],
  "recommendations": [
    "<specific action: Investigate X by doing Y>",
    "<specific action: Fix / Improve Z>",
    "<specific action: Focus on segment W where data shows opportunity>"
  ],
  "assumptions": [
    "<time window used, e.g. Last 30 days unless stated otherwise>",
    "<definition: active user = user with ≥1 event in the window>",
    "<any data caveat>"
  ],
  "hypothesis_verdict": "product_change|technical_issue|external_factor|mix_shift|inconclusive"
}}"""


def _format_findings(sub_results) -> str:
    """Format sub-query results into a text block for the synthesis prompt.

    Results with no data rows are labelled DATA GAP — the LLM must not draw
    conclusions from them.  Results with errors are labelled FAILED.
    """
    blocks = []
    for i, r in enumerate(sub_results, 1):
        if r.skipped:
            continue

        has_data = r.ok and not r.df.empty
        if r.error:
            status = "✗ FAILED"
        elif not has_data:
            status = "⚠ DATA GAP (query ran but returned 0 rows — treat as missing data, draw NO conclusions)"
        else:
            status = "✓ SUCCESS"

        preview = ""
        err_line = ""
        if has_data:
            preview = "\nData preview:\n" + r.df.head(6).to_string(index=False, max_cols=6)
        elif r.error:
            err_line = f"\n    Error detail: {r.error}"

        finding_line = ""
        if has_data:
            finding_line = f"\n    Key finding: {r.key_finding or r.narrative or '(see data above)'}"

        blocks.append(
            f"[{i}] {r.sub_question}\n"
            f"    Status: {status}"
            f"{finding_line}{err_line}{preview}"
        )
    return "\n\n".join(blocks) if blocks else "(no investigation results)"


def _dedupe_deep_sections(sections: list[DeepSection]) -> list[DeepSection]:
    seen: set[str] = set()
    out: list[DeepSection] = []
    for s in sections:
        key = re.sub(r"[^a-z0-9]+", " ", (s.title or "").lower()).strip()
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(s)
    return out


@track(name="deep_story_architect", tags=["agents"], capture_input=False, capture_output=False)
def build_deep_story_arc(
    question: str,
    sub_results,          # list[SubQueryResult] from deep_analysis.run_parallel_investigations
    catalog: Optional[dict] = None,
    openai_api_key: Optional[str] = None,
    narrative_thread: Optional[str] = None,
) -> "DeepStoryArc":
    """
    Synthesize N sub-query results into a single structured DeepStoryArc report.
    Called after run_parallel_investigations() completes.
    Fails gracefully — always returns a DeepStoryArc.
    """
    client = make_llm_client(openai_api_key)

    findings_block = _format_findings(sub_results)
    biz_block      = _business_context_block(catalog)
    thread_block   = _narrative_thread_block(narrative_thread)

    system = _DEEP_SYSTEM.format(
        question=question,
        findings_block=findings_block,
        business_context_block=biz_block,
        narrative_thread_block=thread_block or "(no prior session thread)",
    )

    try:
        resp = client.chat.completions.create(
            model=LLM_STRONG,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": f"Synthesize the investigation findings for: {question}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception:
        # Graceful fallback — assemble from raw findings
        summaries = [r.key_finding or r.narrative for r in sub_results if r.ok and (r.key_finding or r.narrative)]
        return DeepStoryArc(
            headline=f"Analysis of: {question}",
            executive_summary=" ".join(summaries[:2]) if summaries else "Analysis complete.",
            sections=_dedupe_deep_sections([
                DeepSection(
                    title=r.sub_question,
                    finding=r.key_finding or r.narrative or "(no finding)",
                    key_number="",
                    rationale=r.rationale,
                )
                for r in sub_results if r.ok
            ]),
        )

    sections = _dedupe_deep_sections([
        DeepSection(
            title=s.get("title", ""),
            finding=s.get("finding", ""),
            key_number=s.get("key_number", ""),
            rationale=s.get("rationale", ""),
        )
        for s in data.get("sections", [])
        if isinstance(s, dict)
    ])

    return DeepStoryArc(
        headline=data.get("headline", ""),
        executive_summary=data.get("executive_summary", ""),
        sections=sections,
        recommendations=[str(r) for r in data.get("recommendations", [])[:4]],
        assumptions=[str(a) for a in data.get("assumptions", [])[:4]],
        hypothesis_verdict=data.get("hypothesis_verdict", "inconclusive"),
    )
