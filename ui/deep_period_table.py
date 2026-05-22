"""Build a compact period comparison table from Deep Analysis sub-query frames."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

import pandas as pd

_MONTH_PATTERN = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)

_PREFIX_MONTH = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _year_hint(question: str) -> Optional[int]:
    m = re.search(r"\b(20\d{2})\b", question)
    return int(m.group(1)) if m else None


def months_named_in_question(question: str, fallback_year: Optional[int] = None) -> list[tuple[int, int]]:
    """
    Unique (year, month) pairs named in the question, sorted chronologically.
    """
    if not (question or "").strip():
        return []
    y_default = _year_hint(question) or fallback_year or date.today().year
    ordered: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for m in _MONTH_PATTERN.finditer(question.lower()):
        tok = m.group(1).lower()
        prefix = tok[:3]
        mn = _PREFIX_MONTH.get(prefix)
        if mn is None:
            continue
        pair = (y_default, mn)
        if pair in seen:
            continue
        seen.add(pair)
        ordered.append(pair)
    ordered.sort(key=lambda x: (x[0], x[1]))
    return ordered


def _find_time_column(df: pd.DataFrame) -> Optional[str]:
    best: Optional[tuple[float, str]] = None
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            ratio = float(s.notna().mean())
        else:
            conv = pd.to_datetime(s, errors="coerce")
            ratio = float(conv.notna().mean())
        if ratio < 0.5:
            continue
        if best is None or ratio > best[0]:
            best = (ratio, str(c))
    return best[1] if best else None


def _find_numeric_value_column(df: pd.DataFrame, exclude: set[str]) -> Optional[str]:
    candidates = []
    for c in df.columns:
        cs = str(c).lower()
        if str(c) in exclude:
            continue
        if cs in ("user_id", "session_id", "row", "idx", "index"):
            continue
        if not pd.api.types.is_numeric_dtype(df[c]):
            continue
        candidates.append(c)
    if not candidates:
        return None
    for pref in ("users", "user", "count", "n", "value", "rate", "pct", "total", "retained"):
        for c in candidates:
            if pref in str(c).lower():
                return c
    return candidates[0]


def _series_by_calendar_month(df: pd.DataFrame, tcol: str, ncol: str, year_fallback: int) -> dict[tuple[int, int], float]:
    work = df[[tcol, ncol]].copy()
    if not pd.api.types.is_datetime64_any_dtype(work[tcol]):
        work[tcol] = pd.to_datetime(work[tcol], errors="coerce")
    work = work.dropna(subset=[tcol])
    out: dict[tuple[int, int], float] = {}
    for _, row in work.iterrows():
        dt = row[tcol]
        if pd.isna(dt):
            continue
        y, m = int(dt.year), int(dt.month)
        if y < 2000:
            y = year_fallback
        try:
            val = float(row[ncol])
        except (TypeError, ValueError):
            continue
        if pd.isna(val):
            continue
        out[(y, m)] = val
    return out


def build_deep_period_comparison_table(
    question: str,
    sub_results: list[Any],
    *,
    fallback_year: Optional[int] = None,
) -> Optional[pd.DataFrame]:
    """
    When the user names multiple calendar months, assemble a wide table:
      Investigation | Feb 2026 | Mar 2026 | ...

    Values come from the first parseable time + numeric column in each successful sub-result.
    Returns None if fewer than two target months or no usable data.
    """
    fy = fallback_year or _year_hint(question) or date.today().year
    target_months = months_named_in_question(question, fallback_year=fy)
    if len(target_months) < 2:
        return None

    col_labels = [
        f"{pd.Timestamp(year=y, month=m, day=1).strftime('%b %Y')}" for y, m in target_months
    ]
    col_keys = target_months

    rows: list[dict[str, Any]] = []
    for r in sub_results:
        if not getattr(r, "ok", False):
            continue
        df = getattr(r, "df", None)
        if df is None or getattr(df, "empty", True):
            continue
        tcol = _find_time_column(df)
        if not tcol:
            continue
        ncol = _find_numeric_value_column(df, exclude={tcol})
        if not ncol:
            continue
        by_m = _series_by_calendar_month(df, tcol, ncol, year_fallback=fy)
        label = (getattr(r, "sub_question", None) or "Investigation").strip()
        if len(label) > 72:
            label = label[:69] + "…"
        row: dict[str, Any] = {"Investigation": label}
        any_hit = False
        for lbl, key in zip(col_labels, col_keys):
            if key in by_m:
                row[lbl] = by_m[key]
                any_hit = True
            else:
                row[lbl] = pd.NA
        if any_hit:
            rows.append(row)

    if not rows:
        return None
    out_df = pd.DataFrame(rows)
    cols = ["Investigation"] + col_labels
    cols = [c for c in cols if c in out_df.columns]
    return out_df[cols]
