"""
tracer.py — Optional LLM observability via Opik.

Set env vars in .env to activate:
    OPIK_API_KEY=<key>                  → Opik cloud (app.comet.com/opik)
    OPIK_WORKSPACE=<workspace>          → Opik cloud workspace (required with API key)
    OPIK_PROJECT_NAME=analytics-agent  → Project label in the Opik UI

Self-hosted (Docker):
    OPIK_URL=http://localhost:5173
    OPIK_PROJECT_NAME=analytics-agent

Install:
    uv add opik

When none of the above env vars are set every function in this module is a
no-op — zero overhead in the hot path.

Trace shape produced per user turn:

    Trace: ask("Why did DAU drop?")
      Span: context_resolver [llm.context_resolver child]
      Span: hypothesis_agent [llm.hypothesis_agent child]
      Span: orchestrator     [llm.orchestrator child]
      Span: diagnose / investigate / story_architect [llm.* children]
"""
from __future__ import annotations

import os
from typing import Callable

_enabled = False
_opik_mod = None


def _init() -> None:
    global _enabled, _opik_mod
    api_key = os.getenv("OPIK_API_KEY", "").strip()
    url     = os.getenv("OPIK_URL", "").strip()
    if not api_key and not url:
        return
    try:
        import opik
        project = os.getenv("OPIK_PROJECT_NAME", "analytics-agent")
        if url:
            opik.configure(url=url, use_local=True, force=True)
        else:
            ws = os.getenv("OPIK_WORKSPACE", "").strip()
            opik.configure(api_key=api_key, workspace=ws or None, force=True)
        _opik_mod = opik
        _enabled = True
    except Exception:
        pass  # opik not installed or misconfigured — silently degrade


_init()


# ── Decorator ─────────────────────────────────────────────────────────────────

def track(
    name: str = "",
    tags: list[str] | None = None,
    capture_input: bool = True,
    capture_output: bool = True,
) -> Callable:
    """
    Wrap a function as an Opik span.

    Nested @track calls automatically form parent → child spans within the same
    trace. The outermost call in a request creates the root trace; every inner
    call creates a child span.

    No-op when Opik is not configured.

    Usage:
        @track(name="orchestrator", tags=["pipeline"])
        def orchestrate(question, catalog, ...):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        if not _enabled or _opik_mod is None:
            return fn
        project = os.getenv("OPIK_PROJECT_NAME", "analytics-agent")
        return _opik_mod.track(
            name=name or fn.__name__,
            tags=tags or [],
            capture_input=capture_input,
            capture_output=capture_output,
            project_name=project,
        )(fn)
    return decorator


# ── LLM span helper ───────────────────────────────────────────────────────────

def record_llm_call(
    *,
    call_site: str,
    model: str,
    messages: list[dict],
    completion: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
) -> None:
    """
    Create a named child span for one LLM call inside the current @track context.

    Called from call_llm() in llm.py after every successful completion so that
    the full prompt → completion is captured with token counts.

    No-op when Opik is not configured or no active trace context exists.
    """
    if not _enabled or _opik_mod is None:
        return
    try:
        from opik import opik_context, Opik
        ctx = opik_context.get_current_span_data()
        if ctx is None:
            return

        project = os.getenv("OPIK_PROJECT_NAME", "analytics-agent")
        client = Opik(project_name=project)
        span = client.span(
            trace_id=ctx.trace_id,
            parent_span_id=ctx.id,
            name=f"llm.{call_site}",
            type="llm",
            input={"messages": messages[-3:] if len(messages) > 3 else messages},
            output={"completion": completion[:2000]},
            metadata={
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "latency_ms": round(latency_ms),
            },
        )
        span.end()
    except Exception:
        pass  # never let tracing break the main pipeline


# ── Span metadata helpers ─────────────────────────────────────────────────────

def update_current_span(**metadata) -> None:
    """Attach extra metadata to the innermost active span. No-op if not enabled."""
    if not _enabled:
        return
    try:
        from opik import opik_context
        opik_context.update_current_span(metadata=metadata)
    except Exception:
        pass


def update_current_trace(**metadata) -> None:
    """Attach extra metadata to the root trace of the current request. No-op if not enabled."""
    if not _enabled:
        return
    try:
        from opik import opik_context
        opik_context.update_current_trace(metadata=metadata)
    except Exception:
        pass


def is_enabled() -> bool:
    return _enabled
