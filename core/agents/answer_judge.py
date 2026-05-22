"""
answer_judge.py — Lightweight answer quality scorer (Genie improvement #3).

Runs a single LLM_FAST call after story_architect to catch:
  - Narrative that doesn't address the question ("off_topic")
  - Answer admits it couldn't find data ("insufficient_data")
  - Conclusion contradicts the data described ("contradictory")
  - Vague or generic response with no specific numbers ("vague")

Only fires when story_architect's self-reported confidence_label is not "high",
so confident answers pay zero extra latency.

Returns JudgeResult with a 0–1 score and flag list. The score is appended to
AnalyticsResult so the UI can show a quality indicator or re-prompt the user.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

from core.infra.llm import make_llm_client, LLM_FAST
from core.infra.logger import log_llm_call
from core.infra.tracer import track


@dataclass
class JudgeResult:
    score: float                      # 0.0 (bad) → 1.0 (good)
    flags: list[str] = field(default_factory=list)  # e.g. ["off_topic", "vague"]
    skipped: bool = False             # True when judge was bypassed to save latency
    latency_ms: float = 0.0


_JUDGE_SYSTEM = """\
You are a strict QA judge for an analytics AI assistant.
Given a user question and the assistant's narrative answer, score the answer quality.

Scoring rules:
- 10: Directly answers the question with specific numbers or trend descriptions.
- 7–9: Mostly answers but missing some detail or slightly off focus.
- 4–6: Partial answer, vague, or lacks supporting data.
- 1–3: Mostly wrong focus, admits failure, or contradicts itself.
- 0: Completely off-topic, empty, or error message.

Output ONLY valid JSON — no markdown, no explanation:
{"score": <0-10>, "flags": [<zero or more of: "off_topic","insufficient_data","contradictory","vague","ok">]}
"""


@track(name="answer_judge", tags=["agents"])
def judge_answer(
    question: str,
    narrative: str,
    analysis_type: str,
    openai_api_key: Optional[str] = None,
) -> JudgeResult:
    """
    Score narrative quality for the given question.

    Callers should check confidence_label before calling:
        if report.confidence_label != "high":
            judge = judge_answer(...)
    """
    if not narrative or not narrative.strip():
        return JudgeResult(score=0.0, flags=["insufficient_data"])

    # Truncate long narratives — judge only needs the gist
    snippet = narrative[:600].strip()
    user_msg = (
        f'Analysis type: {analysis_type}\n'
        f'Question: "{question}"\n'
        f'Answer (first 600 chars): "{snippet}"'
    )

    t0 = time.perf_counter()
    try:
        client = make_llm_client(openai_api_key)
        resp = client.chat.completions.create(
            model=LLM_FAST,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=60,
        )
        raw = resp.choices[0].message.content.strip()
        usage = resp.usage
        latency = (time.perf_counter() - t0) * 1000
        log_llm_call(
            call_site="answer_judge",
            model=LLM_FAST,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency,
        )
        parsed = json.loads(raw)
        raw_score = float(parsed.get("score", 5))
        flags = [str(f) for f in parsed.get("flags", [])]
        # Normalize 0–10 → 0.0–1.0
        score = max(0.0, min(1.0, raw_score / 10.0))
        return JudgeResult(score=score, flags=flags, latency_ms=latency)
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        log_llm_call(
            call_site="answer_judge", model=LLM_FAST,
            error=str(exc), latency_ms=latency,
        )
        # On any failure, return neutral score so the pipeline isn't blocked
        return JudgeResult(score=0.7, flags=[], latency_ms=latency)
