"""
Human-readable cohort labels from QueryObject / contract dicts.

Used for same_month_anchor (and similar) so summaries and chart titles match
how users speak ("UPI transacting users") instead of raw event_name tokens.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


# Human-readable labels for IS NOT NULL / IS NULL sentinel values, keyed by (col, sentinel).
# Falls back to the generic table when no column-specific entry exists.
_SENTINEL_BY_COL: dict[tuple[str, str], str] = {
    ("transaction_channel", "__IS_NOT_NULL__"): "In App",
    ("transaction_channel", "__IS_NULL__"):     "Off App",
}
_SENTINEL_GENERIC: dict[str, str] = {
    "__IS_NOT_NULL__": "non-null",
    "__IS_NULL__":     "null",
}


def _translate_sentinel(col: str, val: str) -> str:
    """Return a display-safe label for IS_NOT_NULL / IS_NULL sentinels, or val unchanged."""
    return (
        _SENTINEL_BY_COL.get((col, val))
        or _SENTINEL_GENERIC.get(val)
        or val
    )


def _norm_filters(f: Any) -> dict[str, str]:
    if not isinstance(f, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in f.items():
        if v is None:
            continue
        col = str(k).lower()
        out[col] = _translate_sentinel(col, str(v).strip())
    return out


def filter_qualifier_prefix(qo: Any) -> str:
    """
    Short qualifier word derived from filters for use in metric labels.
    e.g. {transaction_channel: "__IS_NOT_NULL__"} → "In App"
         {transaction_channel: "UPI"}             → "UPI"
    Returns empty string when no meaningful prefix can be derived.
    """
    nf = _norm_filters(getattr(qo, "filters", None) if qo else None)
    channel = nf.get("transaction_channel")
    if channel:
        return channel
    # Other single-value non-geo filters
    skip = frozenset({"date", "event_date", "dt", "day"}) | _GEO_FILTER_KEYS
    for k, v in sorted(nf.items()):
        if k not in skip and v:
            return v
    return ""


_GEO_FILTER_KEYS = frozenset(
    {"city", "user_city", "state", "region", "country", "metro", "location"}
)


def _geo_tail(nf: dict[str, str]) -> str:
    """Append ' in Mumbai' style phrase from city / state filters."""
    city = nf.get("city") or nf.get("user_city")
    state = nf.get("state") or nf.get("region")
    parts: list[str] = []
    if city:
        parts.append(str(city).strip())
    if state and str(state).strip().lower() != str(city or "").strip().lower():
        parts.append(str(state).strip())
    if not parts:
        return ""
    return " in " + " · ".join(parts)


def _event_title(ev: Optional[str]) -> str:
    if not ev:
        return "Activity"
    return str(ev).replace("_", " ").strip().title()


def anchor_noun_phrase(event_b: Optional[str]) -> str:
    """Short noun phrase for lifecycle anchor (sentence case)."""
    eb = (event_b or "").strip()
    if eb == "onboarding_completed":
        return "onboarding"
    if eb == "account_created":
        return "account creation"
    if eb == "app_opened":
        return "first app open"
    return _event_title(eb).lower()


def cohort_slice_filters_present(qo: Any) -> bool:
    """
    True when ``filters`` carry dimensions that should override a generic
    catalog metric display name in summaries (channel, status, geo, etc.).
    """
    if not qo:
        return False
    nf = _norm_filters(getattr(qo, "filters", None))
    slice_keys = (
        {"transaction_channel", "transaction_status"} | _GEO_FILTER_KEYS
    )
    return bool(set(nf) & slice_keys)


def activity_cohort_label(
    *,
    event: Optional[str] = None,
    filters: Optional[Mapping[str, Any]] = None,
    qo: Any = None,
) -> str:
    """
    User-facing label for the *activity* cohort (the primary ``event`` slice).

    Examples:
      - transaction_reconciled + transaction_channel=UPI + SUCCESS → "UPI transacting users"
      - transaction_reconciled + SUCCESS only → "Transacting users"
      - Other events + equality filters → compact filter summary + "users"
    """
    if qo is not None:
        event = getattr(qo, "event", None)
        filters = getattr(qo, "filters", None) or {}
    nf = _norm_filters(filters or {})
    ovr = getattr(qo, "_same_month_activity_label", None) if qo is not None else None
    if ovr and str(ovr).strip():
        return f"{str(ovr).strip()}{_geo_tail(nf)}"
    ev = (event or "").strip()

    channel = nf.get("transaction_channel")
    status_u = (nf.get("transaction_status") or "").upper()

    if ev == "transaction_reconciled":
        geo = _geo_tail(nf)
        successish = status_u in ("", "SUCCESS")
        if channel and successish:
            return f"{channel} transacting users{geo}"
        if channel:
            return f"{channel} transaction users{geo}"
        if status_u == "SUCCESS":
            return f"Transacting users{geo}"
        if status_u:
            return f"{status_u.title()} transaction users{geo}"
        if geo:
            return f"{_event_title(ev)} users{geo}"

    bits: list[str] = []
    skip = frozenset(
        {"date", "event_date", "dt", "day", "event_name", "timestamp"}
    ) | _GEO_FILTER_KEYS
    for k, v in sorted(nf.items()):
        if k in skip or not v:
            continue
        bits.append(str(v))
    evd = _event_title(ev)
    geo = _geo_tail(nf)
    if bits:
        return f"{' · '.join(bits)} {evd} users{geo}"
    return f"{evd} users{geo}"


def same_month_anchor_chart_title_parts(
    *,
    event: Optional[str] = None,
    event_b: Optional[str] = None,
    filters: Optional[Mapping[str, Any]] = None,
    calendar_month: str = "",
    qo: Any = None,
) -> str:
    """
    One-line chart title: calendar month + cohort label + anchor timing wording.
    """
    if qo is not None:
        event = getattr(qo, "event", None)
        event_b = getattr(qo, "event_b", None)
        filters = getattr(qo, "filters", None) or {}
    subj = activity_cohort_label(event=event, filters=filters)
    anc = anchor_noun_phrase(event_b)
    head = f"{calendar_month} — " if (calendar_month or "").strip() else ""
    return (
        f"{head}{subj} by {anc} timing "
        f"(same month vs earlier vs missing)"
    )
