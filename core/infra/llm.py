"""
llm.py — Provider-agnostic LLM client factory + logging wrapper.

Switch providers by setting LLM_PROVIDER in your .env:
    LLM_PROVIDER=groq      → Groq (Llama 4 Scout on Groq's OpenAI-compatible API)
    LLM_PROVIDER=grok      → xAI Grok (set XAI_API_KEY; alias: LLM_PROVIDER=xai)
    LLM_PROVIDER=gemini    → Google Gemini (gemini-2.5-flash / gemini-2.5-flash-lite)
    LLM_PROVIDER=openai    → OpenAI (gpt-4o / gpt-4o-mini)
    LLM_PROVIDER=ollama    → Ollama local (llama3.2 / llama3.2)

Override individual models without changing provider:
    LLM_STRONG_MODEL=llama-3.3-70b-versatile
    LLM_MEDIUM_MODEL=llama-3.1-8b-instant
    LLM_FAST_MODEL=llama-3.1-8b-instant

Per-component overrides (useful for eval vs. app):
    EVAL_LLM_PROVIDER=openai       # eval orchestrator uses this provider
    EVAL_STRONG_MODEL=gpt-4o       # override strong model for eval only
    JUDGE_MODEL=gpt-4o-mini        # override model used by the LLM judge specifically

All providers use the OpenAI-compatible chat completions API — no new deps.

Usage:
    from core.infra.llm import call_llm, make_llm_client, LLM_STRONG, LLM_MEDIUM, LLM_FAST
    # Default (reads LLM_PROVIDER from env):
    client = make_llm_client()
    resp = call_llm(client, call_site="orchestrator", model=LLM_STRONG, messages=[...])

    # Explicit provider override (e.g. in eval):
    client = make_llm_client(provider="openai")
    model  = resolve_model("strong", provider="openai")   # → "gpt-4o"
    resp   = call_llm(client, call_site="eval_judge", model=model, messages=[...])
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from openai import OpenAI

from core.infra.logger import log_llm_call
from core.infra.tracer import record_llm_call


# ── Provider registry ─────────────────────────────────────────────────────────
# Each entry: base_url (None = OpenAI default), api_key_env, strong/medium/fast model.

_PROVIDERS: dict[str, dict] = {
    "groq": {
        "base_url":    "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "strong":      "meta-llama/llama-4-scout-17b-16e-instruct",
        "medium":      "meta-llama/llama-4-scout-17b-16e-instruct",
        "fast":        "meta-llama/llama-4-scout-17b-16e-instruct",
    },
    "cerebras": {
        "base_url":    "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "strong":      "qwen-3-235b-a22b-instruct-2507",
        "medium":      "llama3.1-8b",
        "fast":        "llama3.1-8b",
    },
    "sambanova": {
        "base_url":    "https://api.sambanova.ai/v1",
        "api_key_env": "SAMBANOVA_API_KEY",
        "strong":      "Meta-Llama-3.3-70B-Instruct",  # 10M tokens/day free
        "medium":      "Meta-Llama-3.1-8B-Instruct",
        "fast":        "Meta-Llama-3.1-8B-Instruct",
    },
    "grok": {
        "base_url":    "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "strong":      "grok-3-latest",
        "medium":      "grok-3-latest",
        "fast":        "grok-3-latest",
    },
    "gemini": {
        "base_url":    "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GOOGLE_API_KEY",
        "strong":      "gemini-2.5-flash",
        "medium":      "gemini-2.5-flash-lite",
        "fast":        "gemini-2.5-flash-lite",
    },
    "openai": {
        "base_url":    None,
        "api_key_env": "OPENAI_API_KEY",
        "strong":      "gpt-4o",
        "medium":      "gpt-4o-mini",
        "fast":        "gpt-4o-mini",
    },
    "anthropic": {
        "base_url":    "https://api.anthropic.com/v1/",
        "api_key_env": "ANTHROPIC_API_KEY",
        "strong":      "claude-sonnet-4-6",
        "medium":      "claude-haiku-4-5-20251001",
        "fast":        "claude-haiku-4-5-20251001",
    },
    "together": {
        "base_url":    "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
        "strong":      "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "medium":      "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "fast":        "Qwen/Qwen2.5-7B-Instruct-Turbo",
    },
    "ollama": {
        "base_url":    "http://localhost:11434/v1",
        "api_key_env": None,
        "strong":      "llama3.2",
        "medium":      "llama3.2",
        "fast":        "llama3.2",
    },
}

_DEFAULT_PROVIDER = "groq"


# ── Provider lookup ───────────────────────────────────────────────────────────

def _get_provider(name: str | None = None) -> dict:
    """Return provider config for `name`, falling back to LLM_PROVIDER env var."""
    if name is None:
        name = os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER)
    name = name.lower()
    if name == "xai":
        name = "grok"
    if name not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider '{name}'. Choose from: {list(_PROVIDERS)}"
        )
    return _PROVIDERS[name]


# ── Model resolution ──────────────────────────────────────────────────────────

def resolve_model(tier: str = "strong", provider: str | None = None) -> str:
    """
    Return the model name for a given tier and provider.

    Args:
        tier:     "strong" | "medium" | "fast"
        provider: provider name (e.g. "openai"). None → reads LLM_PROVIDER from env.

    Env overrides:
        LLM_STRONG_MODEL / LLM_MEDIUM_MODEL / LLM_FAST_MODEL — default provider only
        EVAL_STRONG_MODEL — overrides strong model when an explicit eval provider is set
    """
    p = _get_provider(provider)
    if provider is None:
        # Respect per-tier env overrides only for the default provider
        env_map = {
            "strong": "LLM_STRONG_MODEL",
            "medium": "LLM_MEDIUM_MODEL",
            "fast":   "LLM_FAST_MODEL",
        }
        override = os.environ.get(env_map.get(tier, ""))
        if override:
            return override
    elif tier == "strong":
        # EVAL_STRONG_MODEL lets you pin e.g. gpt-4o-mini for eval runs without
        # affecting the main app's strong model.
        eval_override = os.environ.get("EVAL_STRONG_MODEL", "").strip()
        if eval_override:
            return eval_override
    return p.get(tier) or p["fast"]


# ── Backward-compat constants (resolved at import time from default provider) ─

def _resolve_models() -> tuple[str, str, str]:
    return (
        resolve_model("strong"),
        resolve_model("medium"),
        resolve_model("fast"),
    )


LLM_STRONG, LLM_MEDIUM, LLM_FAST = _resolve_models()


# ── Client factory ─────────────────────────────────────────────────────────────

def make_llm_client(api_key: str | None = None, provider: str | None = None) -> OpenAI:
    """
    Return an OpenAI-compatible client.

    Args:
        api_key:  Explicit API key. None → read from provider's api_key_env.
        provider: Provider name override (e.g. "openai"). None → LLM_PROVIDER env var.
    """
    p = _get_provider(provider)
    if api_key is None:
        env_var = p.get("api_key_env")
        api_key = os.environ.get(env_var, "") if env_var else "ollama"
    kwargs: dict[str, Any] = {"api_key": api_key}
    if p["base_url"]:
        kwargs["base_url"] = p["base_url"]
    return OpenAI(**kwargs)


# ── Rate-limit retry ──────────────────────────────────────────────────────────

_RETRY_DELAY_RE = re.compile(r"retry in\s+([\d.]+)s", re.IGNORECASE)
_MAX_RETRIES = 3


def call_llm(client: OpenAI, *, call_site: str, **kwargs: Any) -> Any:
    """
    Wraps client.chat.completions.create() with log_llm_call() instrumentation
    and automatic retry on 429 rate-limit responses (up to 3 retries).

    Billing quota exhaustion (insufficient_quota / spending cap) is NOT retried.
    """
    model = kwargs.get("model", "")
    for attempt in range(_MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(**kwargs)
            usage = resp.usage
            latency = (time.perf_counter() - t0) * 1000
            pt = getattr(usage, "prompt_tokens", 0) or 0
            ct = getattr(usage, "completion_tokens", 0) or 0
            log_llm_call(
                call_site=call_site,
                model=model,
                prompt_tokens=pt,
                completion_tokens=ct,
                latency_ms=latency,
            )
            record_llm_call(
                call_site=call_site,
                model=model,
                messages=kwargs.get("messages", []),
                completion=(resp.choices[0].message.content or "") if resp.choices else "",
                prompt_tokens=pt,
                completion_tokens=ct,
                latency_ms=latency,
            )
            return resp
        except Exception as exc:
            err_str = str(exc)
            is_quota = "insufficient_quota" in err_str or "spending cap" in err_str.lower()
            is_rate_limit = (
                not is_quota
                and ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str)
            )
            elapsed = (time.perf_counter() - t0) * 1000
            if is_rate_limit and attempt < _MAX_RETRIES:
                m = _RETRY_DELAY_RE.search(err_str)
                delay = float(m.group(1)) + 2.0 if m else 30.0
                log_llm_call(
                    call_site=call_site, model=model,
                    latency_ms=elapsed,
                    error=f"rate_limit_retry_{attempt+1}: sleeping {delay:.0f}s",
                )
                time.sleep(delay)
                continue
            log_llm_call(
                call_site=call_site, model=model,
                latency_ms=elapsed,
                error=err_str,
            )
            raise
