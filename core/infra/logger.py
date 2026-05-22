"""
logger.py — Structured JSON logging for the analytics agent.

Emits newline-delimited JSON to stderr (redirectable to a file).
Zero external dependencies — uses stdlib logging only.

Quick start:
    from core.infra.logger import log_turn, log_llm_call, log_sql, setup_logging
    setup_logging(log_file="agent.log")   # optional; stderr by default
    log_turn(session_id="abc", question="...", analysis_type="metric", latency_ms=1234)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

_logger = logging.getLogger("analytics_agent")
_initialized = False


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Wire up JSON-line logging. Call once at startup.
    Idempotent — safe to call multiple times.
    """
    global _initialized
    if _initialized:
        return
    fmt = logging.Formatter("%(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        _logger.addHandler(h)
    _logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    _initialized = True


def _emit(event: str, **fields) -> None:
    if not _initialized:
        setup_logging()
    record = {
        "ts": round(time.time(), 3),
        "event": event,
        **{k: v for k, v in fields.items() if v is not None},
    }
    _logger.info(json.dumps(record, default=str))


# ── Public log functions ───────────────────────────────────────────────────────

def log_turn(
    *,
    session_id: str,
    question: str,
    analysis_type: str,
    latency_ms: float,
    llm_calls: int = 0,
    sql_count: int = 0,
    error: Optional[str] = None,
) -> None:
    """Log one complete Q&A turn."""
    _emit(
        "turn",
        session_id=session_id,
        question=question[:120],
        analysis_type=analysis_type,
        latency_ms=round(latency_ms),
        llm_calls=llm_calls or None,
        sql_count=sql_count or None,
        error=error,
    )


def log_llm_call(
    *,
    call_site: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: float = 0.0,
    error: Optional[str] = None,
) -> None:
    """Log one LLM API call with token counts and latency."""
    _emit(
        "llm_call",
        call_site=call_site,
        model=model,
        prompt_tokens=prompt_tokens or None,
        completion_tokens=completion_tokens or None,
        total_tokens=(prompt_tokens + completion_tokens) or None,
        latency_ms=round(latency_ms),
        error=error,
    )


def log_sql(
    *,
    investigation_name: str,
    latency_ms: float,
    row_count: int = 0,
    error: Optional[str] = None,
    timed_out: bool = False,
) -> None:
    """Log one SQL investigation execution."""
    _emit(
        "sql",
        investigation_name=investigation_name,
        latency_ms=round(latency_ms),
        row_count=row_count or None,
        error=error,
        timed_out=True if timed_out else None,
    )


def log_schema_drift(*, removed: list[str], added: list[str]) -> None:
    """Log columns that appeared or disappeared since the catalog was last scanned."""
    if removed or added:
        _emit("schema_drift", removed=removed or None, added=added or None)


def log_tool_call(
    *,
    tool_name: str,
    latency_ms: float,
    success: bool = True,
    error: Optional[str] = None,
) -> None:
    """Log one tool invocation from the tool-use agent."""
    _emit(
        "tool_call",
        tool_name=tool_name,
        latency_ms=round(latency_ms),
        success=success,
        error=error,
    )
