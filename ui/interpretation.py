"""ui/interpretation.py — User-visible interpretation panel + refine chips (Omni-style trust UI)."""

from __future__ import annotations

import html
import hashlib

import streamlit as st

from ui.cohort_labels import activity_cohort_label, anchor_noun_phrase


def interpretation_light_html(contract: dict) -> str:
    """
    Light-theme interpretation panel for the main response card (readable on white).
    All dynamic values are HTML-escaped.
    """
    flt = contract.get("filters") or {}
    filt_line = (
        ", ".join(f"{html.escape(str(k))}={html.escape(str(v))}" for k, v in flt.items())
        if flt
        else "None"
    )
    fs = contract.get("funnel_steps") or []
    funnel_line = " → ".join(html.escape(str(s).replace("_", " ")) for s in fs) if fs else "—"
    at_raw_pre = str(contract.get("analysis") or "").strip().lower()
    if at_raw_pre == "same_month_anchor":
        subj = activity_cohort_label(
            event=contract.get("event"),
            filters=contract.get("filters") or {},
        )
        anc_p = anchor_noun_phrase(contract.get("event_b"))
        ev_a = html.escape(subj)
        ev_b_raw = contract.get("event_b") or ""
        ev_b_tech = html.escape(str(ev_b_raw).replace("_", " ")) if ev_b_raw else ""
        goal = f' · anchor <b>{html.escape(anc_p)}</b> <span style="color:#94a3b8">({ev_b_tech})</span>' if ev_b_raw else ""
        _cohort_metric = subj
    else:
        ev_a = html.escape(str(contract.get("event") or "—").replace("_", " "))
        ev_b_raw = contract.get("event_b") or ""
        ev_b = html.escape(str(ev_b_raw).replace("_", " ")) if ev_b_raw else ""
        goal = f' · goal <b>{ev_b}</b>' if ev_b_raw else ""
        _cohort_metric = None
    metric_line = html.escape(
        str(_cohort_metric or contract.get("metric") or contract.get("metric_id") or "—")
    )
    analysis = html.escape(str(contract.get("analysis") or "—"))
    tw = html.escape(str(contract.get("time_window") or "—"))
    bd = contract.get("breakdown")
    bd_line = html.escape(str(bd).replace("_", " ")) if bd else "—"
    rw = contract.get("retention_window_days")
    rw_line = ""
    if rw is not None and str(contract.get("analysis") or "").lower() == "retention":
        rw_line = (
            f'<div><span style="color:#64748b">Retention window</span> · '
            f'<b>{html.escape(str(rw))}</b> days</div>'
        )
    ts = contract.get("time_source")
    ts_line = ""
    if ts:
        ts_line = (
            f'<div><span style="color:#64748b">Time source</span> · '
            f'<b>{html.escape(str(ts))}</b></div>'
        )
    at_raw = str(contract.get("analysis") or "").strip().lower()
    same_month_note = ""
    if at_raw == "same_month_anchor":
        anc_plain = anchor_noun_phrase(contract.get("event_b"))
        evb_disp = html.escape(anc_plain)
        same_month_note = (
            '<div style="margin-top:0.45rem;padding-top:0.5rem;border-top:1px solid #e2e8f0;'
            'font-size:0.82rem;color:#475569;line-height:1.45">'
            "<b>How this can be computed</b> · <b>(1) User table</b> — a single onboarding date "
            "(or equivalent) on a <b>users</b> / profile table is often the most stable definition "
            "when your semantic layer exposes it. <b>(2) Events</b> — infer the anchor from the "
            "event stream (first <b>"
            f"{evb_disp}"
            "</b>); the quick compiler path uses this when no user-level onboarding "
            "column is wired into the agent.</div>"
        )
    return f"""
<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:0.85rem 1rem;margin:0.5rem 0 0.75rem;font-size:0.88rem;line-height:1.5;color:#334155;">
<div style="font-size:0.65rem;letter-spacing:0.06em;color:#64748b;text-transform:uppercase;margin-bottom:0.35rem;font-weight:600">How we interpreted this</div>
<div><span style="color:#64748b">Analysis</span> · <b>{analysis}</b></div>
<div><span style="color:#64748b">Metric</span> · <b>{metric_line}</b></div>
<div><span style="color:#64748b">Subject</span> · <b>{ev_a}</b>{goal}</div>
<div><span style="color:#64748b">Breakdown</span> · <b>{bd_line}</b></div>
<div><span style="color:#64748b">Window</span> · {tw}</div>
{ts_line}
{rw_line}
<div><span style="color:#64748b">Filters</span> · {filt_line}</div>
<div><span style="color:#64748b">Funnel</span> · {funnel_line}</div>
{same_month_note}
</div>
"""


def _breakdown_followup_prompt(qo, d_disp: str) -> str:
    at = (getattr(qo, "analysis_type", None) or "").strip().lower()
    if at in ("funnel", "funnel_compare"):
        return f"Show the same funnel broken down by {d_disp}"
    if at == "retention":
        return f"Show retention broken down by {d_disp}"
    if at == "journey":
        return f"Show the same journey broken down by {d_disp}"
    return f"Show the same metric broken down by {d_disp}"


def build_refine_suggestions(qo, sampled_values: dict[str, dict[str, list]]) -> list[tuple[str, str]]:
    """
    Return up to 6 (label, follow-up question) pairs for one-click refine.
    Questions are phrased as standalone follow-ups so orchestrator history can resolve context.
    """
    if qo is None:
        return []
    at = (getattr(qo, "analysis_type", None) or "").strip().lower()
    if at in ("clarify", "out_of_scope"):
        return []

    chips: list[tuple[str, str]] = []
    ev = (sampled_values or {}).get("events") or {}
    current_bd = (getattr(qo, "breakdown", None) or "").strip().lower()

    preferred = (
        "platform",
        "transaction_channel",
        "city",
        "account_type",
        "device_type",
        "payment_method",
    )
    dims: list[str] = []
    for p in preferred:
        pl = p.lower()
        if pl == current_bd:
            continue
        vals = ev.get(p)
        if vals and 2 <= len(vals) <= 20:
            dims.append(p)
    for col, vals in sorted(ev.items(), key=lambda x: x[0]):
        cl = str(col).strip().lower()
        if cl == current_bd or col in dims:
            continue
        if vals and 2 <= len(vals) <= 15:
            dims.append(col)
        if len(dims) >= 8:
            break

    if at in (
        "metric",
        "segment",
        "funnel",
        "funnel_compare",
        "retention",
        "behavioral_cohort",
        "journey",
        "",
    ):
        for d in dims[:3]:
            d_disp = d.replace("_", " ")
            chips.append((f"By {d_disp}", _breakdown_followup_prompt(qo, d_disp)))

    trd = int(getattr(qo, "time_range_days", 30) or 30)
    if trd <= 30:
        chips.append(("Last 90 days", "Show the same view for the last 90 days"))
    if trd <= 90:
        chips.append(("Last 12 months", "Show the same view for the last 12 months"))

    gran = (getattr(qo, "time_granularity", None) or "day").lower()
    if gran != "month":
        if at in ("funnel", "funnel_compare"):
            chips.append(("Month over month", "Month over month comparison for the same funnel"))
        elif at == "retention":
            chips.append(("Month over month", "Month over month retention trend"))
        else:
            chips.append(("Month over month", "Month over month trend for the same metric"))
    if gran != "week":
        if at in ("funnel", "funnel_compare"):
            chips.append(("Weekly trend", "Weekly trend for the same funnel"))
        elif at == "retention":
            chips.append(("Weekly trend", "Weekly retention trend"))
        else:
            chips.append(("Weekly trend", "Weekly trend for the same metric"))

    if at == "segment" and current_bd:
        chips.append(("Remove breakdown", "Show the same without segment breakdown — overall trend only"))

    # ── Analyst-style "what next?" suggestions ────────────────────────────────
    # These sit before dimension/time chips so the most insightful options
    # appear first.
    analyst_chips: list[tuple[str, str]] = []
    ev_display = (getattr(qo, "event", None) or "this event").replace("_", " ")

    if at in ("metric", "segment"):
        analyst_chips.append((
            "Why did this change?",
            f"Why did {ev_display} change over this period? Diagnose the drivers.",
        ))
    if at == "metric":
        analyst_chips.append((
            "Which group drives this?",
            f"Which segment contributes most to {ev_display}?",
        ))
    if at in ("metric", "segment", "funnel"):
        analyst_chips.append((
            "Compare to prior period",
            "Show the same metric compared month over month",
        ))
    if at == "funnel":
        analyst_chips.append((
            "Break down drop-off",
            "For the funnel step with the worst drop-off, break down by platform",
        ))
        analyst_chips.append((
            "Why did conversion change?",
            "Why did overall funnel conversion change over this period?",
        ))
    if at == "behavioral_cohort":
        analyst_chips.append((
            "What do these users do next?",
            f"What events do users do after {ev_display}?",
        ))
    if at == "retention":
        # Window alternatives — show the windows the user hasn't seen yet
        current_win = int(getattr(qo, "retention_window_days", 7) or 7)
        _win_labels = {1: "D1", 7: "D7", 30: "D30"}
        for w, lbl in _win_labels.items():
            if w != current_win:
                analyst_chips.append((
                    f"{lbl} retention",
                    f"Show {lbl} retention for the same period",
                ))
        analyst_chips.append((
            "Which cohort retains best?",
            "Which acquisition cohort has the highest retention rate?",
        ))
        analyst_chips.append((
            "Retention by platform",
            "Show retention broken down by platform",
        ))
    if at == "metric" and "activation" in (getattr(qo, "metric_id", "") or "").lower():
        current_win = getattr(qo, "activation_window_days", None)  # None = lifetime
        for w, lbl in ((1, "D1"), (7, "D7"), (30, "D30")):
            if current_win != w:  # None (lifetime) is always different from any int
                analyst_chips.append((
                    f"{lbl} activation",
                    f"Show monthly activation rate trend with {w}-day conversion window",
                ))
        analyst_chips.append((
            "Activation funnel",
            "Show step-by-step onboarding to first transaction funnel",
        ))
    if at == "user_lifecycle":
        analyst_chips.append((
            "What drives active users?",
            "What distinguishes active users from at-risk users?",
        ))
    if at == "journey":
        analyst_chips.append((
            "Funnel these steps",
            f"Show the funnel conversion for the top steps users take after {ev_display}",
        ))

    chips = analyst_chips + chips

    # De-dupe by question text, cap length
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for lbl, q in chips:
        qn = q.strip()
        if not qn or qn in seen:
            continue
        seen.add(qn)
        out.append((lbl, qn))
        if len(out) >= 6:
            break
    return out


def refine_chips_to_json(chips: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"label": a, "query": b} for a, b in chips]


def render_refine_chips(chips: list[tuple[str, str]], *, key_prefix: str) -> None:
    return  # hidden temporarily
    st.markdown(
        '<div style="font-size:0.72rem;font-weight:600;color:#64748b;'
        'text-transform:uppercase;letter-spacing:0.06em;margin:0.75rem 0 0.4rem">'
        "Refine</div>",
        unsafe_allow_html=True,
    )
    cols = st.columns(min(len(chips), 3))
    h = hashlib.sha256(key_prefix.encode()).hexdigest()[:10]
    for i, (lbl, query) in enumerate(chips):
        with cols[i % 3]:
            if st.button(lbl, key=f"refine_{h}_{i}_{lbl[:12]}", use_container_width=True):
                st.session_state["pending"] = query
                st.rerun()


def render_refine_chips_from_saved(saved: list[dict], *, key_prefix: str) -> None:
    chips = [(x.get("label") or "", x.get("query") or "") for x in (saved or [])]
    chips = [(a, b) for a, b in chips if a and b]
    render_refine_chips(chips, key_prefix=key_prefix)
