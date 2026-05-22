"""Parse activation conversion window (N days) from user prompts — industry-agnostic."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

_ROLLING_WINDOW_RE = re.compile(
    r"\b(?:last|past|previous|next)\s+(\d+)\s+days?\b", re.IGNORECASE
)

_COL_WINDOW_SUFFIX_RE = re.compile(r"_(\d+)d$", re.IGNORECASE)


def _is_rolling_time_window(prompt_lower: str, days: int) -> bool:
    """True when ``days`` appears only as a rolling range (e.g. 'last 7 days')."""
    return bool(_ROLLING_WINDOW_RE.search(prompt_lower)) and str(days) in (
        m.group(1) for m in _ROLLING_WINDOW_RE.finditer(prompt_lower)
    )


def _prompt_has_retention_window_context(prompt_lower: str) -> bool:
    """True when the prompt is about a retention return window (not only rolling lookback)."""
    if "retention" in prompt_lower:
        return True
    return bool(
        re.search(
            r"\bsame\b.{0,48}\b(?:retention|metric)\b|\b(?:retention|metric)\b.{0,48}\bsame\b",
            prompt_lower,
        )
    )


def parse_retention_window_days_from_prompt(prompt: str) -> Optional[int]:
    """Extract D{N} retention window (e.g. D7, 7 day retention, for 14 days)."""
    if not prompt:
        return None
    pl = prompt.lower()
    if not _prompt_has_retention_window_context(pl):
        return None

    m = re.search(
        r"\b(?:d(\d+)|(?<!last )(?<!past )(?<!next )(?<!previous )(\d+)\s*-?\s*days?)\b"
        r"(?=(?:\s+\w+){0,8}\s+retention\b)",
        pl,
    )
    if m:
        days = int(m.group(1) or m.group(2))
        if not _is_rolling_time_window(pl, days):
            return days

    m = re.search(
        r"\bretention\b(?:\s+\w+){0,6}\s+(?:d(\d+)|(\d+)\s*-?\s*days?)\b",
        pl,
    )
    if m:
        days = int(m.group(1) or m.group(2))
        if not _is_rolling_time_window(pl, days):
            return days

    m = re.search(r"\bd(\d+)\b(?=[^.]{0,40}\bretention\b)|\bretention\b[^.]{0,40}\bd(\d+)\b", pl)
    if m:
        days = int(m.group(1) or m.group(2))
        if not _is_rolling_time_window(pl, days):
            return days

    # "first 14 days" / "for 14 days" (follow-ups: "same retention … for 14 days")
    m = re.search(
        r"(?:\bfirst\b|(?:\bfor\b|\bwithin\b|\bover\b))\s+(\d+)\s+days?\b",
        pl,
    )
    if m:
        days = int(m.group(1))
        if not _is_rolling_time_window(pl, days):
            return days
    return None


def apply_retention_window_from_prompt(
    qo: Any,
    prompt: Optional[str],
    *,
    catalog: Optional[dict] = None,
) -> None:
    """
    Set ``qo.retention_window_days`` from the user prompt for retention queries.

    Runs after metric hydration so an explicit "for 14 days" overrides catalog D7.
    """
    if not qo or not prompt:
        return
    if getattr(qo, "analysis_type", "") != "retention":
        return
    days = parse_retention_window_days_from_prompt(prompt)
    if days is not None:
        qo.retention_window_days = days
        setattr(qo, "_retention_window_explicit", True)


def parse_activation_window_days_from_prompt(prompt: str) -> Optional[int]:
    """
    Extract N-day activation window from natural language.

    Handles D7, 7-day, and 7 day (space) before/after the word ``activation``.
    Skips rolling-window phrases like ``last 7 days``.
    """
    if not prompt:
        return None
    pl = prompt.lower()

    m = re.search(
        r"\b(?:d(\d+)|(?<!last )(?<!past )(?<!next )(?<!previous )(\d+)\s*-?\s*days?)\b"
        r"(?=(?:\s+\w+){0,8}\s+activation\b)",
        pl,
    )
    if m:
        days = int(m.group(1) or m.group(2))
        if not _is_rolling_time_window(pl, days):
            return days

    m = re.search(
        r"\bactivation\b(?:\s+\w+){0,6}\s+(?:d(\d+)|(\d+)\s*-?\s*days?)\b",
        pl,
    )
    if m:
        days = int(m.group(1) or m.group(2))
        if not _is_rolling_time_window(pl, days):
            return days

    m = re.search(r"\bd(\d+)\b|(\d+)-day\b", pl)
    if m:
        days = int(m.group(1) or m.group(2))
        if not _is_rolling_time_window(pl, days):
            return days
    return None


def _metric_builder_type(metric: dict) -> str:
    return str((metric.get("builder_definition") or {}).get("builder_type") or "").lower()


def is_pct_users_metric(
    metric_id: str,
    *,
    catalog: Optional[dict] = None,
    metrics: Optional[list[dict]] = None,
) -> bool:
    if metrics:
        m = next((x for x in metrics if x.get("id") == metric_id), None)
        if m:
            bt = _metric_builder_type(m)
            return "% of users" in bt or "pct of users" in bt
    if catalog:
        for tname, tdata in catalog.items():
            if tname.startswith("__") or not isinstance(tdata, dict):
                continue
            for m in tdata.get("suggested_metrics") or []:
                if isinstance(m, dict) and m.get("id") == metric_id:
                    bt = _metric_builder_type(m)
                    return "% of users" in bt or "pct of users" in bt
    return False


def apply_activation_window_from_prompt(
    qo: Any,
    prompt: Optional[str],
    *,
    catalog: Optional[dict] = None,
    metrics: Optional[list[dict]] = None,
) -> None:
    """
    Set ``qo.activation_window_days`` from the user prompt for % of users metrics.

    Overwrites an existing value when the prompt names an explicit N-day activation
    window (fixes LLM defaulting to 30 while user asked for 60, etc.).
    """
    if not qo or not prompt:
        return
    mid = getattr(qo, "metric_id", None)
    if not mid or not is_pct_users_metric(mid, catalog=catalog, metrics=metrics):
        return
    days = parse_activation_window_days_from_prompt(prompt)
    if days is not None:
        qo.activation_window_days = days


def effective_activation_window_days(qo: Any) -> int:
    """Days used in SQL/narration; compiler default is 30 when unset."""
    raw = getattr(qo, "activation_window_days", None)
    return int(raw) if raw is not None else 30


def series_label_for_column(col: str, qo: Any = None) -> str:
    """Human label for result columns; honors ``_60d`` suffix and QO window."""
    name = str(col)
    m = _COL_WINDOW_SUFFIX_RE.search(name)
    if m:
        days = int(m.group(1))
        base = name[: m.start()].replace("_", " ").strip().title()
        return f"{base} ({days}-day window)"
    win = effective_activation_window_days(qo) if qo else None
    s = name.replace("_", " ").strip().title()
    if win and win != 30 and "activation" in s.lower():
        return f"{s} ({win}-day window)"
    return s or name


def incomplete_activation_cohort_note(df: pd.DataFrame, qo: Any) -> str:
    """
    Note when recent cohort months have not had the full activation window to mature.
    """
    if df is None or df.empty or not qo:
        return ""
    win = getattr(qo, "activation_window_days", None)
    if not win or int(win) <= 0:
        return ""
    win = int(win)

    month_col = next(
        (c for c in df.columns if str(c).lower() in ("month", "cohort_month", "date")),
        None,
    )
    if not month_col:
        return ""

    today = date.today()
    immature: list[str] = []
    for raw in df[month_col].dropna().unique():
        dt = pd.to_datetime(raw, errors="coerce")
        if pd.isna(dt):
            continue
        y, mo = int(dt.year), int(dt.month)
        last_dom = calendar.monthrange(y, mo)[1]
        cohort_end = date(y, mo, last_dom)
        if cohort_end + timedelta(days=win) > today:
            immature.append(dt.strftime("%b %Y"))

    if not immature:
        return ""
    immature = sorted(set(immature), key=lambda x: pd.to_datetime(x))
    if len(immature) == 1:
        cohorts = immature[0]
    elif len(immature) <= 3:
        cohorts = ", ".join(immature)
    else:
        cohorts = f"{immature[0]}, …, {immature[-1]} ({len(immature)} months)"

    return (
        f"**Initial results:** {cohorts} — the **{win}-day** activation window has "
        f"not fully elapsed for every user in these cohorts; rates may increase as "
        f"more users convert."
    )


def incomplete_retention_cohort_note(df: pd.DataFrame, qo: Any) -> str:
    """
    Note when recent cohort months have not had the full retention window to mature.
    """
    if df is None or df.empty or not qo:
        return ""
    if getattr(qo, "analysis_type", "") != "retention":
        return ""
    win = getattr(qo, "retention_window_days", None)
    if not win or int(win) <= 0:
        return ""
    win = int(win)

    month_col = next(
        (c for c in df.columns if str(c).lower() in ("month", "cohort_month", "date")),
        None,
    )
    if not month_col or "retention_pct" not in df.columns:
        return ""

    today = date.today()
    immature: list[str] = []
    for raw in df[month_col].dropna().unique():
        dt = pd.to_datetime(raw, errors="coerce")
        if pd.isna(dt):
            continue
        y, mo = int(dt.year), int(dt.month)
        last_dom = calendar.monthrange(y, mo)[1]
        cohort_end = date(y, mo, last_dom)
        if cohort_end + timedelta(days=win) > today:
            immature.append(dt.strftime("%b %Y"))

    if not immature:
        return ""
    immature = sorted(set(immature), key=lambda x: pd.to_datetime(x))
    if len(immature) == 1:
        cohorts = immature[0]
    elif len(immature) <= 3:
        cohorts = ", ".join(immature)
    else:
        cohorts = f"{immature[0]}, …, {immature[-1]} ({len(immature)} months)"

    return (
        f"**Cohort maturity:** {cohorts} — the **{win}-day** retention window has "
        f"not fully elapsed; rates for these months may still rise and should not be "
        f"compared directly to fully mature cohorts."
    )


# Back-compat alias for tests
def _catalog_suggested_metrics(catalog: dict) -> list[dict]:
    out: list[dict] = []
    for tname, tdata in catalog.items():
        if tname.startswith("__") or not isinstance(tdata, dict):
            continue
        for m in tdata.get("suggested_metrics") or []:
            if isinstance(m, dict) and (m.get("id") or "").strip():
                out.append(m)
    return out
