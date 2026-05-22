"""
ui/feedback.py — Per-response feedback bar.

Renders: 👍  👎  ✏️ Correct this
State machine per message, keyed by msg_i.

States
------
idle            → show thumb buttons + "Correct this" link
negative_input  → thumbs-down clicked; show text area + submit
correction_input→ ✏️ clicked (thumbs-neutral); show text area + submit
submitted       → thank-you line
"""
from __future__ import annotations

import os
import streamlit as st

from core.memory.chat_history import save_feedback

# ── State helpers ─────────────────────────────────────────────────────────────

def _state(key: str) -> str:
    return st.session_state.get(f"fb_state_{key}", "idle")

def _set_state(key: str, val: str) -> None:
    st.session_state[f"fb_state_{key}"] = val

def _sentiment(key: str) -> str | None:
    return st.session_state.get(f"fb_sent_{key}")

def _set_sentiment(key: str, val: str) -> None:
    st.session_state[f"fb_sent_{key}"] = val


# ── Main render ───────────────────────────────────────────────────────────────

def render_feedback_bar(
    msg_i: int | str,
    msg: dict,
    session_id: str,
) -> None:
    """
    Render a compact feedback bar below an assistant response.

    msg must contain:
        _question     (str)  — the user question this response answered
        turn_id       (int|None)
        _event        (str|None)
        _metric_id    (str|None)
        _analysis_type(str|None)
    """
    key        = str(msg_i)
    question   = msg.get("_question", "")
    turn_id    = msg.get("turn_id")
    event      = msg.get("_event")
    metric_id  = msg.get("_metric_id")
    atype      = msg.get("_analysis_type")
    state      = _state(key)
    sentiment  = _sentiment(key)

    # ── Already submitted ─────────────────────────────────────────────────────
    if state == "submitted":
        icon = "👍" if sentiment == "positive" else "👎"
        st.markdown(
            f'<div class="fb-bar fb-done">'
            f'{icon} &nbsp;Thanks — feedback saved'
            f'</div>',
            unsafe_allow_html=True,
        )
        return

    # ── Idle state — show thumb buttons + correction link ─────────────────────
    if state == "idle":
        st.markdown('<div class="fb-bar">', unsafe_allow_html=True)
        st.markdown('<span class="fb-label">Was this helpful?</span>', unsafe_allow_html=True)

        c1, c2, c3, c4 = st.columns([0.18, 0.18, 0.18, 6])

        with c1:
            if st.button("👍", key=f"fb_pos_{key}", help="Mark as helpful"):
                _save(session_id, question, "positive", event, metric_id, atype, None, turn_id)
                _set_sentiment(key, "positive")
                _set_state(key, "submitted")
                st.rerun()

        with c2:
            if st.button("👎", key=f"fb_neg_{key}", help="Mark as unhelpful"):
                _set_sentiment(key, "negative")
                _set_state(key, "negative_input")
                st.rerun()

        with c3:
            if st.button("✏️", key=f"fb_edit_{key}", help="Correct this response"):
                _set_state(key, "correction_input")
                st.rerun()

        st.markdown('</div>', unsafe_allow_html=True)
        return

    # ── Negative / correction — show inline text form ─────────────────────────
    if state in ("negative_input", "correction_input"):
        placeholder = (
            "What was wrong? e.g. 'Should exclude test users' or 'Wrong metric — use transactions not sessions'"
            if state == "negative_input"
            else "What should the correct answer look like?"
        )
        st.markdown('<div class="fb-bar fb-input">', unsafe_allow_html=True)

        with st.form(key=f"fb_form_{key}", clear_on_submit=True):
            note = st.text_area(
                "Your correction (optional)",
                placeholder=placeholder,
                height=80,
                key=f"fb_note_area_{key}",
                label_visibility="collapsed",
            )
            col_s, col_c = st.columns([1, 5])
            with col_s:
                submitted = st.form_submit_button("Submit", use_container_width=True)
            with col_c:
                cancelled = st.form_submit_button("Cancel", use_container_width=False)

            if submitted:
                sent = "negative" if state == "negative_input" else "correction"
                # Store correction as negative sentiment (actionable for the model)
                _save(session_id, question, "negative", event, metric_id, atype, note or None, turn_id)
                _set_sentiment(key, sent)
                _set_state(key, "submitted")
                st.rerun()

            if cancelled:
                _set_state(key, "idle")
                st.rerun()

        st.markdown('</div>', unsafe_allow_html=True)


# ── DB write ──────────────────────────────────────────────────────────────────

def _save(
    session_id: str,
    question: str,
    sentiment: str,
    event: str | None,
    metric_id: str | None,
    analysis_type: str | None,
    note: str | None,
    turn_id: int | None,
) -> None:
    try:
        save_feedback(
            session_id=session_id,
            question=question,
            sentiment=sentiment,
            event=event,
            metric_id=metric_id,
            analysis_type=analysis_type,
            note=note,
            turn_id=turn_id,
        )
    except Exception:
        pass  # never crash the UI over feedback
