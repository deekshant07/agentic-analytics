"""
context_resolver.py — Session memory + query rewriting before orchestration.

Flow:
  1. Load last N turns from chat_history for the given session_id.
  2. Detect whether the incoming question is contextual (references prior state).
  3. If contextual, rewrite it into a self-contained question via LLM.
  4. Return ResolvedQuery with the (possibly rewritten) question + loaded history.

Why:
  The orchestrator receives history as QO dicts, which helps for follow-up slot
  inheritance. But it does not do explicit query rewriting. Putting rewriting here
  keeps the orchestrator prompt clean and makes the logic independently testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from core.infra.llm import make_llm_client, LLM_FAST
from core.infra.logger import log_llm_call
from core.infra.tracer import track

# ── Contextual signal patterns ────────────────────────────────────────────────
# Questions that clearly reference a prior answer and need rewriting.
_CONTEXTUAL_RE = re.compile(
    r"\b("
    r"same|that|this|those|it|them|the same|the metric|the event"
    r"|what about|how about|what if|filter (?:to|by)|breakdown by|split by"
    r"|also|additionally|and (?:for|in|by)|but for|except for"
    r"|instead of|compared to|versus|vs"
    r"|can you|could you|show me|give me"
    r"|now (?:for|show|filter|break)"
    r"|previous|prior|last time|earlier"
    r")\b",
    re.IGNORECASE,
)

# Short questions (<6 words) are almost always follow-ups ("and by city?", "what about iOS?")
_SHORT_FOLLOWUP_RE = re.compile(r"^(\w+\b\s+){0,5}\w+\??$")


@dataclass
class ResolvedQuery:
    question: str                        # final question sent to orchestrator (possibly rewritten)
    original_question: str               # what the user actually typed
    was_rewritten: bool                  # True if rewriting changed the question
    rewrite_reason: str                  # short explanation logged for tracing
    session_id: str
    history: list[dict] = field(default_factory=list)    # get_qo_history() output
    narrative_thread: str = ""                            # get_narrative_thread() output


def _is_contextual(question: str, has_history: bool) -> bool:
    """Heuristic: does this question likely reference a prior turn?"""
    if not has_history:
        return False
    q = question.strip()
    # Very short question — almost certainly a follow-up
    word_count = len(q.split())
    if word_count <= 5:
        return True
    # Contains explicit contextual signal word/phrase
    if _CONTEXTUAL_RE.search(q):
        return True
    return False


_REWRITE_SYSTEM = """\
You are a query contextualizer for an analytics assistant.
You will be given a conversation history and a new user question.
If the question references something from the prior conversation \
(e.g. "show me that", "same for iOS", "what about last week"), \
rewrite it into a fully self-contained question that can be answered \
without any prior context.

Rules:
- Only rewrite if the question genuinely requires prior context.
- If the question is already self-contained, return it verbatim.
- Keep the rewritten question concise and natural (1–2 sentences max).
- Do NOT add interpretation or explanation — just the rewritten question.
- Output ONLY the rewritten question, nothing else.
"""


def _build_history_block(history: list[dict]) -> str:
    """Format the last N turns into a readable block for the rewriter prompt."""
    lines = []
    for i, turn in enumerate(history, 1):
        q = turn.get("question", "")
        qo = turn.get("qo") or {}
        summary = (turn.get("memory") or {}).get("summary", "")
        metric = turn.get("metric_name") or qo.get("metric_id") or qo.get("event") or ""
        at = qo.get("analysis_type", "")
        bd = qo.get("breakdown", "")
        parts = [f"Turn {i}: Q: \"{q}\""]
        if metric:
            parts.append(f"metric/event={metric}")
        if at:
            parts.append(f"type={at}")
        if bd:
            parts.append(f"breakdown={bd}")
        if summary:
            parts.append(f"result: {summary[:120]}")
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)


@track(name="context_resolver", tags=["pipeline"])
def rewrite_if_contextual(
    question: str,
    history: list[dict],
    openai_api_key: Optional[str] = None,
) -> tuple[str, bool, str]:
    """
    Returns (final_question, was_rewritten, reason).
    Calls LLM_FAST only when the question looks contextual.
    """
    if not _is_contextual(question, bool(history)):
        return question, False, "standalone"

    history_block = _build_history_block(history)
    user_prompt = (
        f"Conversation history:\n{history_block}\n\n"
        f"New question: \"{question}\"\n\n"
        f"Rewritten question:"
    )

    import time
    t0 = time.perf_counter()
    try:
        client = make_llm_client(openai_api_key)
        resp = client.chat.completions.create(
            model=LLM_FAST,
            messages=[
                {"role": "system", "content": _REWRITE_SYSTEM},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.0,
        )
        rewritten = resp.choices[0].message.content.strip().strip('"')
        latency = (time.perf_counter() - t0) * 1000
        usage = resp.usage
        log_llm_call(
            call_site="context_resolver",
            model=LLM_FAST,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency,
        )
        was_rewritten = rewritten.lower() != question.lower().strip()
        reason = "contextual_rewrite" if was_rewritten else "no_change_needed"
        return rewritten if was_rewritten else question, was_rewritten, reason
    except Exception as exc:
        log_llm_call(call_site="context_resolver", model=LLM_FAST, error=str(exc), latency_ms=0)
        return question, False, f"rewrite_failed:{exc}"


def resolve(
    question: str,
    session_id: str,
    openai_api_key: Optional[str] = None,
    history_limit: int = 5,
    narrative_limit: int = 3,
) -> ResolvedQuery:
    """
    Main entry point. Load memory for session_id, optionally rewrite question,
    return a ResolvedQuery ready for api.py to pass to orchestrate().

    If session_id is empty, returns a no-op ResolvedQuery with empty history.
    """
    history: list[dict] = []
    narrative_thread: str = ""

    if session_id:
        try:
            from core.memory.chat_history import get_qo_history, get_narrative_thread
            history = get_qo_history(session_id, limit=history_limit)
            narrative_thread = get_narrative_thread(session_id, limit=narrative_limit)
        except Exception:
            pass  # DB not init'd yet or first turn

    final_question, was_rewritten, reason = rewrite_if_contextual(
        question, history, openai_api_key
    )

    return ResolvedQuery(
        question=final_question,
        original_question=question,
        was_rewritten=was_rewritten,
        rewrite_reason=reason,
        session_id=session_id,
        history=history,
        narrative_thread=narrative_thread,
    )
