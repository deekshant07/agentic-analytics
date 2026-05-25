"""
Shared catalog → orchestrator vocabulary helpers.

Merges catalog column value_meanings with DB samples so the LLM sees qualifiers
(UPI, etc.) even when profiling omitted them. Used by build_vocab() and query rescue.
"""
from __future__ import annotations

import re
from typing import Optional

_PROMPT_FILTER_VALUES_MAX = 20
_PROMPT_DIM_HINT_MAX = 12


def catalog_value_meanings_by_column(catalog: dict) -> dict[str, list[str]]:
    """Collect filterable values from catalog events[].value_meanings (all tables)."""
    by_col: dict[str, set[str]] = {}
    for tdata in catalog.values():
        if not isinstance(tdata, dict):
            continue
        for col_def in tdata.get("events") or []:
            if not isinstance(col_def, dict):
                continue
            raw_name = str(col_def.get("raw_name") or "").strip()
            if not raw_name:
                continue
            for val_key in (col_def.get("value_meanings") or {}):
                vk = str(val_key).strip()
                if vk:
                    by_col.setdefault(raw_name, set()).add(vk)
    return {col: sorted(vals) for col, vals in by_col.items()}


def merge_filter_vocab(
    catalog: dict,
    sampled_values: dict[str, dict[str, list]],
) -> tuple[dict[str, list], dict[str, list], list[str]]:
    """
    Build filter_cols and dim_value_hints from catalog meanings + DB samples.

    Catalog meanings are the base; sampled values extend/override per column.
    Returns (filter_cols, dim_value_hints, dimension_cols).
    """
    filter_cols: dict[str, list] = {}
    dim_value_hints: dict[str, list] = {}
    dimension_cols: list[str] = []

    for col, vals in catalog_value_meanings_by_column(catalog).items():
        filter_cols[col] = list(vals)

    for table_vals in (sampled_values or {}).values():
        if not isinstance(table_vals, dict):
            continue
        for col, vals in table_vals.items():
            if not vals:
                continue
            merged: list[str] = []
            seen: set[str] = set()
            for v in list(vals) + filter_cols.get(col, []):
                s = str(v).strip()
                if not s or s.lower() in seen:
                    continue
                seen.add(s.lower())
                merged.append(s)
            filter_cols[col] = merged[:_PROMPT_FILTER_VALUES_MAX]
            if 2 <= len(merged) <= 15:
                dimension_cols.append(col)
                dim_value_hints[col] = merged[:_PROMPT_DIM_HINT_MAX]

    for col, vals in filter_cols.items():
        if col not in dim_value_hints and 2 <= len(vals) <= 15:
            dimension_cols.append(col)
            dim_value_hints[col] = vals[:_PROMPT_DIM_HINT_MAX]

    return filter_cols, dim_value_hints, sorted(set(dimension_cols))


def extract_dimension_filters_from_question(
    question: str,
    sampled_values: Optional[dict],
    catalog: Optional[dict] = None,
) -> dict[str, str]:
    """Map user words to column=value using merged catalog + sampled vocabulary."""
    if not question:
        return {}
    q_lower = question.lower()
    found: dict[str, str] = {}

    # Min 3 chars: short tokens (2-char country/state codes, etc.) produce too many
    # false positives when they coincide with common prepositions or abbreviations.
    _MIN_LEN = 3

    if catalog:
        _, hints, _ = merge_filter_vocab(catalog, sampled_values or {})
        for col, vals in hints.items():
            for val in vals:
                vs = str(val).strip()
                if len(vs) < _MIN_LEN:
                    continue
                if re.search(r"\b" + re.escape(vs.lower()) + r"\b", q_lower):
                    found[str(col)] = vs
        return found

    for table_vals in (sampled_values or {}).values():
        if not isinstance(table_vals, dict):
            continue
        for col, vals in table_vals.items():
            for raw in vals or []:
                val = str(raw).strip()
                if len(val) < _MIN_LEN:
                    continue
                if re.search(r"\b" + re.escape(val.lower()) + r"\b", q_lower):
                    found[str(col)] = val
    return found
