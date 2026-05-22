"""
tools.py — OpenAI function-calling tool layer for the analytics agent.

Exposes four tools the LLM can invoke:
  query_database        — run a read-only SQL query and return results
  get_metric_definition — look up a metric in the catalog by id/name
  get_available_events  — list all tracked event names with descriptions
  get_industry_benchmark — return industry benchmark values for common metrics

Usage (standalone):
    from core.infra.tools import ToolAgent
    agent = ToolAgent(db_path="jupiter.duckdb", catalog=catalog, api_key=key)
    answer = agent.run("How does our D7 retention compare to fintech benchmarks?")
    print(answer)
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

# ── Tool definitions (OpenAI function-calling schema) ─────────────────────────

ANALYTICS_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "Execute a read-only SQL SELECT query against the analytics DuckDB database. "
                "Tables available: events (user_id, event_name, timestamp, platform, …), "
                "users (user_id, city, age_group, income_bracket, …). "
                "Use this when you need actual data to answer the question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A valid DuckDB SELECT query. Must start with SELECT or WITH.",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "One sentence describing what question this query answers.",
                    },
                },
                "required": ["sql", "purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metric_definition",
            "description": (
                "Look up a metric definition from the product catalog. "
                "Returns the metric's name, description, SQL hint, and status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_id": {
                        "type": "string",
                        "description": "The metric ID or partial name to look up (e.g. 'dau', 'activation_rate').",
                    },
                },
                "required": ["metric_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_events",
            "description": (
                "List all tracked event names with their descriptions and semantic labels "
                "(journey stage, funnel position). Use this to discover what data exists "
                "before writing queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_industry_benchmark",
            "description": (
                "Return industry benchmark values for a common product metric. "
                "Useful for contextualising whether a metric is healthy or not."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": (
                            "Metric name (e.g. 'day7_retention', 'day30_retention', "
                            "'activation_rate', 'dau_mau_ratio', 'funnel_conversion')."
                        ),
                    },
                    "industry": {
                        "type": "string",
                        "description": "Industry segment: 'fintech', 'saas', 'ecommerce', 'edtech', 'healthtech'.",
                    },
                },
                "required": ["metric"],
            },
        },
    },
]


# ── Industry benchmarks (static reference table) ──────────────────────────────

_BENCHMARKS: dict[str, dict[str, str]] = {
    "day1_retention": {
        "fintech": "25–40%",
        "saas": "30–45%",
        "ecommerce": "20–35%",
        "edtech": "30–50%",
        "default": "25–40%",
    },
    "day7_retention": {
        "fintech": "10–20%",
        "saas": "15–25%",
        "ecommerce": "8–15%",
        "edtech": "12–22%",
        "default": "10–20%",
    },
    "day30_retention": {
        "fintech": "5–12%",
        "saas": "8–18%",
        "ecommerce": "3–8%",
        "edtech": "5–15%",
        "default": "5–12%",
    },
    "activation_rate": {
        "fintech": "40–60%",
        "saas": "30–50%",
        "ecommerce": "50–70%",
        "edtech": "35–55%",
        "default": "40–60%",
    },
    "dau_mau_ratio": {
        "fintech": "10–25%",
        "saas": "20–40%",
        "ecommerce": "5–15%",
        "edtech": "15–35%",
        "default": "15–30%",
    },
    "funnel_conversion": {
        "fintech": "3–8%",
        "saas": "1–5%",
        "ecommerce": "1–4%",
        "edtech": "5–12%",
        "default": "2–6%",
    },
    "transaction_success_rate": {
        "fintech": "90–97%",
        "ecommerce": "92–98%",
        "default": "90–97%",
    },
}


# ── SQL safety guard ───────────────────────────────────────────────────────────

_DANGEROUS_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _is_safe_sql(sql: str) -> tuple[bool, str]:
    """Return (is_safe, reason). Rejects anything that isn't a read-only SELECT."""
    s = sql.strip()
    upper = s.upper().lstrip()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, "Query must start with SELECT or WITH"
    m = _DANGEROUS_PATTERN.search(s)
    if m:
        return False, f"Dangerous keyword detected: {m.group()}"
    return True, ""


# ── Tool implementations ───────────────────────────────────────────────────────

def _tool_query_database(sql: str, purpose: str, db_path: str) -> str:
    safe, reason = _is_safe_sql(sql)
    if not safe:
        return json.dumps({"error": f"Query rejected: {reason}"})
    try:
        conn = duckdb.connect(db_path, read_only=True)
        try:
            df = conn.execute(sql).df()
        finally:
            conn.close()
        if df.empty:
            return json.dumps({"rows": 0, "data": [], "note": "Query returned no rows."})
        preview = df.head(20).to_dict(orient="records")
        num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        stats = {}
        for col in num_cols[:4]:
            s = df[col].dropna()
            if not s.empty:
                stats[col] = {
                    "min": float(s.min()),
                    "max": float(s.max()),
                    "mean": round(float(s.mean()), 2),
                    "sum": float(s.sum()),
                }
        return json.dumps(
            {"rows": len(df), "columns": list(df.columns), "data": preview, "stats": stats},
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _tool_get_metric_definition(metric_id: str, catalog: dict) -> str:
    needle = metric_id.lower().strip()
    matches = []
    for tname, tdata in catalog.items():
        if tname.startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []):
            mid = str(m.get("id", "")).lower()
            mname = str(m.get("name", "")).lower()
            if needle in mid or needle in mname:
                matches.append({
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "description": m.get("description"),
                    "sql_hint": m.get("sql_hint", ""),
                    "status": m.get("status", ""),
                    "table": tname,
                })
    if not matches:
        return json.dumps({"error": f"No metric found matching '{metric_id}'"})
    return json.dumps({"matches": matches[:5]}, default=str)


def _tool_get_available_events(catalog: dict) -> str:
    events = {}
    event_semantics = {}
    for tdata in catalog.values():
        if isinstance(tdata, dict):
            events.update(tdata.get("events", {}) or {})
            event_semantics.update(
                (tdata.get("events", {}) or {}).get("event_semantics", {}) or {}
            )
    # Try top-level event_semantics
    if "events" in catalog and isinstance(catalog["events"], dict):
        event_semantics.update(catalog["events"].get("event_semantics", {}))

    result = []
    for ev_name, ev_meta in event_semantics.items():
        result.append({
            "event": ev_name,
            "description": ev_meta.get("description", ""),
            "journey": ev_meta.get("journey", ""),
            "stage": ev_meta.get("stage", ""),
        })
    if not result:
        return json.dumps({"note": "No event metadata found in catalog."})
    return json.dumps({"events": result}, default=str)


def _tool_get_industry_benchmark(metric: str, industry: str = "default") -> str:
    needle = metric.lower().replace(" ", "_").replace("-", "_")
    ind = (industry or "default").lower()
    # Try exact match first
    bench = _BENCHMARKS.get(needle)
    if bench is None:
        # Fuzzy: find any key that contains the needle
        for key, val in _BENCHMARKS.items():
            if needle in key or key in needle:
                bench = val
                break
    if bench is None:
        available = list(_BENCHMARKS.keys())
        return json.dumps({"error": f"No benchmark for '{metric}'", "available_metrics": available})
    value = bench.get(ind) or bench.get("default", "N/A")
    return json.dumps({
        "metric": metric,
        "industry": ind,
        "benchmark": value,
        "note": "Source: industry estimates (Mixpanel, Amplitude, a16z benchmarks, 2023–2024).",
    })


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def _dispatch_tool(name: str, args: dict, db_path: str, catalog: dict) -> str:
    if name == "query_database":
        return _tool_query_database(args["sql"], args.get("purpose", ""), db_path)
    if name == "get_metric_definition":
        return _tool_get_metric_definition(args["metric_id"], catalog)
    if name == "get_available_events":
        return _tool_get_available_events(catalog)
    if name == "get_industry_benchmark":
        return _tool_get_industry_benchmark(args["metric"], args.get("industry", "default"))
    return json.dumps({"error": f"Unknown tool: {name}"})


# ── ToolAgent ─────────────────────────────────────────────────────────────────

_TOOL_SYSTEM = """You are a senior data analyst with direct access to analytics tools.
When the user asks a question:
1. Call tools to gather the data you need.
2. Reason over the results.
3. Answer with exact numbers. No hedging. No "it appears" or "seems like".
4. If a benchmark is relevant, fetch it and compare explicitly.
5. End with one concrete recommended action the user can take TODAY.

Available data: DuckDB analytics database with events and users tables.
"""


class ToolAgent:
    """
    A simple tool-use loop agent powered by OpenAI function calling.

    Unlike the main pipeline (which deterministically compiles SQL from a QueryObject),
    the ToolAgent lets the LLM decide which tools to call and in what order.
    Best suited for ad-hoc questions, benchmark comparisons, or multi-step lookups
    that don't fit neatly into a single analysis_type.

    Example:
        agent = ToolAgent(db_path="jupiter.duckdb", catalog=catalog, api_key=key)
        answer = agent.run("How does our D7 retention compare to fintech industry benchmarks?")
    """

    def __init__(
        self,
        db_path: str,
        catalog: dict,
        api_key: Optional[str] = None,
        model: str = None,
        max_turns: int = 6,
    ) -> None:
        self.db_path = db_path
        self.catalog = catalog
        self.api_key = api_key
        self.model = model
        self.max_turns = max_turns

    def run(self, question: str) -> str:
        """
        Run the tool-use loop and return a final text answer.
        """
        from core.infra.llm import make_llm_client, LLM_STRONG
        from core.infra.logger import log_tool_call

        client = make_llm_client(self.api_key)
        model = self.model or LLM_STRONG
        messages: list[dict] = [
            {"role": "system", "content": _TOOL_SYSTEM},
            {"role": "user", "content": question},
        ]

        for _ in range(self.max_turns):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=ANALYTICS_TOOLS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message

            # No tool call → final answer
            if not msg.tool_calls:
                return msg.content or ""

            messages.append(msg.model_dump(exclude_none=True))

            # Execute each tool call
            for tc in msg.tool_calls:
                t0 = time.perf_counter()
                try:
                    args = json.loads(tc.function.arguments)
                    result = _dispatch_tool(tc.function.name, args, self.db_path, self.catalog)
                    success = "error" not in json.loads(result)
                    log_tool_call(
                        tool_name=tc.function.name,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                        success=success,
                    )
                except Exception as exc:
                    result = json.dumps({"error": str(exc)})
                    log_tool_call(
                        tool_name=tc.function.name,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                        success=False,
                        error=str(exc),
                    )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        # Max turns reached — ask for a final answer from what we have
        messages.append({"role": "user", "content": "Summarise your findings in a final answer."})
        resp = client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""
