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


_HOURS_RE = re.compile(r"\b(\d+)\s*(?:hr|hrs|hour|hours)\b", re.IGNORECASE)


def _hours_to_days(hours: int) -> int:
    """Convert hours to whole days, rounding up (minimum 1 day)."""
    return max(1, -(-hours // 24))  # ceiling division


def parse_retention_window_days_from_prompt(prompt: str) -> Optional[int]:
    """Extract D{N} retention window (e.g. D7, 7 day retention, for 14 days, 24hr retention)."""
    if not prompt:
        return None
    pl = prompt.lower()
    if not _prompt_has_retention_window_context(pl):
        return None

    # Hours: "24hr retention", "48 hour retention" → convert to days
    m = _HOURS_RE.search(pl)
    if m:
        return _hours_to_days(int(m.group(1)))

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


_WEEK_RE = re.compile(r"\bweek\s*(\d+)\b", re.IGNORECASE)


def parse_retention_week_from_prompt(prompt: str) -> Optional[tuple[int, int]]:
    """
    Detect "week N" retention phrasing and return (from_days, to_days) exclusive.

    Week N covers days (N-1)*7+1 through N*7 inclusive:
      Week 1 → (1, 8),  Week 2 → (8, 15),  Week 3 → (15, 22),  Week 4 → (22, 29)

    Returns None when no week-N pattern is found.
    """
    if not prompt:
        return None
    pl = prompt.lower()
    if "retention" not in pl and "retained" not in pl:
        return None
    m = _WEEK_RE.search(pl)
    if not m:
        return None
    n = int(m.group(1))
    if n < 1:
        return None
    from_days = (n - 1) * 7 + 1
    to_days = n * 7 + 1  # exclusive upper bound → covers days from_days..to_days-1
    return from_days, to_days


def apply_retention_window_from_prompt(
    qo: Any,
    prompt: Optional[str],
    *,
    catalog: Optional[dict] = None,
) -> None:
    """
    Set ``qo.retention_window_days`` (and optionally ``qo.retention_window_from``)
    from the user prompt for retention queries.

    Week-N phrasing is resolved first; explicit D-N / N-day overrides follow.
    Runs after metric hydration so an explicit window overrides catalog defaults.
    """
    if not qo or not prompt:
        return
    if getattr(qo, "analysis_type", "") != "retention":
        return

    week_bounds = parse_retention_week_from_prompt(prompt)
    if week_bounds is not None:
        from_days, to_days = week_bounds
        qo.retention_window_from = from_days
        qo.retention_window_days = to_days
        setattr(qo, "_retention_window_explicit", True)
        return

    days = parse_retention_window_days_from_prompt(prompt)
    if days is not None:
        qo.retention_window_days = days
        setattr(qo, "_retention_window_explicit", True)


def parse_activation_window_days_from_prompt(prompt: str) -> Optional[int]:
    """
    Extract N-day activation window from natural language.

    Handles D7, 7-day, 7 day, and hour-based windows (24hr→1, 48hr→2).
    Skips rolling-window phrases like ``last 7 days``.
    """
    if not prompt:
        return None
    pl = prompt.lower()

    # Hours: "24hr activation", "24 hour conversion" → convert to days
    m = _HOURS_RE.search(pl)
    if m:
        return _hours_to_days(int(m.group(1)))

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
    Note when recent cohort periods have not had the full activation window to mature.

    Handles two modes:
    - Single window: qo.activation_window_days is set (or inferred from columns)
    - Multi-window: columns have _Nd suffixes (e.g. activation_rate_7d, activation_rate_30d)
      → reports per-window which cohorts are still maturing, highest window first.

    Works for both weekly (time_col='week') and monthly (time_col='month') granularities.
    For weekly cohorts, adds 6 days to the maturity threshold because users can join
    any day within the cohort week.
    """
    if df is None or df.empty or not qo:
        return ""

    time_col = next(
        (c for c in df.columns if str(c).lower() in ("week", "month", "cohort_week", "cohort_month", "date")),
        None,
    )
    if not time_col:
        return ""

    is_weekly = str(time_col).lower() in ("week", "cohort_week") or (
        getattr(qo, "time_granularity", "day") or "day"
    ).lower() == "week"
    # Users can join any day within a cohort period; add the period span to the threshold
    cohort_span_days = 6 if is_weekly else calendar.monthrange(
        date.today().year, date.today().month
    )[1] - 1

    # Detect windows: from qo or from column _Nd suffixes (multi-window case)
    win_raw = getattr(qo, "activation_window_days", None)
    if win_raw and int(win_raw) > 0:
        windows = [int(win_raw)]
    else:
        windows = sorted(set(
            int(m.group(1))
            for c in df.columns
            for m in [_COL_WINDOW_SUFFIX_RE.search(str(c))]
            if m
        ))
        if not windows:
            return ""

    today = date.today()
    period_label = "week" if is_weekly else "month"
    date_fmt = "%b %d" if is_weekly else "%b %Y"

    if len(windows) == 1:
        win = windows[0]
        immature: list[str] = []
        for raw in df[time_col].dropna().unique():
            dt = pd.to_datetime(raw, errors="coerce")
            if pd.isna(dt):
                continue
            cohort_start = dt.date()
            if cohort_start + timedelta(days=cohort_span_days + win) > today:
                immature.append(dt.strftime(date_fmt))
        if not immature:
            return ""
        immature = sorted(set(immature), key=lambda x: pd.to_datetime(x))
        cohorts = (
            immature[0] if len(immature) == 1
            else ", ".join(immature) if len(immature) <= 3
            else f"{immature[0]}, …, {immature[-1]} ({len(immature)} {period_label}s)"
        )
        return (
            f"**Maturing cohorts:** {cohorts} — the **{win}-day** activation window has "
            f"not fully elapsed; rates will increase as more users convert."
        )

    # Multi-window: report per window which cohorts are still open
    notes: list[str] = []
    for win in sorted(windows, reverse=True):
        immature_dates: list[date] = []
        for raw in df[time_col].dropna().unique():
            dt = pd.to_datetime(raw, errors="coerce")
            if pd.isna(dt):
                continue
            cohort_start = dt.date()
            if cohort_start + timedelta(days=cohort_span_days + win) > today:
                immature_dates.append(cohort_start)
        if not immature_dates:
            continue
        immature_dates = sorted(set(immature_dates))
        n = len(immature_dates)
        first_fmt = immature_dates[0].strftime(date_fmt)
        if n == 1:
            notes.append(f"**{win}d window**: {first_fmt} still maturing")
        else:
            notes.append(f"**{win}d window**: last {n} {period_label}s (from {first_fmt}) still maturing")

    if not notes:
        return ""
    return (
        f"**⚠️ Cohort maturity — rates below are not directly comparable:**\n"
        + "\n".join(f"- {n}" for n in notes)
        + f"\n\nRecent {period_label}s haven't had the full window to convert. "
        f"Compare only mature cohorts for a valid trend."
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
