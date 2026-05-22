"""
Single post-orchestration normalization pipeline for QueryObject.

All fixups run here in a fixed order; prompt-level overrides run after catalog
hydration so explicit user intent (e.g. "14 day retention") wins over metric defaults.
"""
from __future__ import annotations

from typing import Optional

from core.semantic.query_semantics import attach_query_semantics
from core.sql.query_object import QueryObject


def _snap_qo(qo: QueryObject) -> dict:
    """Lightweight snapshot of mutable QO fields for delta tracking."""
    return {
        "analysis_type":      getattr(qo, "analysis_type", None),
        "event":              getattr(qo, "event", None),
        "event_b":            getattr(qo, "event_b", None),
        "metric_id":          getattr(qo, "metric_id", None),
        "metric_variant":     getattr(qo, "metric_variant", None),
        "metric_status_col":  getattr(qo, "metric_status_col", None),
        "breakdown":          getattr(qo, "breakdown", None),
        "filters":            dict(getattr(qo, "filters", None) or {}),
        "filter_excludes":    dict(getattr(qo, "filter_excludes", None) or {}),
        "time_range_days":    getattr(qo, "time_range_days", None),
        "time_granularity":   getattr(qo, "time_granularity", None),
        "retention_window_days": getattr(qo, "retention_window_days", None),
        "date_from":          getattr(qo, "date_from", None),
        "date_to":            getattr(qo, "date_to", None),
    }


def normalize_query_object(
    qo: QueryObject,
    *,
    prompt: str,
    history: Optional[list[dict]],
    catalog: dict,
    sampled_values: dict,
    metrics: Optional[list[dict]] = None,
) -> QueryObject:
    """
    Apply the full fixup chain and attach ``_query_semantics`` on ``qo``.

    Stores ``qo._fixup_deltas`` — a list of ``{fixup, delta}`` dicts for each
    pass that mutated the QO. Consumed by the eval harness for observability.
    """
    from ui.qo_fixups import (
        _apply_activation_window_from_prompt,
        _apply_followup_context_repair,
        _apply_lineage_rollforward_filters,
        _apply_metric_variant_overrides,
        _apply_retention_window_from_prompt,
        _apply_retention_context_from_history,
        _apply_same_month_anchor_composite_metric,
        _apply_display_modifier_followup,
        _apply_same_query_followup,
        _apply_time_intent_overrides,
        _hydrate_primary_event_for_action_types,
        _hydrate_retention_event_from_metric,
        _inherit_metric_status_and_filters,
        _materialize_metric_status_into_filters_for_equality_cohorts,
        _remap_invalid_filter_values_via_custom_events,
        _sanitize_invalid_breakdown,
        _sanitize_retention_dimension_status,
        _strip_redundant_calendar_day_filter,
    )

    if not qo or qo.analysis_type in ("clarify", "out_of_scope"):
        return qo

    deltas: list[dict] = []

    def _run(name: str, fn, *args, **kwargs) -> None:
        before = _snap_qo(qo)
        fn(*args, **kwargs)
        after = _snap_qo(qo)
        diff = {k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]}
        if diff:
            deltas.append({"fixup": name, "delta": diff})

    _run("time_intent_overrides",       _apply_time_intent_overrides,       qo, history)
    _run("hydrate_retention_event",     _hydrate_retention_event_from_metric, qo, catalog)
    _run("hydrate_action_event",        _hydrate_primary_event_for_action_types, qo, catalog)
    _run("metric_variant_overrides",    _apply_metric_variant_overrides,    qo, catalog)
    _run("sanitize_breakdown",          _sanitize_invalid_breakdown,         qo, sampled_values)
    _run("remap_filter_sentinels",      _remap_invalid_filter_values_via_custom_events, qo, sampled_values, catalog)
    _run("followup_context_repair",     _apply_followup_context_repair,      qo, history, catalog)
    _run("same_query_followup",         _apply_same_query_followup,          qo, prompt, history)
    _run("display_modifier_followup",   _apply_display_modifier_followup,    qo, prompt, history)
    _run("inherit_metric_status",       _inherit_metric_status_and_filters,  qo, history, prompt)
    _run("sanitize_retention_status",   _sanitize_retention_dimension_status, qo)
    _run("activation_window",           _apply_activation_window_from_prompt, qo, prompt, metrics=metrics)
    _run("retention_window",            _apply_retention_window_from_prompt, qo, prompt, catalog=catalog)
    _run("retention_context_history",   _apply_retention_context_from_history, qo, prompt, history)
    _run("same_month_anchor",           _apply_same_month_anchor_composite_metric, qo, metrics, prompt)
    _run("lineage_rollforward",         _apply_lineage_rollforward_filters,  qo, history, catalog)
    _run("materialize_status_filters",  _materialize_metric_status_into_filters_for_equality_cohorts, qo)
    _run("strip_calendar_filter",       _strip_redundant_calendar_day_filter, qo)

    qo._fixup_deltas = deltas  # type: ignore[attr-defined]
    attach_query_semantics(qo)
    return qo
