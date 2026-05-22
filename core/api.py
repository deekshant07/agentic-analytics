"""
api.py — AnalyticsAPI: the single entry point for all analytics queries.

Separates pipeline logic from UI concerns (Streamlit, CLI, REST).
Both agent.py (CLI) and chat.py (Streamlit) can delegate here instead of
duplicating orchestration logic.

Usage:
    from core.api import AnalyticsAPI, AnalyticsResult

    api = AnalyticsAPI()                       # reads DB_PATH, CATALOG_PATH from defaults
    result = api.ask("Why did DAU drop?", session_id="abc")
    print(result.narrative)
    print(result.next_steps)
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import duckdb

from core.infra.logger import log_llm_call, log_turn
from core.infra.tracer import track, update_current_trace


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class AnalyticsResult:
    question: str
    analysis_type: str
    narrative: str
    executive_summary: str = ""
    next_steps: list[str] = field(default_factory=list)
    confidence: str = ""
    hypothesis_verdict: str = ""
    matched_metric: Optional[str] = None
    latency_ms: float = 0.0
    investigations: list = field(default_factory=list)
    error: Optional[str] = None
    session_id: str = ""
    original_question: Optional[str] = None   # set when query was rewritten
    was_rewritten: bool = False
    # Genie improvements
    quality_score: float = 1.0               # 0–1 from answer judge (1.0 = not judged)
    quality_flags: list[str] = field(default_factory=list)  # e.g. ["vague","off_topic"]
    retry_triggered: bool = False            # True if self-correction retry fired

    def to_dict(self) -> dict:
        d = {
            "question": self.question,
            "analysis_type": self.analysis_type,
            "narrative": self.narrative,
            "executive_summary": self.executive_summary,
            "next_steps": self.next_steps,
            "confidence": self.confidence,
            "hypothesis_verdict": self.hypothesis_verdict,
            "matched_metric": self.matched_metric,
            "latency_ms": round(self.latency_ms),
            "error": self.error,
            "session_id": self.session_id,
            "quality_score": round(self.quality_score, 2),
            "quality_flags": self.quality_flags,
            "retry_triggered": self.retry_triggered,
        }
        if self.was_rewritten:
            d["original_question"] = self.original_question
            d["was_rewritten"] = True
        return d


# ── Diagnose signals ───────────────────────────────────────────────────────────

_DIAGNOSE_SIGNALS = (
    "why", "cause", "drop", "spike", "decline", "increase",
    "decrease", "forecast", "predict", "project",
)


# ── Self-correction helpers ───────────────────────────────────────────────────

def _all_investigations_failed(report) -> bool:
    """
    True when every investigation raised a SQL error.

    Empty results (df.empty but no error) are NOT treated as failures — they may
    be valid answers ("no users did X in January"). Only actual execution errors
    indicate the orchestrator guessed the wrong query and a retry is worth trying.
    """
    invs = getattr(report, "investigations", [])
    if not invs:
        return False
    errored = [i for i in invs if bool(i.error)]
    return len(errored) > 0 and len(errored) == len(invs)


def _build_correction_notes(report) -> list[dict]:
    """Convert investigation failures into orchestrator correction hints."""
    notes = []
    for inv in getattr(report, "investigations", []):
        if inv.error:
            notes.append({"note": f"SQL for '{inv.name}' failed: {inv.error[:180]}"})
        elif inv.df.empty:
            notes.append({"note": f"Query '{inv.name}' returned no rows — try a wider time range or different event name"})
    return notes[:3]  # cap: keep prompt size bounded


# ── Multi-trajectory helpers ──────────────────────────────────────────────────

# Words that indicate the question is genuinely ambiguous or multi-dimensional,
# worth paying the cost of a second trajectory.
_COMPLEX_SIGNALS = frozenset({
    "compare", "versus", "vs", "between", "difference", "better", "worse",
    "trend", "breakdown", "split", "across", "segment", "cohort",
    "relationship", "correlation", "impact", "effect", "influence",
    "multiple", "both", "each", "per",
})


def _is_multi_trajectory_candidate(question: str) -> bool:
    """
    True for questions that are long enough AND contain vocabulary that suggests
    multiple valid interpretations — worth running a second orchestration path.
    Simple metric/segment lookups don't benefit and shouldn't pay the extra latency.
    """
    words = question.lower().split()
    if len(words) < 9:
        return False
    return any(w in _COMPLEX_SIGNALS for w in words)


def _qo_slot_count(qo) -> int:
    """
    Count how many meaningful slots are filled in a QueryObject.
    More slots = more specific interpretation = preferred trajectory.
    """
    score = 0
    for attr in ("event", "metric_id", "breakdown", "time_range_days",
                 "date_from", "date_to", "time_granularity"):
        v = getattr(qo, attr, None)
        if v is not None and str(v).strip():
            score += 1
    filters = getattr(qo, "filters", None) or {}
    score += min(3, len(filters))   # each filter adds specificity, cap at 3
    return score


def _pick_better_qo(qo_a, qo_b):
    """
    Choose the more specific QueryObject from two trajectories.
    Falls back to qo_a when equal (deterministic run is preferred).
    """
    bad_types = {"clarify", "out_of_scope", "multi_intent"}
    a_ok = getattr(qo_a, "analysis_type", "") not in bad_types
    b_ok = getattr(qo_b, "analysis_type", "") not in bad_types
    if a_ok and not b_ok:
        return qo_a
    if b_ok and not a_ok:
        return qo_b
    return qo_a if _qo_slot_count(qo_a) >= _qo_slot_count(qo_b) else qo_b


# ── AnalyticsAPI ──────────────────────────────────────────────────────────────

class AnalyticsAPI:
    """
    Single-entry-point wrapper around the core analytics pipeline.

    Encapsulates:
      pre_check → hypothesis_agent → orchestrate → diagnose / investigate → story_architect

    The class lazy-loads the catalog and sampled schema values so the first
    call pays the I/O cost; subsequent calls reuse the cached data.
    Call ``invalidate_cache()`` after a catalog rescan.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        catalog_path: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        _root = Path(__file__).parent.parent
        self.db_path = db_path or str(_root / "jupiter.duckdb")
        self.catalog_path = catalog_path or str(_root / "catalog.json")
        self.api_key = api_key  # None → make_llm_client resolves from LLM_PROVIDER
        self._catalog: Optional[dict] = None
        self._sampled: Optional[dict] = None

    # ── Cache management ─────────────────────────────────────────────────────

    def catalog(self) -> dict:
        if self._catalog is None:
            self._catalog = json.loads(Path(self.catalog_path).read_text())
        return self._catalog

    def sampled_values(self) -> dict:
        if self._sampled is None:
            self._sampled = self._load_sampled_values()
        return self._sampled

    def invalidate_cache(self) -> None:
        """Force a reload of catalog + sampled values on next call."""
        self._catalog = None
        self._sampled = None

    def _load_sampled_values(self) -> dict:
        sampled: dict = {}
        if not Path(self.db_path).exists():
            return sampled
        try:
            conn = duckdb.connect(self.db_path, read_only=True)
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).df()["table_name"].tolist()
            skip = {"user_id", "session_id", "event_id", "transaction_id", "device_id"}
            for tname in tables:
                sampled[tname] = {}
                try:
                    cols_df = conn.execute(f"DESCRIBE {tname}").df()
                    for _, row in cols_df.iterrows():
                        cname, ctype = row["column_name"], row["column_type"]
                        if ctype != "VARCHAR" or cname in skip:
                            continue
                        try:
                            vals = conn.execute(
                                f"SELECT DISTINCT {cname} FROM {tname} "
                                f"WHERE {cname} IS NOT NULL LIMIT 20"
                            ).df()[cname].tolist()
                            sampled[tname][cname] = [str(v) for v in vals if v]
                        except Exception:
                            pass
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass
        return sampled

    # ── Main entry point ─────────────────────────────────────────────────────

    @track(name="turn", tags=["pipeline"], capture_input=False, capture_output=False)
    def ask(
        self,
        question: str,
        session_id: str = "",
        history: Optional[list[dict]] = None,
        narrative_thread: Optional[str] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
        hypothesis_only_for_diagnose: bool = True,
    ) -> AnalyticsResult:
        """
        Run the full analytics pipeline for a natural-language question.

        Args:
            question: The user's question.
            session_id: Session identifier. When provided and history/narrative_thread
                are not explicitly passed, memory is auto-loaded from chat_history.db
                and contextual queries are rewritten to be self-contained.
            history: Override: explicit QO history dicts (skips auto-load).
            narrative_thread: Override: formatted prior findings (skips auto-load).
            stream_callback: If provided, called with each narrative text token
                as the LLM streams it.
            hypothesis_only_for_diagnose: When True (default), hypothesis generation
                is skipped for metric/segment questions to save latency.

        Returns:
            AnalyticsResult dataclass.
        """
        from core.pipeline.pre_check import check_out_of_scope, check_underspecified
        from core.pipeline.orchestrator import orchestrate
        from core.agents.hypothesis_agent import generate_hypotheses
        from core.analysis.analyst import investigate
        from core.pipeline.context_resolver import resolve as resolve_context

        t0 = time.perf_counter()
        key = self.api_key  # may be None — make_llm_client handles provider key resolution
        update_current_trace(question=question, session_id=session_id or "")

        # 0. Pre-check (zero-LLM regex gate — runs on original question)
        oos = check_out_of_scope(question)
        if oos:
            return AnalyticsResult(
                question=question,
                analysis_type="out_of_scope",
                narrative=oos,
                executive_summary=oos,
            )

        underspec = check_underspecified(question)
        if underspec:
            return AnalyticsResult(
                question=question,
                analysis_type="clarify",
                narrative=underspec,
                executive_summary=underspec,
            )

        catalog = self.catalog()
        sampled = self.sampled_values()

        # 0.5. Context resolution — load session memory and rewrite contextual queries.
        # Only runs if session_id is given AND the caller didn't pass history explicitly.
        resolved = None
        if session_id and history is None:
            try:
                resolved = resolve_context(
                    question=question,
                    session_id=session_id,
                    openai_api_key=key,
                )
                question        = resolved.question          # possibly rewritten
                history         = resolved.history           # loaded from DB
                narrative_thread = resolved.narrative_thread # loaded from DB
            except Exception as exc:
                log_llm_call(call_site="api.context_resolver", model="", error=str(exc), latency_ms=0)

        # 1. Hypothesis generation — runs in parallel with semantic-index pre-warm
        #    (Genie improvement #2: parallel discovery saves ~50–100 ms per diagnose turn).
        generate_hyp = not hypothesis_only_for_diagnose or any(
            s in question.lower() for s in _DIAGNOSE_SIGNALS
        )
        hypothesis_doc = None
        if generate_hyp:
            from core.semantic.semantic_index import get_or_build_index
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _pool:
                _hyp_f = _pool.submit(
                    generate_hypotheses,
                    question=question, catalog=catalog, openai_api_key=key,
                )
                _idx_f = _pool.submit(get_or_build_index, catalog)  # pre-warms BM25 cache
                try:
                    hypothesis_doc = _hyp_f.result(timeout=12)
                except Exception as exc:
                    log_llm_call(call_site="api.hypothesis_agent", model="", error=str(exc), latency_ms=0)
                try:
                    _idx_f.result(timeout=3)   # ensure cache is warm before orchestrate()
                except Exception:
                    pass
        else:
            # Non-diagnose path: still pre-warm the index in the background
            from core.semantic.semantic_index import get_or_build_index
            _bg = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            _bg.submit(get_or_build_index, catalog)
            _bg.shutdown(wait=False)

        # 2. Orchestrate — natural language → QueryObject
        # Multi-trajectory (Genie improvement #5): for complex/ambiguous questions
        # run two orchestration paths in parallel — deterministic (temp=0) and a
        # slightly varied one (temp=0.3) — then pick the more specific result.
        # Simple questions skip the second trajectory to avoid paying extra latency.
        _orch_kwargs = dict(
            question=question,
            catalog=catalog,
            sampled_values=sampled,
            openai_api_key=key,
            history=history or [],
            hypothesis_doc=hypothesis_doc,
        )
        if _is_multi_trajectory_candidate(question):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _tp:
                _f1 = _tp.submit(orchestrate, **_orch_kwargs, temperature=0.0)
                _f2 = _tp.submit(orchestrate, **_orch_kwargs, temperature=0.3)
                try:
                    _qo1 = _f1.result(timeout=40)
                    _qo2 = _f2.result(timeout=40)
                    qo = _pick_better_qo(_qo1, _qo2)
                except Exception:
                    # Fallback: use whichever future finished
                    qo = _f1.result() if not _f1.exception() else orchestrate(**_orch_kwargs)
        else:
            qo = orchestrate(**_orch_kwargs)

        if qo.analysis_type == "clarify":
            msg = qo.clarify_message or "Could you rephrase your question?"
            return AnalyticsResult(
                question=question,
                analysis_type="clarify",
                narrative=msg,
                executive_summary=msg,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        if qo.analysis_type == "out_of_scope":
            msg = qo.clarify_message or "This question is outside the scope of this analytics agent."
            return AnalyticsResult(
                question=question,
                analysis_type="out_of_scope",
                narrative=msg,
                executive_summary=msg,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        if qo.analysis_type == "multi_intent":
            msg = (
                qo.clarify_message
                or "This question asks about multiple metrics at once. "
                   "I can only analyze one metric per query — please ask about each separately."
            )
            return AnalyticsResult(
                question=question,
                analysis_type="multi_intent",
                narrative=msg,
                executive_summary=msg,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # 3. Diagnose path — structural root-cause analysis
        if qo.analysis_type == "diagnose":
            from core.analysis.diagnose import run_diagnosis
            result = run_diagnosis(
                event=qo.event or "",
                time_range_days=qo.time_range_days or 60,
                db_path=self.db_path,
                openai_api_key=key,
                period_end=qo.diagnose_period_end,
                filters=qo.filters or {},
                catalog=catalog,
                event_sampled_values=sampled.get("events", {}),
                question=question,
                hypothesis_doc=hypothesis_doc,
                narrative_thread=narrative_thread,
                narrative_qo=qo,
            )
            latency = (time.perf_counter() - t0) * 1000
            log_turn(
                session_id=session_id,
                question=question,
                analysis_type="diagnose",
                latency_ms=latency,
            )
            _orig = resolved.original_question if resolved and resolved.was_rewritten else None
            return AnalyticsResult(
                question=question,
                analysis_type="diagnose",
                matched_metric=qo.event,
                narrative=result["narrative"],
                executive_summary=result["narrative"],
                next_steps=result.get("next_steps", []),
                latency_ms=latency,
                session_id=session_id,
                original_question=_orig,
                was_rewritten=bool(_orig),
            )

        # 4. Analyst path — multi-investigation engine + story_architect
        report = investigate(
            question=question,
            qo=qo,
            db_path=self.db_path,
            openai_key=key,
            catalog=catalog,
            event_sampled_values=sampled.get("events", {}),
            user_sampled_values=sampled.get("users", {}),
            hypothesis_doc=hypothesis_doc,
            narrative_thread=narrative_thread,
            stream_callback=stream_callback,
        )

        # Self-correction (Genie improvement #1): if every SQL attempt failed, feed the
        # errors back to orchestrate() as correction hints and retry once.
        retry_triggered = False
        if _all_investigations_failed(report):
            corrections = _build_correction_notes(report)
            if corrections:
                try:
                    qo_v2 = orchestrate(
                        question=question,
                        catalog=catalog,
                        sampled_values=sampled,
                        openai_api_key=key,
                        history=history or [],
                        hypothesis_doc=hypothesis_doc,
                        corrections=corrections,
                    )
                    if qo_v2.analysis_type not in ("clarify", "out_of_scope", "multi_intent"):
                        retry_report = investigate(
                            question=question,
                            qo=qo_v2,
                            db_path=self.db_path,
                            openai_key=key,
                            catalog=catalog,
                            event_sampled_values=sampled.get("events", {}),
                            user_sampled_values=sampled.get("users", {}),
                            hypothesis_doc=hypothesis_doc,
                            narrative_thread=narrative_thread,
                            stream_callback=stream_callback,
                        )
                        retry_ok = sum(1 for i in retry_report.investigations if not i.error and not i.df.empty)
                        orig_ok  = sum(1 for i in report.investigations      if not i.error and not i.df.empty)
                        if retry_ok > orig_ok:
                            report = retry_report
                            qo     = qo_v2
                            retry_triggered = True
                except Exception:
                    pass  # self-correction failure must never surface to the user

        # Answer judge (Genie improvement #3): lightweight quality check.
        # Only fires when story_architect is not self-confident, so high-confidence
        # answers pay zero extra latency.
        quality_score: float = 1.0
        quality_flags: list[str] = []
        if report.confidence_label and report.confidence_label.lower() not in ("high", ""):
            from core.agents.answer_judge import judge_answer
            try:
                jr = judge_answer(
                    question=question,
                    narrative=report.narrative,
                    analysis_type=report.analysis_type,
                    openai_api_key=key,
                )
                if not jr.skipped:
                    quality_score = jr.score
                    quality_flags = jr.flags
            except Exception:
                pass

        latency   = (time.perf_counter() - t0) * 1000
        sql_count = sum(
            1 for inv in report.investigations
            if not inv.error and not inv.df.empty
        )
        log_turn(
            session_id=session_id,
            question=question,
            analysis_type=report.analysis_type,
            latency_ms=latency,
            sql_count=sql_count,
        )

        _orig = resolved.original_question if resolved and resolved.was_rewritten else None
        return AnalyticsResult(
            question=question,
            analysis_type=report.analysis_type,
            matched_metric=qo.metric_id or qo.event,
            narrative=report.narrative,
            executive_summary=report.executive_summary,
            next_steps=report.next_steps,
            confidence=report.confidence_label,
            hypothesis_verdict=report.hypothesis_verdict,
            latency_ms=latency,
            investigations=report.investigations,
            session_id=session_id,
            original_question=_orig,
            was_rewritten=bool(_orig),
            quality_score=quality_score,
            quality_flags=quality_flags,
            retry_triggered=retry_triggered,
        )

    # ── Tool-use path ─────────────────────────────────────────────────────────

    def ask_with_tools(self, question: str) -> str:
        """
        Alternative path: run the question through the ToolAgent (function-calling loop)
        instead of the deterministic pipeline. Better for ad-hoc lookups, benchmark
        comparisons, and questions that need multi-step data gathering.

        Returns the final text answer.
        """
        from core.infra.tools import ToolAgent
        agent = ToolAgent(
            db_path=self.db_path,
            catalog=self.catalog(),
            api_key=self.api_key,
        )
        return agent.run(question)
