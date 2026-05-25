"""
Production-parity helpers for offline evaluation.

Mirrors ``ui.pipeline.get_sql`` post-orchestration steps (fixups) and metric list
construction so benchmarks match chat behavior without importing Streamlit.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from core.sql.compilers import set_global_sql_guards
from core.sql.query_object import QueryObject
from ui.qo_fixups import (
    _apply_followup_context_repair,
    _apply_lineage_rollforward_filters,
    _apply_metric_variant_overrides,
    _apply_same_month_anchor_composite_metric,
    _apply_time_intent_overrides,
    _hydrate_retention_event_from_metric,
    _materialize_metric_status_into_filters_for_equality_cohorts,
    _sanitize_invalid_breakdown,
    _strip_redundant_calendar_day_filter,
)


def configure_sql_guards_from_catalog(catalog: dict) -> None:
    """Apply ``always_filter`` fragments from catalog (same idea as ``chat.py``)."""
    bctx = catalog.get("__business_context__", {}) if isinstance(catalog, dict) else {}
    raw = ((bctx.get("exclusions") or {}).get("always_filter") or [])
    guards = [f for f in raw if isinstance(f, str) and f.strip()]
    set_global_sql_guards(guards)


def build_metrics_like_chat(catalog: dict) -> list[dict]:
    """
    Match ``chat.load_schema_and_metrics`` metric list: suggested_metrics +
    ``ce_*`` pseudo-metrics from ``__business_context__.custom_events``.
    """
    metrics: list[dict] = []
    for tname, tdata in (catalog or {}).items():
        if not isinstance(tdata, dict) or str(tname).startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []) or []:
            if m.get("status") not in ("approved", None, ""):
                continue
            metrics.append({
                "id":               m.get("id", ""),
                "name":             m.get("name", ""),
                "description":      m.get("description", ""),
                "sql":              m.get("sql_hint", ""),
                "type":             m.get("type"),
                "builder_definition": m.get("builder_definition"),
            })
    biz = (catalog.get("__business_context__", {}) or {}) if isinstance(catalog, dict) else {}
    for ce in biz.get("custom_events", []) or []:
        if not isinstance(ce, dict):
            continue
        ce_name = (ce.get("name") or "").strip()
        ce_sql = (ce.get("sql") or "").strip()
        if not ce_name or not ce_sql:
            continue
        ce_id = f"ce_{re.sub(r'[^a-z0-9_]+', '_', ce_name.lower()).strip('_')}"
        metrics.append({
            "id": ce_id,
            "name": ce_name.replace("_", " ").title(),
            "description": ce.get("description", f"Custom event: {ce_name}"),
            "sql": f"SELECT COUNT(DISTINCT user_id) AS value FROM events WHERE ({ce_sql})",
        })
    return metrics


def apply_post_orchestrator_fixups(
    qo: QueryObject,
    *,
    question: str,
    catalog: dict,
    sampled: dict[str, dict[str, list]],
    metrics: list[dict],
    history: list[dict],
) -> None:
    """
    Same order as ``ui.pipeline.get_sql`` after ``orchestrate()`` (before resolver).

    Does not run the policy arbiter — benchmarks use ``gate_allow_execute`` instead
    unless you add an arbiter hook later.
    """
    if not qo:
        return
    from core.pipeline.normalize_query import normalize_query_object

    normalize_query_object(
        qo,
        prompt=question,
        history=history,
        catalog=catalog,
        sampled_values=sampled,
        metrics=metrics,
    )


def qo_snapshot(qo: QueryObject) -> dict[str, Any]:
    """Compact QO dict for eval traces (avoid huge / non-serializable fields)."""
    if not qo:
        return {}
    fs = getattr(qo, "filters", None) or {}
    return {
        "analysis_type": getattr(qo, "analysis_type", None),
        "event": getattr(qo, "event", None),
        "event_b": getattr(qo, "event_b", None),
        "metric_id": getattr(qo, "metric_id", None),
        "metric_variant": getattr(qo, "metric_variant", None),
        "metric_status_col": getattr(qo, "metric_status_col", None),
        "metric_status_target": getattr(qo, "metric_status_target", None),
        "breakdown": getattr(qo, "breakdown", None),
        "filters": dict(fs) if isinstance(fs, dict) else {},
        "date_from": getattr(qo, "date_from", None),
        "date_to": getattr(qo, "date_to", None),
        "time_range_days": getattr(qo, "time_range_days", None),
        "time_granularity": getattr(qo, "time_granularity", None),
        "time_source": getattr(qo, "time_source", None),
        "funnel_steps": list(getattr(qo, "funnel_steps", None) or []),
    }
