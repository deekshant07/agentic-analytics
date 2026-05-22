"""
Single post-orchestration normalization pipeline for QueryObject.

All fixups run here in a fixed order; prompt-level overrides run after catalog
hydration so explicit user intent (e.g. "14 day retention") wins over metric defaults.
"""
from __future__ import annotations

from typing import Optional

from core.semantic.query_semantics import attach_query_semantics
from core.sql.query_object import QueryObject


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

    _apply_time_intent_overrides(qo, history)
    _hydrate_retention_event_from_metric(qo, catalog)
    _hydrate_primary_event_for_action_types(qo, catalog)
    _apply_metric_variant_overrides(qo, catalog)
    _sanitize_invalid_breakdown(qo, sampled_values)
    _remap_invalid_filter_values_via_custom_events(qo, sampled_values, catalog)
    _apply_followup_context_repair(qo, history, catalog)
    _apply_same_query_followup(qo, prompt, history)
    _apply_display_modifier_followup(qo, prompt, history)
    _inherit_metric_status_and_filters(qo, history, prompt)
    _sanitize_retention_dimension_status(qo)
    # Prompt overrides after hydration — explicit N-day / MOM beats catalog D7.
    _apply_activation_window_from_prompt(qo, prompt, metrics=metrics)
    _apply_retention_window_from_prompt(qo, prompt, catalog=catalog)
    _apply_retention_context_from_history(qo, prompt, history)
    _apply_same_month_anchor_composite_metric(qo, metrics, prompt)
    _apply_lineage_rollforward_filters(qo, history, catalog)
    _materialize_metric_status_into_filters_for_equality_cohorts(qo)
    _strip_redundant_calendar_day_filter(qo)

    attach_query_semantics(qo)
    return qo
