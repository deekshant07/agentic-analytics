"""Presentation rules derived from resolved query semantics."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.semantic.query_semantics import ResolvedQuerySemantics, ValueKind, resolve_query_semantics
from core.sql.query_object import QueryObject


def is_rate_column_name(col: str, semantics: Optional[ResolvedQuerySemantics] = None) -> bool:
    if semantics and semantics.value_kind == ValueKind.PERCENT_0_100:
        if semantics.primary_metric_column and col == semantics.primary_metric_column:
            return True
        if col.endswith("_pct") or col in ("pct", "rate_pct", "retention_pct", "activation_rate"):
            return True
    cl = str(col).lower()
    return (
        cl in ("retention_pct", "rate_pct", "status_rate_pct", "pct", "activation_rate")
        or cl.endswith("_pct")
        or cl.endswith("_rate")
        or ("activation" in cl and ("pct" in cl or "rate" in cl))
    )


def preferred_display_column(
    df: pd.DataFrame,
    semantics: Optional[ResolvedQuerySemantics] = None,
) -> Optional[str]:
    """Column to chart / summarize — never prefer cohort_size over retention_pct."""
    if df is None or df.empty:
        return None
    if semantics and semantics.primary_metric_column:
        col = semantics.primary_metric_column
        if col in df.columns:
            return col
    preferred = [
        "retention_pct",
        "activation_rate",
        "rate_pct",
        "status_rate_pct",
        "pct",
    ]
    for col in preferred:
        if col in df.columns:
            return col
    pct_cols = [c for c in df.columns if is_rate_column_name(c)]
    if pct_cols:
        return pct_cols[0]
    skip = {"cohort_size", "retained_users", "cohort_week", "cohort_month"}
    num_cols = [
        c
        for c in df.select_dtypes("number").columns
        if c not in skip
    ]
    return num_cols[0] if num_cols else None


def format_rate_columns_for_display(
    df: pd.DataFrame,
    semantics: Optional[ResolvedQuerySemantics] = None,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if not is_rate_column_name(col, semantics):
            continue
        out[col] = out[col].apply(
            lambda v: (
                f"{float(v):.1f}%"
                if pd.notna(v) and str(v).strip() not in ("", "nan")
                else v
            )
        )
    return out


def presentation_from_qo(qo: Optional[QueryObject]) -> Optional[ResolvedQuerySemantics]:
    if not qo:
        return None
    sem = getattr(qo, "_query_semantics", None)
    if sem is not None:
        return sem
    if getattr(qo, "analysis_type", None) == "retention":
        return resolve_query_semantics(qo)
    return None
