"""Backward-compatible re-exports — prefer core.semantic.presentation."""
from core.semantic.presentation import (
    format_rate_columns_for_display,
    is_rate_column_name,
    preferred_display_column,
    presentation_from_qo,
)

__all__ = [
    "format_rate_columns_for_display",
    "is_rate_column_name",
    "preferred_display_column",
    "presentation_from_qo",
]
