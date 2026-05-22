"""
chat.py — Analytics chat agent over jupiter.duckdb
Run: streamlit run chat.py
"""
from __future__ import annotations

import json
import os
import re
import traceback
from datetime import date
from pathlib import Path
from typing import Optional

import altair as alt
import duckdb
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv(override=True)  # must run before core imports so LLM_STRONG/LLM_FAST resolve correctly

# Streamlit Cloud: secrets.toml → environment (load_dotenv does not read st.secrets)
try:
    import streamlit as _st_boot

    for _k in (
        "LLM_PROVIDER", "GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "XAI_API_KEY", "CEREBRAS_API_KEY", "SAMBANOVA_API_KEY",
        "LLM_STRONG_MODEL", "LLM_MEDIUM_MODEL", "LLM_FAST_MODEL",
    ):
        if _k in _st_boot.secrets and _k not in os.environ:
            os.environ[_k] = str(_st_boot.secrets[_k])
except Exception:
    pass

from openai import OpenAI

from core.pipeline.pre_check import check_out_of_scope
from core.pipeline.orchestrator import orchestrate
from core.sql.compilers import (
    compile_query,
    compile_custom_event_split,
    compile_custom_event_single,
    compile_custom_event_segment,
    compile_custom_event_retention,
    set_global_sql_guards,
)
from core.semantic.resolver_policy import resolve_query_policy
from core.analysis.diagnose import run_diagnosis
from core.analysis.analyst import investigate, AnalystReport
try:
    from core.agents.hypothesis_agent import generate_hypotheses, generate_deep_plan
except ImportError:
    from core.agents.hypothesis_agent import generate_hypotheses, HypothesisDoc

    def generate_deep_plan(question: str, catalog: dict, openai_api_key=None, playbook=None):
        return HypothesisDoc(question=question, analysis_type_hint="")
from core.semantic.playbooks import PlaybookRegistry
from core.analysis.deep_analysis import run_parallel_investigations, SubQueryResult
from core.agents.story_architect import build_story_arc, build_deep_story_arc, DeepStoryArc
from core.agents.policy_arbiter import arbitrate
from core.semantic.qo_lineage import materialize_rollups_on_qo
from core.sql.result_validator_runtime import validate_result_shape
from ui.chart_display import display_evidence_chart
from core.memory.chat_history import (
    init_db, new_session, get_session, list_sessions,
    save_turn, load_turns, get_qo_history, turns_to_messages,
    get_narrative_thread,
)

from ui.qo_fixups import (
    _apply_time_intent_overrides,
    _hydrate_retention_event_from_metric,
    _apply_metric_variant_overrides,
    _sanitize_invalid_breakdown,
    _apply_followup_context_repair,
)
from ui.format_display import format_rate_columns_for_display
from ui.pipeline import (
    ask_llm, get_sql, run_sql, run_sql_with_retry, fix_sql, narrate,
    _extract_clarify_context, should_generate_hypotheses, should_run_deep_analysis,
    _period_hint_from_qo, _contextual_metric_label, _scalar_summary_agent,
    _looks_like_rate_metric, _fmt_period_value, _build_time_series_summary,
    _preferred_metric_column, small_narration, evidence_chart_title_for_qo,
    DEFAULT_USE_POLICY_ARBITER, DEFAULT_HYPOTHESIS_ONLY_DIAGNOSE, DEFAULT_DEEP_ANALYSIS_AUTO,
)
from ui.debug_panel import (
    _canonical_metric_contract, _format_time_window_phrase,
    _build_public_answer_contract, _answer_contract_html,
    _render_answer_contract_section, _debug_html_escape, _debug_trunc,
    _debug_kv, _debug_step_html, _debug_candidates_table,
    _build_full_debug_signature, _finalize_debug_signature_outputs,
    _render_semantics_debug_panel, _render_debug_summary_v2,
    _sql_for_debug_panel, _flush_assistant_debug_panel, _render_debug_pipeline,
)
from ui.legacy_charts import (
    _PALETTE, _PALETTE_DUAL, _FONT, _BG_DARK, _GRID_DARK, _LABEL_DARK,
    _AXIS_DARK, _BG, _GRID_COL, _LABEL_COL, _AXIS_CFG,
    _dark, _light, _base, _is_temporal_col, _should_show_chart, auto_chart,
    _AXIS_LIGHT,
)
from ui.styles import inject_styles
from ui.feedback import render_feedback_bar
from ui.render import (
    _render_scalar_metric, _is_scalar_like, _render_retention_matrix,
    _is_retention_matrix_df, _render_investigation, _render_dim_grid,
    _render_deep_report, _render_analyst_report,
)
from ui.doc_upload import render_doc_upload
from ui.interpretation import (
    build_refine_suggestions,
    refine_chips_to_json,
    render_refine_chips,
    render_refine_chips_from_saved,
)

from core.infra.logger import setup_logging as _setup_logging
_setup_logging(log_file="agent.log")

DB_PATH           = Path(__file__).parent / "jupiter.duckdb"
CATALOG_PATH      = Path(__file__).parent / "catalog.json"
PLAYBOOKS_DIR     = Path(__file__).parent / "playbooks"
PLAYBOOK_REGISTRY = PlaybookRegistry(PLAYBOOKS_DIR)
init_db()   # ensure schema exists

st.set_page_config(page_title="Analytics Agent", page_icon="📊", layout="wide")

# ── Data loading ──────────────────────────────────────────────────────────────

SKIP_VALUE_COLS = {"user_id", "session_id", "event_id", "transaction_id",
                   "device_id", "utr", "campaign_id", "merchant_name",
                   "ifsc_code", "source_vpa", "beneficiary_vpa", "npci_error_code"}


def load_schema_and_metrics():
    catalog = json.loads(CATALOG_PATH.read_text())

    # Schema from actual DB columns — ground truth
    conn   = duckdb.connect(str(DB_PATH), read_only=True)
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).df()["table_name"].tolist()

    # Pre-fetch distinct values for low-cardinality VARCHAR columns
    def _sample_values(tname: str, col: str) -> list[str]:
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {col} FROM {tname} WHERE {col} IS NOT NULL LIMIT 20"
            ).df()[col].tolist()
            return [str(v) for v in rows if v]
        except Exception:
            return []

    sampled_values: dict[str, dict[str, list]] = {}   # {table: {col: [vals]}}

    parts = []
    for tname in tables:
        cols_df = conn.execute(f"DESCRIBE {tname}").df()
        tdata   = catalog.get(tname, {})
        events  = tdata.get("event_semantics", {})
        event_block = ""
        if events:
            names = ", ".join(f"'{e}'" for e in events.keys())
            event_block = f"\n  -- VALID event_name values: {names}"

        col_lines = []
        sampled_values[tname] = {}
        for _, row in cols_df.iterrows():
            cname = row["column_name"]
            ctype = row["column_type"]
            hint  = ""
            if ctype == "VARCHAR" and cname not in SKIP_VALUE_COLS:
                vals = _sample_values(tname, cname)
                sampled_values[tname][cname] = vals
                if 1 < len(vals) <= 15:          # only useful for low-cardinality cols
                    hint = f"  -- values: {', '.join(repr(v) for v in sorted(vals))}"
            col_lines.append(f"  {cname}  {ctype}{hint}")

        desc = tdata.get("table_description", "")
        parts.append(
            f"Table: {tname}" + (f"  -- {desc}" if desc else "")
            + event_block + "\n" + "\n".join(col_lines)
        )
    conn.close()

    schema = "\n\n".join(parts)

    metrics = []
    for tname, tdata in catalog.items():
        if tname.startswith("__"):
            continue
        for m in tdata.get("suggested_metrics", []):
            if m.get("status") not in ("approved", None, ""):
                continue
            metrics.append({
                "id":               m.get("id", ""),
                "name":             m["name"],
                "description":      m.get("description", ""),
                "sql":              m.get("sql_hint", ""),
                "type":             m.get("type"),
                "builder_definition": m.get("builder_definition"),
            })

    # Include Business Context custom events as executable pseudo-metrics so
    # natural-language queries can target them by name in chat.
    biz = catalog.get("__business_context__", {}) or {}
    custom_events = biz.get("custom_events", []) or []
    for ce in custom_events:
        ce_name = (ce.get("name") or "").strip()
        ce_sql = (ce.get("sql") or "").strip()
        if not ce_name or not ce_sql:
            continue
        ce_id = f"ce_{re.sub(r'[^a-z0-9_]+', '_', ce_name.lower()).strip('_')}"
        metrics.append({
            "id": ce_id,
            "name": ce_name.replace("_", " ").title(),
            "description": ce.get("description", f"Custom event: {ce_name}"),
            # Keep as non-CTE single-SELECT so time filter injection can work.
            "sql": f"SELECT COUNT(DISTINCT user_id) AS value FROM events WHERE ({ce_sql})",
        })

    return schema, metrics, catalog, sampled_values


# Cache once per session
if "schema" not in st.session_state:
    with st.spinner("Loading schema..."):
        (st.session_state["schema"],
         st.session_state["metrics"],
         st.session_state["catalog"],
         st.session_state["sampled_values"]) = load_schema_and_metrics()


SCHEMA         = st.session_state["schema"]
METRICS        = st.session_state["metrics"]
CATALOG        = st.session_state["catalog"]
SAMPLED_VALUES = st.session_state["sampled_values"]

# Enforce business-level exclusions (e.g. test/internal user removal) in SQL compilers.
_bctx = CATALOG.get("__business_context__", {}) if isinstance(CATALOG, dict) else {}
_always_filters = ((_bctx.get("exclusions") or {}).get("always_filter") or [])
set_global_sql_guards(_always_filters)


inject_styles()

# ── Session management ────────────────────────────────────────────────────────
# Session identity lives in the URL (?session=<id>) so it survives page refresh.

if "session_id" not in st.session_state:
    params = st.query_params
    sid = params.get("session")
    if sid and get_session(sid):
        # Restore an existing session from SQLite
        turns = load_turns(sid)
        st.session_state["session_id"]  = sid
        st.session_state["messages"]    = turns_to_messages(turns)
    else:
        # Brand-new session
        sid = new_session()
        st.session_state["session_id"] = sid
        st.session_state["messages"]   = []
        st.query_params["session"] = sid

SESSION_ID = st.session_state["session_id"]


def _attach_and_render_refine_chips(assistant_msg: dict, qo, key_suffix: str) -> None:
    if not qo:
        return
    chips = build_refine_suggestions(qo, SAMPLED_VALUES)
    if not chips:
        return
    assistant_msg["refine_chips"] = refine_chips_to_json(chips)
    render_refine_chips(chips, key_prefix=f"{SESSION_ID}_{key_suffix}")


def _analysis_quality_clarify_message(qo, df: pd.DataFrame) -> str | None:
    """
    Guardrail for low-quality outputs where user intent is likely under-specified.
    Runs per analysis type with type-appropriate checks.
    """
    if qo is None or df is None or df.empty:
        return None

    at = str(getattr(qo, "analysis_type", "") or "").lower()

    # ── Segment quality: breakdown dimension is empty/degenerate ─────────────
    if at == "segment":
        b = str(getattr(qo, "breakdown", "") or "").strip()
        if not b or b not in df.columns:
            return None
        vals = (
            df[b]
            .dropna()
            .astype(str)
            .str.strip()
            .replace({"": pd.NA, "None": pd.NA, "none": pd.NA, "NULL": pd.NA, "null": pd.NA, "nan": pd.NA})
            .dropna()
        )
        if not vals.empty:
            # If we have at least 2 meaningful buckets, the breakdown is usable.
            if int(vals.nunique(dropna=True)) >= 2:
                return None
            # One concrete non-empty bucket is still okay if totals were filtered intentionally.
            if int(vals.nunique(dropna=True)) == 1 and str(vals.iloc[0]).lower() not in {"none", "null", "nan"}:
                return None

        # Build suggestions from low-cardinality event dimensions.
        event_dims = (SAMPLED_VALUES.get("events", {}) or {})
        hints = []
        preferred = ("platform", "device_type", "device_os", "manufacturer", "model", "transaction_channel", "account_type")
        for d in preferred:
            if d == b:
                continue
            vv = event_dims.get(d) or []
            if 2 <= len(vv) <= 25:
                hints.append(d)
        if len(hints) < 4:
            for d, vv in event_dims.items():
                dl = str(d).lower()
                if d == b or d in hints:
                    continue
                if any(k in dl for k in ("device", "platform", "os", "channel", "type")) and 2 <= len(vv) <= 25:
                    hints.append(d)
                if len(hints) >= 6:
                    break
        hint_text = ", ".join(hints[:4]) if hints else "platform, device_type, transaction_channel"
        return (
            f"I mapped this split to **{b}**, but that field is mostly empty in this result. "
            f"Did you mean one of these dimensions: **{hint_text}**?"
        )

    # ── Funnel quality: need at least 2 usable steps ─────────────────────────
    if at in {"funnel", "funnel_compare"}:
        if "step_name" in df.columns:
            step_vals = (
                df["step_name"]
                .dropna()
                .astype(str)
                .str.strip()
                .replace({"": pd.NA, "None": pd.NA, "null": pd.NA})
                .dropna()
            )
            if int(step_vals.nunique(dropna=True)) < 2:
                return (
                    "I found too few valid funnel steps to give a reliable drop-off view. "
                    "Could you specify the exact step sequence (for example: app_opened → transaction_initiated → transaction_reconciled)?"
                )
        return None

    # ── Journey quality: next_event should have multiple meaningful values ───
    if at == "journey" and "next_event" in df.columns:
        next_vals = (
            df["next_event"]
            .dropna()
            .astype(str)
            .str.strip()
            .replace({"": pd.NA, "None": pd.NA, "null": pd.NA, "(not set)": pd.NA})
            .dropna()
        )
        if int(next_vals.nunique(dropna=True)) < 2:
            return (
                "I couldn't extract a meaningful next-step path distribution from this setup. "
                "Please specify a clearer starting event (and optional goal event) for the journey."
            )
        return None

    # ── Retention quality: retention % should be present and in-range ────────
    if at == "retention":
        pct_cols = [c for c in df.columns if "retention" in str(c).lower() and "pct" in str(c).lower()]
        if pct_cols:
            s = pd.to_numeric(df[pct_cols[0]], errors="coerce").dropna()
            if s.empty:
                return "I couldn't compute a valid retention percentage for this setup. Should I switch to D1, D7, or D30 retention?"
            if ((s < 0) | (s > 100)).any():
                return (
                    "The retention output looks out of expected range (0–100%). "
                    "Please confirm the retention definition/window and any mandatory filters."
                )
        return None

    # ── Behavioral cohort trend quality ───────────────────────────────────────
    if at == "behavioral_cohort":
        gran = str(getattr(qo, "time_granularity", "") or "").lower()
        if gran in {"week", "month"}:
            tcol = "week" if gran == "week" else "month"
            if tcol in df.columns and int(df[tcol].nunique(dropna=True)) <= 1 and len(df) <= 1:
                return (
                    "This cohort trend has only one time bucket, so movement is not interpretable. "
                    "Would you like a wider time range?"
                )
        return None

    # default: no extra clarify
    return None


# ── Sidebar: session history ──────────────────────────────────────────────────

with st.sidebar:
    # ── Brand / logo ──────────────────────────────────────────────────────
    st.markdown("""
<div class="sb-brand">
  <div class="sb-brand-icon">⬡</div>
  <div>
    <div class="sb-brand-name">Analytics Agent</div>
    <div class="sb-brand-sub">Analytics Agent · Data</div>
  </div>
</div>
""", unsafe_allow_html=True)

    # ── Top nav ───────────────────────────────────────────────────────────
    if st.button("＋  New Analysis", use_container_width=True,
                 help="Start a new analysis session"):
        sid = new_session()
        st.query_params["session"] = sid
        for key in ("session_id", "messages", "uc_filter"):
            st.session_state.pop(key, None)
        st.rerun()

    st.markdown('<div style="height:0.3rem"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sb-section-label">Recent analyses</div>', unsafe_allow_html=True)

    # ── Session list ──────────────────────────────────────────────────────
    recent = list_sessions(15)
    for s in recent:
        label = s.get("title") or "New analysis"
        is_active = s["session_id"] == SESSION_ID
        display = label[:34] + "…" if len(label) > 36 else label
        ts = s["updated_at"][:10] if s.get("updated_at") else ""
        active_cls = "sb-session-active" if is_active else ""
        active_badge = '<span class="sb-active-badge">ACTIVE</span>' if is_active else ""
        st.markdown(
            f'<div class="sb-session-item {active_cls}">'
            f'<div style="display:flex;align-items:center;gap:0.2rem">'
            f'<div class="sb-session-title">{display}</div>'
            f'{active_badge}</div>'
            f'<div class="sb-session-date">{ts}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if not is_active:
            if st.button("Open", key=f"open_{s['session_id']}", use_container_width=True):
                st.query_params["session"] = s["session_id"]
                for key in ("session_id", "messages"):
                    st.session_state.pop(key, None)
                st.rerun()

    # ── Business document upload ──────────────────────────────────────────
    st.markdown('<div style="height:0.5rem"></div>', unsafe_allow_html=True)
    render_doc_upload(catalog_path=str(CATALOG_PATH), api_key=None)

    # ── Bottom nav ────────────────────────────────────────────────────────
    st.markdown('<div style="flex:1"></div>', unsafe_allow_html=True)
    st.markdown("""
<div style="border-top:1px solid rgba(255,255,255,0.05);margin-top:1rem;padding-top:0.5rem">
  <div class="sb-nav-item"><span>⚙️</span><span>Settings</span></div>
  <div style="padding:0.6rem 1rem 0.8rem;font-size:0.62rem;color:rgba(100,116,139,0.5);
              font-family:Inter,sans-serif;line-height:1.7">
    893K events · 15K users · DuckDB<br>
    gpt-4o-mini · 4–5 LLM calls/query
  </div>
</div>
""", unsafe_allow_html=True)


# ── UI ────────────────────────────────────────────────────────────────────────

# Build session title from first user message (if any)
def _session_display_title() -> str:
    msgs = st.session_state.get("messages", [])
    for m in msgs:
        if m.get("role") == "user" and m.get("content"):
            t = m["content"][:60]
            return t + ("…" if len(m["content"]) > 60 else "")
    return "New Analysis"

_session_title = _session_display_title()
_today = date.today().strftime("%b %d, %Y")
_mode_badge = "Reliability" if st.session_state.get("runtime_mode") == "Reliability mode (new)" else "Legacy"

st.markdown(f"""
<div class="main-topbar">
  <div class="topbar-breadcrumb">
    <span class="topbar-crumb-parent">Liveboard</span>
    <span class="topbar-crumb-sep">›</span>
    <span class="topbar-crumb-current">{_session_title}</span>
  </div>
  <div class="main-topbar-actions">
    <span class="topbar-btn topbar-btn-primary">↗ Share</span>
    <span class="topbar-icon-btn" title="Refresh">⟳</span>
    <span class="topbar-icon-btn" title="Favourite">☆</span>
    <span class="topbar-icon-btn" title="More">···</span>
  </div>
</div>
""", unsafe_allow_html=True)

# Debug toggle — small, unobtrusive
if "debug_semantics" not in st.session_state:
    st.session_state["debug_semantics"] = True
st.session_state["debug_semantics"] = st.toggle(
    "Debug semantics",
    value=st.session_state["debug_semantics"],
    help="Show full pipeline: inputs vs orchestrator text, component call order, resolver/arbiter, compiler, delivered output, and raw JSON.",
)

# Runtime mode switch — allows instant A/B between old and new flow in UI.
_MODE_OPTIONS = ("Reliability mode (new)", "Legacy mode (old)")
if "runtime_mode" not in st.session_state:
    st.session_state["runtime_mode"] = _MODE_OPTIONS[0]
st.session_state["runtime_mode"] = st.selectbox(
    "Execution mode",
    _MODE_OPTIONS,
    index=_MODE_OPTIONS.index(st.session_state.get("runtime_mode", _MODE_OPTIONS[0])),
    help="Reliability mode uses resolver as a single gate and explicit deep-analysis trigger. Legacy mode restores prior behavior.",
)
if st.session_state["runtime_mode"] == "Legacy mode (old)":
    st.session_state["flag_use_policy_arbiter"] = True
    st.session_state["flag_hypothesis_only_diagnose"] = False
    st.session_state["flag_deep_auto"] = True
else:
    st.session_state["flag_use_policy_arbiter"] = DEFAULT_USE_POLICY_ARBITER
    st.session_state["flag_hypothesis_only_diagnose"] = DEFAULT_HYPOTHESIS_ONLY_DIAGNOSE
    st.session_state["flag_deep_auto"] = DEFAULT_DEEP_ANALYSIS_AUTO

if "messages" not in st.session_state:
    st.session_state.messages = []

# ── Home page — shown only when chat is empty ─────────────────────────────────
if not st.session_state.messages:

    # ── Hero ─────────────────────────────────────────────────────────────────
    st.markdown("""
<div class="home-hero">
  <div style="display:inline-flex;align-items:center;gap:0.45rem;
              background:#EEF2FF;border:1px solid #C7D2FE;
              border-radius:999px;padding:0.28rem 0.9rem;
              font-size:0.67rem;font-weight:700;color:#3B5BDB;margin-bottom:1.1rem;
              letter-spacing:0.07em;text-transform:uppercase">
    ✦ AI Analytics · Liveboards
  </div>
  <h2 style="font-size:1.9rem;font-weight:800;color:#0F172A;margin:0 0 0.55rem;
             letter-spacing:-0.03em;font-family:Inter,sans-serif;line-height:1.2">
    What do you want to analyse?
  </h2>
  <p style="color:#64748B;font-size:0.9rem;margin:0;line-height:1.65;
             font-family:Inter,sans-serif;max-width:420px;display:inline-block">
    Choose a template below, or ask anything about your data
  </p>
</div>
""", unsafe_allow_html=True)

    # ── Role filter row ───────────────────────────────────────────────────────
    _ROLES = ["All", "Product", "Engineering", "Data", "Marketing", "Growth"]
    if "uc_filter" not in st.session_state:
        st.session_state["uc_filter"] = "All"

    _fc = st.columns(len(_ROLES))
    for _ri, _role in enumerate(_ROLES):
        with _fc[_ri]:
            if st.button(
                _role,
                key=f"rf_{_role}",
                use_container_width=True,
                type="primary" if st.session_state["uc_filter"] == _role else "secondary",
            ):
                st.session_state["uc_filter"] = _role
                st.rerun()

    st.markdown('<div style="height:1.25rem"></div>', unsafe_allow_html=True)

    # ── Use-case definitions ──────────────────────────────────────────────────
    _USE_CASES = [
        {
            "icon": "📝",
            "title": "Registration Funnel Friction",
            "desc": "Find the specific screen where users quit during registration",
            "question": "Show me the registration funnel drop-off",
            "badges": [("Fintech", "#10b981"), ("Product", "#3b82f6")],
            "roles": ["Product"],
        },
        {
            "icon": "💳",
            "title": "Transaction Failure Rates",
            "desc": "Monitor how many users try to send money but hit a technical error",
            "question": "What is the transaction failure rate?",
            "badges": [("Fintech", "#10b981"), ("Engineering", "#f97316")],
            "roles": ["Engineering"],
        },
        {
            "icon": "🔐",
            "title": "Biometric Login Adoption",
            "desc": "Track how many users enable FaceID vs typing a PIN",
            "question": "What percentage of users use biometric login?",
            "badges": [("Fintech", "#10b981"), ("Product", "#3b82f6")],
            "roles": ["Product"],
        },
        {
            "icon": "✅",
            "title": "KYC Verification Funnel",
            "desc": "Track identity verification completion and drop-off points",
            "question": "What's the KYC completion rate?",
            "badges": [("Fintech", "#10b981"), ("Data", "#8b5cf6")],
            "roles": ["Data"],
        },
        {
            "icon": "💰",
            "title": "First Deposit Conversion",
            "desc": "Measure how many verified users actually fund their accounts",
            "question": "What's the first deposit conversion rate?",
            "badges": [("Fintech", "#10b981"), ("Marketing", "#ec4899")],
            "roles": ["Marketing"],
        },
        {
            "icon": "📈",
            "title": "Daily Active Users",
            "desc": "DAU trend by platform and city over the last 30 days",
            "question": "DAU by platform last 30 days",
            "badges": [("Fintech", "#10b981"), ("Data", "#8b5cf6")],
            "roles": ["Data", "Product"],
        },
        {
            "icon": "🔁",
            "title": "D7 Retention MOM",
            "desc": "Month-over-month change in 7-day user retention rate",
            "question": "D7 retention MOM",
            "badges": [("Fintech", "#10b981"), ("Product", "#3b82f6")],
            "roles": ["Product"],
        },
        {
            "icon": "👥",
            "title": "Installed But Never Transacted",
            "desc": "Cohort of users who onboarded but never made a first payment",
            "question": "Users who installed but never transacted",
            "badges": [("Fintech", "#10b981"), ("Growth", "#f59e0b")],
            "roles": ["Growth", "Marketing"],
        },
        {
            "icon": "⏱️",
            "title": "Time to First Transaction",
            "desc": "Median time from sign-up to first successful payment",
            "question": "How long does onboarding take on average?",
            "badges": [("Fintech", "#10b981"), ("Data", "#8b5cf6")],
            "roles": ["Data", "Growth"],
        },
        {
            "icon": "🏙️",
            "title": "Top Cities by Active Users",
            "desc": "Geographic breakdown of your most engaged user base",
            "question": "Top cities by active users last 30 days",
            "badges": [("Fintech", "#10b981"), ("Marketing", "#ec4899")],
            "roles": ["Marketing"],
        },
        {
            "icon": "🔍",
            "title": "Why Did DAU Drop?",
            "desc": "Automated 4-step root cause: supply → conversion → funnel → segments",
            "question": "Why did DAU drop?",
            "badges": [("Fintech", "#10b981"), ("Product", "#3b82f6")],
            "roles": ["Product", "Engineering"],
        },
        {
            "icon": "🚀",
            "title": "Onboarding Funnel",
            "desc": "Step-by-step conversion from app install to activated user",
            "question": "Show onboarding funnel drop-off",
            "badges": [("Fintech", "#10b981"), ("Growth", "#f59e0b")],
            "roles": ["Growth", "Product"],
        },
    ]

    _active_filter = st.session_state.get("uc_filter", "All")
    _filtered_ucs = [
        uc for uc in _USE_CASES
        if _active_filter == "All" or _active_filter in uc.get("roles", [])
    ]

    # ── Card grid (3 columns) ─────────────────────────────────────────────────
    _N_COLS = 3
    _uc_rows = [_filtered_ucs[i:i + _N_COLS] for i in range(0, len(_filtered_ucs), _N_COLS)]
    for _row_ucs in _uc_rows:
        _gcols = st.columns(_N_COLS, gap="medium")
        for _ci, (_gcol, _uc) in enumerate(zip(_gcols, _row_ucs)):
            with _gcol:
                _badge_html = " ".join(
                    f'<span style="display:inline-block;background:{c}18;color:{c};'
                    f'border:1px solid {c}35;border-radius:6px;font-size:0.62rem;'
                    f'font-weight:700;padding:0.1rem 0.5rem;margin-right:0.2rem;'
                    f'font-family:Inter,sans-serif;letter-spacing:0.04em">{b}</span>'
                    for b, c in _uc["badges"]
                )
                st.markdown(f"""
<div class="uc-card">
  <div style="margin-bottom:0.7rem">{_badge_html}</div>
  <div style="font-size:1.5rem;margin-bottom:0.5rem;line-height:1">{_uc["icon"]}</div>
  <div style="font-size:0.88rem;font-weight:700;color:#0F172A;font-family:Inter,sans-serif;
              margin-bottom:0.38rem;line-height:1.35;letter-spacing:-0.015em">{_uc["title"]}</div>
  <div style="font-size:0.76rem;color:#64748B;font-family:Inter,sans-serif;
              line-height:1.6;margin-bottom:0.9rem">{_uc["desc"]}</div>
  <div style="font-size:0.62rem;color:#94A3B8;font-family:Inter,sans-serif;
              text-transform:uppercase;letter-spacing:0.08em;margin-bottom:0.28rem;
              font-weight:600">Example question:</div>
  <div style="font-size:0.77rem;color:#3B5BDB;font-family:Inter,sans-serif;
              font-style:italic;line-height:1.5">"{_uc["question"]}"</div>
</div>
""", unsafe_allow_html=True)
                if st.button(
                    "Ask this →",
                    key=f"uc_{_ci}_{_uc['title'][:14].replace(' ', '_')}",
                    use_container_width=True,
                ):
                    st.session_state["pending"] = _uc["question"]
                    st.rerun()

    st.markdown('<div style="height:1.2rem"></div>', unsafe_allow_html=True)

# Render history
for msg_i, msg in enumerate(st.session_state.messages):
    if msg["role"] == "user":
        st.markdown(
            f'<div class="user-msg-wrap">'
            f'<div class="user-msg-label">You</div>'
            f'<div class="user-msg-text">{msg["content"]}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        # ── Assistant response — document card ────────────────────────────
        st.markdown('<div class="response-card">', unsafe_allow_html=True)
        st.markdown('<div class="response-badge">📊 Analytics Agent</div>',
                    unsafe_allow_html=True)

        # Steps (collapsed)
        if msg.get("steps"):
            with st.expander("Agent steps", expanded=False):
                for step in msg["steps"]:
                    st.markdown(f'<div class="step-line">{step}</div>', unsafe_allow_html=True)

        # SQL
        if msg.get("compiler_sql"):
            with st.expander("Compiled SQL", expanded=False):
                st.code(msg["compiler_sql"], language="sql")
        if msg.get("sql"):
            with st.expander("Generated SQL", expanded=False):
                st.code(msg["sql"], language="sql")
        if msg.get("answer_contract"):
            st.markdown(_answer_contract_html(msg["answer_contract"]), unsafe_allow_html=True)
        if msg.get("debug_signature") and msg.get("metric_contract"):
            if st.session_state.get("debug_semantics"):
                sig = msg["debug_signature"]
                if isinstance(sig.get("component_flow"), list):
                    _finalize_debug_signature_outputs(
                        msg, sql=msg.get("sql"), metric_name=msg.get("metric_name")
                    )
                    _render_semantics_debug_panel(
                        sig,
                        msg["metric_contract"],
                        _sql_for_debug_panel(msg.get("sql")),
                        expanded=False,
                        widget_key_suffix=f"hist_{msg_i}",
                    )
                else:
                    with st.expander("Pipeline debug (legacy)", expanded=False):
                        st.json(sig)
                        st.json(msg["metric_contract"])

        if msg.get("active_recipe"):
            _ar = msg["active_recipe"]
            st.markdown(
                f'<div style="font-size:0.72rem;color:#64748b;margin-bottom:0.45rem">'
                f'🍳 <b>{_ar.get("cookbook_id","default")}</b> · recipe '
                f'<b>{_ar.get("recipe_id","unknown")}</b></div>',
                unsafe_allow_html=True,
            )

        # Metric badge
        if msg.get("metric_name"):
            st.markdown(
                f'<div style="font-size:0.78rem;color:#3b82f6;margin-bottom:0.6rem;'
                f'font-weight:500">📌 {msg["metric_name"]}</div>',
                unsafe_allow_html=True,
            )

        # Answer narrative
        if msg.get("content"):
            st.markdown(msg["content"])

        # Demographic breakdown replay
        if msg.get("analysis_type") == "demographic_breakdown" and msg.get("demo_cards"):
            from types import SimpleNamespace
            all_invs = [
                SimpleNamespace(
                    name    = card["name"],
                    purpose = card["purpose"],
                    insight = card.get("insight", ""),
                    df      = pd.DataFrame(card["data"], columns=card["columns"]),
                )
                for card in msg["demo_cards"]
                if card.get("data")
            ]
            user_invs  = [i for i in all_invs if i.name.startswith("user_")]
            event_invs = [i for i in all_invs if i.name.startswith("event_")]
            other_invs = [i for i in all_invs if not i.name.startswith(("user_", "event_"))]
            _hist_sid = f"hist_{SESSION_ID}_{msg_i}"
            _render_dim_grid(user_invs,  "User Profile", _hist_sid)
            _render_dim_grid(event_invs, "Event Properties", _hist_sid)
            if other_invs:
                _render_dim_grid(other_invs, "Other", _hist_sid)

        # Chart + table
        elif msg.get("table"):
            df = pd.DataFrame(msg["table"]["data"], columns=msg["table"]["columns"])
            if not df.empty:
                if _is_retention_matrix_df(df):
                    _render_retention_matrix(df)
                else:
                    is_scalar = _is_scalar_like(df)
                    if is_scalar:
                        _render_scalar_metric(df)
                    else:
                        st.markdown('<div class="evidence-header">Supporting Evidence</div>',
                                    unsafe_allow_html=True)
                        show_chart, _ = _should_show_chart(df, compact=False)
                        if show_chart:
                            _ev_title = (msg.get("evidence_chart_title") or msg.get("metric_name") or "").strip() or None
                            display_evidence_chart(
                                df.copy(),
                                msg.get("metric_name") or "Evidence",
                                chart_title=_ev_title,
                                key=f"hist_{SESSION_ID}_{msg_i}_evidence",
                            )
                        with st.expander("View data", expanded=False):
                            st.dataframe(
                                format_rate_columns_for_display(df),
                                use_container_width=True,
                                hide_index=True,
                            )

        if msg.get("refine_chips"):
            render_refine_chips_from_saved(
                msg["refine_chips"],
                key_prefix=f"hist_{SESSION_ID}_{msg_i}",
            )

        if msg.get("error"):
            st.error(msg["error"])

        st.markdown('</div>', unsafe_allow_html=True)  # close response-card

        # Feedback bar — only for real responses (not clarify/error-only messages)
        if msg.get("content") and not msg.get("error"):
            render_feedback_bar(msg_i, msg, SESSION_ID)

# New message — from chat input OR suggestion button
prompt = st.chat_input("Ask a question about your data...")
if not prompt and st.session_state.get("pending"):
    prompt = st.session_state.pop("pending")

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})

    # Render user message as document-style (not chat bubble)
    st.markdown(
        f'<div class="user-msg-wrap">'
        f'<div class="user-msg-label">You</div>'
        f'<div class="user-msg-text">{prompt}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Open assistant response card
    st.markdown('<div class="response-card">', unsafe_allow_html=True)
    st.markdown('<div class="response-badge">📊 Analytics Agent</div>',
                unsafe_allow_html=True)

    with st.container():
        steps        = []
        assistant_msg = {"role": "assistant", "content": "", "steps": steps}

        step_box = st.empty()

        def show_step(icon: str, text: str):
            steps.append(f"{icon} {text}")
            step_box.markdown("\n\n".join(steps))

        # turn dict accumulates everything needed for SQLite at the end
        turn: dict = {"question": prompt}

        try:
            sql: str | None = None
            metric_name: str | None = None
            qo = None  # set by get_sql / drill shortcut; must exist if try aborts early
            clarify_msg = None
            _matched_playbook = None

            # ── Drill-down shortcut: button click bypasses orchestrator ────────
            # Stores full context so we don't need the LLM to re-parse it.
            drill_ctx = st.session_state.pop("drill_context", None)
            hypothesis_doc = None
            if drill_ctx:
                from core.sql.query_object import QueryObject as _QO
                _dqo = _QO(
                    analysis_type="diagnose",
                    event=drill_ctx["event"],
                    filters=drill_ctx.get("filters", {}),
                    time_range_days=60,
                    diagnose_period_end=drill_ctx.get("period_end"),
                )
                sql, metric_name, clarify_msg, qo = "__diagnose__", None, None, _dqo
            else:
                show_step("🔍", "Understanding your question...")
                try:
                    if should_generate_hypotheses(prompt):
                        hist = get_qo_history(st.session_state.get("session_id", ""), limit=5)
                        hypothesis_doc = generate_hypotheses(
                            question=prompt,
                            catalog=CATALOG,
                            history=hist,
                            openai_api_key=None,
                        )
                    else:
                        hypothesis_doc = None
                except Exception:
                    hypothesis_doc = None
                sql, metric_name, clarify_msg, qo = get_sql(prompt, hypothesis_doc=hypothesis_doc)

            # ── Cookbook / Recipe context binding (playbook-backed, backward compatible) ─
            if qo and not drill_ctx:
                _matched_playbook = PLAYBOOK_REGISTRY.find(prompt)
                if _matched_playbook:
                    setattr(qo, "_active_recipe_validation_rules", _matched_playbook.validation_rules or {})
                    setattr(qo, "_active_recipe_tool_controls", _matched_playbook.tool_controls or {})
                    setattr(qo, "_active_recipe_id", _matched_playbook.recipe_id or _matched_playbook.id)
                    setattr(qo, "_active_cookbook_id", _matched_playbook.cookbook_id or "default")
                    assistant_msg["active_recipe"] = {
                        "cookbook_id": _matched_playbook.cookbook_id or "default",
                        "recipe_id": _matched_playbook.recipe_id or _matched_playbook.id,
                        "playbook_id": _matched_playbook.id,
                    }
                    turn["cookbook_id"] = _matched_playbook.cookbook_id or "default"
                    turn["recipe_id"] = _matched_playbook.recipe_id or _matched_playbook.id
                    turn["playbook_id"] = _matched_playbook.id

                    allowed = (_matched_playbook.tool_controls or {}).get("allowed_analysis_types") or []
                    allowed_set = {str(x) for x in allowed}
                    at = (getattr(qo, "analysis_type", "") or "").strip().lower()
                    if allowed and at not in allowed_set:
                        pb_label = (
                            getattr(_matched_playbook, "name", None)
                            or _matched_playbook.id
                            or "this playbook"
                        )
                        types_txt = ", ".join(str(x) for x in allowed)
                        if at in ("clarify", "out_of_scope"):
                            base = (
                                (clarify_msg or "").strip()
                                or (getattr(qo, "clarify_message", None) or "").strip()
                            )
                            tip = (
                                f"**Tip:** *{pb_label}* questions usually work as: {types_txt} "
                                "(e.g. a **metric** or **funnel** with named catalog events). "
                                "Saying which event or pre-built metric you mean helps."
                            )
                            clarify_msg = f"{base}\n\n{tip}" if base else (
                                "I wasn’t confident enough to run that as-is.\n\n" + tip
                            )
                        else:
                            clarify_msg = (
                                f"This playbook (*{pb_label}*) is set up for: {types_txt}. "
                                f"I interpreted this as `{at}`. "
                                "Try rephrasing as one of those, or name the event/metric explicitly."
                            )

            # ── Deep Analysis — explicit by default (predictable control flow) ─
            # With DEEP_ANALYSIS_AUTO=true, orchestrator depth can auto-enable it.
            # Drill-down bypasses the orchestrator entirely, so never deep.
            if qo and should_run_deep_analysis(prompt, qo) and not drill_ctx:
                show_step("🔬", "Building multi-angle investigation plan...")
                _deep_plan = generate_deep_plan(
                    question=prompt,
                    catalog=CATALOG,
                    openai_api_key=None,
                    playbook=_matched_playbook,
                )
                if _deep_plan.investigation_queries:
                    n = len(_deep_plan.investigation_queries)
                    step_box.empty()

                    with st.status(f"Deep Analysis · {n} queries running in parallel…", expanded=True) as _status:
                        for _iq in _deep_plan.investigation_queries:
                            st.write(f"◦ {_iq['sub_question']}")
                        _sub_results = run_parallel_investigations(
                            investigation_queries=_deep_plan.investigation_queries,
                            catalog=CATALOG,
                            sampled_values=SAMPLED_VALUES,
                            db_path=str(DB_PATH),
                            openai_api_key=None,
                            parent_qo_dict=qo.to_dict() if qo and hasattr(qo, "to_dict") else None,
                        )
                        _ok_n = sum(1 for r in _sub_results if r.ok)
                        _status.update(
                            label=f"Deep Analysis complete · {_ok_n}/{n} queries succeeded",
                            state="complete",
                            expanded=False,
                        )

                    show_step("✍️", "Synthesizing findings into report...")
                    _deep_arc = build_deep_story_arc(
                        question=prompt,
                        sub_results=_sub_results,
                        catalog=CATALOG,
                        openai_api_key=None,
                        narrative_thread=get_narrative_thread(SESSION_ID, limit=4),
                    )
                    step_box.empty()

                    if qo and qo.analysis_type not in ("clarify", "out_of_scope"):
                        assistant_msg["answer_contract"] = _render_answer_contract_section(
                            qo, metric_name
                        )

                    _render_deep_report(_deep_arc, _sub_results, assistant_msg, turn, user_question=prompt)
                    _attach_and_render_refine_chips(assistant_msg, qo, "deep")
                    _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
                    assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)
                    st.session_state.messages.append(assistant_msg)
                    st.stop()
                # If plan was empty, fall through to the normal single-query path

            compiler_sql = _sql_for_debug_panel(sql)
            if compiler_sql:
                assistant_msg["compiler_sql"] = compiler_sql
            if qo and st.session_state.get("debug_semantics"):
                if drill_ctx:
                    setattr(qo, "_debug_user_prompt", prompt)
                    setattr(
                        qo,
                        "_debug_orchestrator_input",
                        "(drill-down shortcut — orchestrator skipped)",
                    )
                    setattr(
                        qo,
                        "_debug_flow",
                        [
                            "chat: drill_context (orchestrator bypass)",
                            "→ core.diagnose.run_diagnosis",
                        ],
                    )
                    setattr(qo, "_hypothesis_generated_debug", hypothesis_doc is not None)
                    setattr(qo, "_clarify_ctx_injected_debug", False)
                    setattr(
                        qo,
                        "_qo_pre_override_debug",
                        {
                            "analysis_type": qo.analysis_type,
                            "metric_id": getattr(qo, "metric_id", None),
                            "event": qo.event,
                            "time_granularity": getattr(qo, "time_granularity", None),
                            "time_range_days": qo.time_range_days,
                            "date_from": qo.date_from,
                            "date_to": qo.date_to,
                            "breakdown": getattr(qo, "breakdown", None),
                            "funnel_steps": list(getattr(qo, "funnel_steps", None) or []),
                        },
                    )
                else:
                    hyp_pre = (
                        ["core.hypothesis_agent.generate_hypotheses"]
                        if hypothesis_doc
                        else ["core.hypothesis_agent (skipped)"]
                    )
                    setattr(
                        qo,
                        "_debug_flow",
                        hyp_pre + (getattr(qo, "_debug_flow", None) or []),
                    )
                assistant_msg["debug_signature"] = _build_full_debug_signature(qo)
                assistant_msg["metric_contract"] = _canonical_metric_contract(qo, METRICS)

            # Capture QO slots so they're available for orchestrator context next turn
            if qo and qo.analysis_type not in ("clarify", "out_of_scope"):
                materialize_rollups_on_qo(qo, CATALOG)
                turn.update({
                    "analysis_type":   qo.analysis_type,
                    "event":           qo.event,
                    "breakdown":       qo.breakdown,
                    "filters":         qo.filters or {},
                    "metric_id":       qo.metric_id,
                    "date_from":       qo.date_from,
                    "date_to":         qo.date_to,
                    "time_range_days": qo.time_range_days,
                    "time_granularity": qo.time_granularity,
                    "time_source":     getattr(qo, "time_source", None) or "default",
                    "custom_event_name": getattr(qo, "_executed_custom_event_name", None),
                    "qo_obj":          qo,
                })
            if hypothesis_doc:
                turn["hypothesis_context"] = hypothesis_doc.investigation_context

            if clarify_msg:
                step_box.empty()
                st.info(clarify_msg)
                assistant_msg["content"] = clarify_msg
                turn["answer"] = clarify_msg
                turn["memory_summary"] = clarify_msg[:220]
                # Save the clarify QO so history contains the clarification question.
                # This allows _extract_clarify_context() to detect it on the next turn
                # and prepend the context for multi-pass resolution.
                if qo:
                    turn.update({
                        "analysis_type":    "clarify",
                        "event":            getattr(qo, "event", None),
                        "breakdown":        None,
                        "filters":          {},
                        "metric_id":        None,
                        "date_from":        None,
                        "date_to":          None,
                        "time_range_days":  30,
                        "time_granularity": "day",
                        "time_source":      "default",
                        "qo_obj":           qo,
                    })
                _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
                assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)
                st.session_state.messages.append(assistant_msg)
                st.stop()

            if qo and qo.analysis_type not in ("clarify", "out_of_scope"):
                assistant_msg["answer_contract"] = _render_answer_contract_section(qo, metric_name)

            # ── Diagnose path ────────────────────────────────────────────────
            if sql == "__diagnose__":
                event        = qo.event or "app_opened"
                diag_filters = qo.filters or {}
                excl_dims    = drill_ctx.get("exclude_dims", []) if drill_ctx else []

                filter_label = (
                    " · ".join(f"{k}={v}" for k, v in diag_filters.items())
                    if diag_filters else ""
                )
                scope_label = f"{event.replace('_',' ')} · {filter_label}" if filter_label else event.replace('_',' ')
                show_step("🔬", f"Forming hypotheses for **{scope_label}**...")
                result = run_diagnosis(
                    event=event,
                    time_range_days=qo.time_range_days or 60,
                    db_path=str(DB_PATH),
                    openai_api_key=None,
                    period_end=qo.diagnose_period_end or None,
                    filters=diag_filters,
                    exclude_dims=excl_dims,
                    catalog=CATALOG,
                    event_sampled_values=SAMPLED_VALUES.get("events", {}),
                    question=prompt,
                    hypothesis_doc=hypothesis_doc,
                    narrative_thread=get_narrative_thread(SESSION_ID, limit=4),
                    narrative_qo=qo,
                )
                show_step("✅", "Analysis complete")
                step_box.empty()

                ov          = result["overall"]
                pct         = ov["pct_change"]
                clr         = "#34d399" if ov["delta"] >= 0 else "#fb7185"
                arr         = "▲" if ov["delta"] >= 0 else "▼"
                curr_period = result.get("curr_period", "")
                prev_period = result.get("prev_period", "")
                direction   = "increase" if ov["delta"] >= 0 else "drop"

                # ── Act 1: What happened ─────────────────────────────────────
                filter_badge = (
                    f'<div style="display:inline-block;font-size:0.68rem;color:#7c6af7;'
                    f'background:#1e1b3a;border:1px solid #3b3570;border-radius:6px;'
                    f'padding:0.15rem 0.5rem;margin-bottom:0.5rem">'
                    f'🔍 Filtered: {filter_label}</div>'
                    if filter_label else ""
                )
                st.markdown(f"""
<div style="background:#1a1d27;border:1px solid #2a2d3a;border-radius:12px;padding:1.2rem 1.6rem;margin-bottom:1rem">
  {filter_badge}
  <div style="font-size:0.7rem;color:#64748b;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:0.5rem">{event.replace("_"," ").upper()}</div>
  <div style="display:flex;align-items:baseline;gap:0.8rem;flex-wrap:wrap">
    <span style="font-size:2rem;font-weight:700;color:#e2e8f0">{ov['previous']:,}</span>
    <span style="font-size:1.2rem;color:#475569">→</span>
    <span style="font-size:2rem;font-weight:700;color:{clr}">{ov['current']:,}</span>
    <span style="font-size:1.1rem;font-weight:600;color:{clr}">{arr} {abs(ov['delta']):,} &nbsp;({abs(pct):.1f}%)</span>
  </div>
  <div style="font-size:0.72rem;color:#475569;margin-top:0.4rem">
    Previous period: {prev_period} &nbsp;·&nbsp; Current period: {curr_period}
  </div>
</div>""", unsafe_allow_html=True)

                # ── Act 2: Structural checks (supply / conversion / funnel) ──
                sc  = result.get("supply_check")
                cc  = result.get("conversion_check")
                fdf = result.get("funnel_df")

                if sc or cc:
                    st.markdown(
                        '<div style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                        'text-transform:uppercase;letter-spacing:0.05em;margin:1rem 0 0.5rem">'
                        'Structural checks</div>',
                        unsafe_allow_html=True,
                    )
                    check_cols = st.columns(2 if (sc and cc) else 1)

                    if sc:
                        sc_pct   = sc["pct_change"]
                        sc_clr   = "#34d399" if sc_pct >= 0 else "#fb7185"
                        sc_arr   = "▲" if sc_pct >= 0 else "▼"
                        sc_label = "Supply: fewer inputs?" if sc_pct < -2 else \
                                   "Supply: more inputs" if sc_pct > 2 else "Supply: stable"
                        with check_cols[0]:
                            st.markdown(f"""
<div style="background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:0.9rem 1.2rem">
  <div style="font-size:0.68rem;color:#64748b;text-transform:uppercase;letter-spacing:0.05em">{sc["upstream_event"].replace("_"," ")}</div>
  <div style="font-size:1.4rem;font-weight:700;color:#e2e8f0;margin:0.3rem 0">{sc["prev_n"]:,} → {sc["curr_n"]:,}</div>
  <div style="font-size:0.82rem;font-weight:600;color:{sc_clr}">{sc_arr} {abs(sc_pct):.1f}% &nbsp;<span style="color:#64748b;font-weight:400">{sc_label}</span></div>
</div>""", unsafe_allow_html=True)

                    if cc:
                        dp = cc["delta_pp"]
                        cv_clr = "#34d399" if dp >= 0 else "#fb7185"
                        cv_arr = "▲" if dp >= 0 else "▼"
                        with check_cols[1 if sc else 0]:
                            st.markdown(f"""
<div style="background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:0.9rem 1.2rem">
  <div style="font-size:0.68rem;color:#64748b;text-transform:uppercase;letter-spacing:0.05em">{cc["upstream"].replace("_"," ")} → {cc["downstream"].replace("_"," ")} rate</div>
  <div style="font-size:1.4rem;font-weight:700;color:#e2e8f0;margin:0.3rem 0">{cc["prev_pct"]:.1f}% → {cc["curr_pct"]:.1f}%</div>
  <div style="font-size:0.82rem;font-weight:600;color:{cv_clr}">{cv_arr} {abs(dp):.1f} pp &nbsp;<span style="color:#64748b;font-weight:400">conversion {"worsened" if dp < 0 else "improved"}</span></div>
</div>""", unsafe_allow_html=True)

                # ── Act 2.5: Quality check (demand vs failure rate) ──────────
                qc   = result.get("quality_check")
                fms  = result.get("failure_modes") or {}
                if qc:
                    st.markdown(
                        '<div style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                        'text-transform:uppercase;letter-spacing:0.05em;margin:1rem 0 0.5rem">'
                        'Quality check — demand vs failure rate</div>',
                        unsafe_allow_html=True,
                    )
                    # Classify the problem type
                    if qc["demand_dropped"] and not qc["quality_degraded"]:
                        badge_clr, badge_txt = "#f97316", "DEMAND PROBLEM"
                    elif qc["quality_degraded"] and not qc["demand_dropped"]:
                        badge_clr, badge_txt = "#fb7185", "QUALITY PROBLEM"
                    elif qc["demand_dropped"] and qc["quality_degraded"]:
                        badge_clr, badge_txt = "#e879f9", "BOTH PROBLEMS"
                    else:
                        badge_clr, badge_txt = "#34d399", "STABLE QUALITY"

                    att_chg = round(
                        (qc["curr_attempts"] - qc["prev_attempts"])
                        / max(qc["prev_attempts"], 1) * 100, 1
                    )
                    rate_delta = round(qc["curr_success_rate"] - qc["prev_success_rate"], 1)
                    rate_clr   = "#34d399" if rate_delta >= 0 else "#fb7185"
                    att_clr    = "#34d399" if att_chg >= 0 else "#fb7185"

                    qc_cols = st.columns(3)
                    with qc_cols[0]:
                        st.markdown(f"""
<div style="background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:0.9rem 1.2rem">
  <div style="font-size:0.62rem;color:#64748b;text-transform:uppercase;letter-spacing:0.05em">Total attempts (all statuses)</div>
  <div style="font-size:1.3rem;font-weight:700;color:#e2e8f0;margin:0.3rem 0">{qc["prev_attempts"]:,} → {qc["curr_attempts"]:,}</div>
  <div style="font-size:0.82rem;font-weight:600;color:{att_clr}">{att_chg:+.1f}%</div>
</div>""", unsafe_allow_html=True)
                    with qc_cols[1]:
                        st.markdown(f"""
<div style="background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:0.9rem 1.2rem">
  <div style="font-size:0.62rem;color:#64748b;text-transform:uppercase;letter-spacing:0.05em">Success rate ({qc["status_col"]})</div>
  <div style="font-size:1.3rem;font-weight:700;color:#e2e8f0;margin:0.3rem 0">{qc["prev_success_rate"]:.1f}% → {qc["curr_success_rate"]:.1f}%</div>
  <div style="font-size:0.82rem;font-weight:600;color:{rate_clr}">{rate_delta:+.1f} pp</div>
</div>""", unsafe_allow_html=True)
                    with qc_cols[2]:
                        st.markdown(f"""
<div style="background:#1a1d27;border:1px solid {badge_clr}44;border-radius:10px;padding:0.9rem 1.2rem">
  <div style="font-size:0.62rem;color:#64748b;text-transform:uppercase;letter-spacing:0.05em">Diagnosis</div>
  <div style="font-size:1rem;font-weight:700;color:{badge_clr};margin:0.5rem 0">{badge_txt}</div>
</div>""", unsafe_allow_html=True)

                # ── Act 2.6: Failure mode breakdown ──────────────────────────
                if fms:
                    st.markdown(
                        '<div style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                        'text-transform:uppercase;letter-spacing:0.05em;margin:1rem 0 0.5rem">'
                        'Failure mode breakdown</div>',
                        unsafe_allow_html=True,
                    )
                    fm_cols = st.columns(min(len(fms), 2))
                    for i, (dim, fm_df) in enumerate(fms.items()):
                        if fm_df.empty:
                            continue
                        with fm_cols[i % 2]:
                            st.caption(dim.replace("_", " ").title())
                            fm_plot = fm_df.copy()
                            fm_plot["failure_mode"] = (
                                fm_plot["failure_mode"].astype(str)
                                .str.replace("_", " ").str[:28]
                            )
                            _fm_bar = (
                                alt.Chart(fm_plot.head(8))
                                .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
                                .encode(
                                    y=alt.Y("failure_mode:N",
                                            sort=fm_plot["curr_n"].tolist(),
                                            title="",
                                            axis=alt.Axis(labelFontSize=10)),
                                    x=alt.X("curr_n:Q", title="Users (current period)"),
                                    color=alt.condition(
                                        alt.datum.delta > 0,
                                        alt.value("#fb7185"),
                                        alt.value("#475569"),
                                    ),
                                    tooltip=[
                                        alt.Tooltip("failure_mode:N", title="Mode"),
                                        alt.Tooltip("prev_n:Q", title="Prev"),
                                        alt.Tooltip("curr_n:Q", title="Current"),
                                        alt.Tooltip("pct_change:Q", title="% chg", format="+.1f"),
                                    ],
                                )
                            )
                            st.altair_chart(
                                _light(_fm_bar).properties(height=max(160, len(fm_plot.head(8)) * 36)),
                                use_container_width=True,
                            )

                # ── Act 3: Funnel breakdown ───────────────────────────────────
                if fdf is not None and not fdf.empty and len(fdf) > 1:
                    st.markdown(
                        '<div style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                        'text-transform:uppercase;letter-spacing:0.05em;margin:1.2rem 0 0.5rem">'
                        'Funnel step comparison</div>',
                        unsafe_allow_html=True,
                    )
                    fdf_display = fdf.copy()
                    fdf_display["step"] = fdf_display["step"].str.replace("_", " ")
                    melted_f = pd.melt(
                        fdf_display[["step", "curr_n", "prev_n"]],
                        id_vars="step", value_vars=["prev_n", "curr_n"],
                        var_name="period", value_name="users",
                    )
                    melted_f["period"] = melted_f["period"].map(
                        {"prev_n": "Previous", "curr_n": "Current"}
                    )
                    step_order = fdf_display["step"].tolist()
                    _fc = (
                        alt.Chart(melted_f)
                        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                        .encode(
                            x=alt.X("step:N", sort=step_order, title="",
                                    axis=alt.Axis(labelAngle=0)),
                            y=alt.Y("users:Q", title="Users"),
                            color=alt.Color(
                                "period:N",
                                scale=alt.Scale(
                                    domain=["Previous", "Current"],
                                    range=["#475569", _PALETTE[0]],
                                ),
                                legend=alt.Legend(orient="bottom", labelFontSize=10),
                            ),
                            xOffset="period:N",
                            tooltip=[
                                alt.Tooltip("step:N"),
                                alt.Tooltip("period:N"),
                                alt.Tooltip("users:Q", format=","),
                            ],
                        )
                    )
                    st.altair_chart(
                        _light(_fc).properties(height=260),
                        use_container_width=True,
                    )

                # ── Act 4: Narrative ─────────────────────────────────────────
                st.markdown(result["narrative"])
                assistant_msg["content"] = result["narrative"]

                # ── Act 5: Dimension charts ──────────────────────────────────
                if result["top_dims"]:
                    st.markdown(
                        f'<div style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                        f'text-transform:uppercase;letter-spacing:0.05em;margin:1.2rem 0 0.6rem">'
                        f'Segment breakdown</div>',
                        unsafe_allow_html=True,
                    )
                    cols = st.columns(min(len(result["top_dims"]), 2))
                    for i, finding in enumerate(result["top_dims"]):
                        dim_df = result["slice_tables"].get(finding["dim"])
                        if dim_df is None:
                            continue
                        with cols[i % 2]:
                            mix_shift = finding.get("mix_shift_pp", 0)
                            shift_clr = "#34d399" if mix_shift > 0 else "#fb7185"
                            shift_arr = "▲" if mix_shift > 0 else "▼"
                            st.markdown(
                                f'<div style="font-size:0.78rem;font-weight:600;color:#e2e8f0;margin-bottom:0.2rem">'
                                f'{finding["dim"].replace("_"," ").title()}</div>'
                                f'<div style="font-size:0.7rem;color:#64748b;margin-bottom:0.5rem">'
                                f'<span style="color:{shift_clr}">{finding["slice"]} '
                                f'{shift_arr} {abs(mix_shift):.1f} pp</span>'
                                f' mix shift (share of {event.replace("_"," ")} users)</div>',
                                unsafe_allow_html=True,
                            )
                            # Show % distribution (share of total users in each period)
                            # rather than absolute counts — reveals whether the segment
                            # mix changed or everything just dropped proportionally.
                            has_pct = "curr_pct" in dim_df.columns and "prev_pct" in dim_df.columns
                            top_slices = dim_df.head(6).copy()
                            if has_pct:
                                melted = pd.melt(
                                    top_slices[["slice", "curr_pct", "prev_pct"]],
                                    id_vars="slice", value_vars=["prev_pct", "curr_pct"],
                                    var_name="period", value_name="share_pct",
                                )
                                melted["period"] = melted["period"].map(
                                    {"prev_pct": "Previous", "curr_pct": "Current"}
                                )
                                order = top_slices.sort_values("prev_pct", ascending=False)["slice"].tolist()
                                _dc = (
                                    alt.Chart(melted)
                                    .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                                    .encode(
                                        y=alt.Y("slice:N", sort=order, title="",
                                                axis=alt.Axis(labelLimit=110, labelFontSize=11)),
                                        x=alt.X("share_pct:Q", title="% of users",
                                                axis=alt.Axis(format=".1f")),
                                        color=alt.Color(
                                            "period:N",
                                            scale=alt.Scale(
                                                domain=["Previous", "Current"],
                                                range=[_PALETTE[2], _PALETTE[0]],
                                            ),
                                            legend=alt.Legend(orient="bottom", labelFontSize=10),
                                        ),
                                        xOffset="period:N",
                                        tooltip=[
                                            alt.Tooltip("slice:N"),
                                            alt.Tooltip("period:N"),
                                            alt.Tooltip("share_pct:Q", title="% share", format=".1f"),
                                        ],
                                    )
                                )
                            else:
                                # Fallback: absolute counts
                                melted = pd.melt(
                                    top_slices[["slice", "curr_n", "prev_n"]],
                                    id_vars="slice", value_vars=["prev_n", "curr_n"],
                                    var_name="period", value_name="users",
                                )
                                melted["period"] = melted["period"].map(
                                    {"prev_n": "Previous", "curr_n": "Current"}
                                )
                                order = top_slices.sort_values("prev_n", ascending=False)["slice"].tolist()
                                _dc = (
                                    alt.Chart(melted)
                                    .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                                    .encode(
                                        y=alt.Y("slice:N", sort=order, title="",
                                                axis=alt.Axis(labelLimit=110, labelFontSize=11)),
                                        x=alt.X("users:Q", title="users"),
                                        color=alt.Color(
                                            "period:N",
                                            scale=alt.Scale(
                                                domain=["Previous", "Current"],
                                                range=[_PALETTE[2], _PALETTE[0]],
                                            ),
                                            legend=alt.Legend(orient="bottom", labelFontSize=10),
                                        ),
                                        xOffset="period:N",
                                        tooltip=[
                                            alt.Tooltip("slice:N"),
                                            alt.Tooltip("period:N"),
                                            alt.Tooltip("users:Q", format=","),
                                        ],
                                    )
                                )
                            dim_chart = _light(_dc).properties(height=max(160, len(top_slices) * 36))
                            st.altair_chart(dim_chart, use_container_width=True)

                # ── Act 5b: Next step chips (from story_architect) ───────────
                diag_next_steps = result.get("next_steps") or []
                if diag_next_steps:
                    st.markdown(
                        '<div style="font-size:0.75rem;color:#64748b;'
                        'margin-top:1rem;margin-bottom:0.4rem">Recommended actions:</div>',
                        unsafe_allow_html=True,
                    )
                    ns_cols = st.columns(min(len(diag_next_steps), 3))
                    for i, step in enumerate(diag_next_steps[:3]):
                        with ns_cols[i]:
                            if st.button(step, key=f"dns_{SESSION_ID}_{i}_{step[:20]}",
                                         use_container_width=True):
                                st.session_state["pending"] = step
                                st.rerun()

                # ── Act 6: Drill-down buttons ────────────────────────────────
                drillable = [
                    f for f in result["top_dims"]
                    if f["dim"] not in (excl_dims or [])
                ][:3]
                if drillable:
                    st.markdown(
                        '<div style="font-size:0.75rem;color:#64748b;'
                        'margin-top:1.2rem;margin-bottom:0.5rem">Dig deeper:</div>',
                        unsafe_allow_html=True,
                    )
                    dcols = st.columns(len(drillable))
                    for i, f in enumerate(drillable):
                        with dcols[i]:
                            shift = f.get("mix_shift_pp", 0)
                            shift_str = f"{shift:+.1f}pp" if shift else ""
                            btn_label = (
                                f'{f["dim"].replace("_"," ").title()}: '
                                f'{f["slice"]}'
                                + (f' ({shift_str})' if shift_str else '')
                            )
                            if st.button(
                                f"🔍 {btn_label}",
                                key=f"drill_{f['dim']}_{f['slice']}_{SESSION_ID}",
                                use_container_width=True,
                            ):
                                new_filters = {**diag_filters, f["dim"]: f["slice"]}
                                st.session_state["pending"] = (
                                    f"drill into {f['dim']}={f['slice']} "
                                    f"for {event}"
                                )
                                st.session_state["drill_context"] = {
                                    "event":        event,
                                    "filters":      new_filters,
                                    "exclude_dims": list({*excl_dims, f["dim"]}),
                                    "period_end":   qo.diagnose_period_end,
                                }
                                st.rerun()

                turn["answer"] = result["narrative"]
                turn["memory_summary"] = (result["narrative"] or "")[:220]
                turn["steps"]  = steps
                _attach_and_render_refine_chips(assistant_msg, qo, "diagnose")
                _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
                assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)
                st.session_state.messages.append(assistant_msg)
                st.stop()

            # ── Analyst path (all non-diagnose types) ────────────────────────
            # "__analyst__" from compile_query means "run investigate() instead of this SQL".
            # Do not treat it as a hard compile failure — only truly empty SQL is.
            _analyst_only_types = {"forecast"}
            if not sql and qo.analysis_type not in _analyst_only_types:
                st.error("Could not generate SQL. Try rephrasing.")
                st.stop()
            if sql == "__analyst__":
                sql = ""  # investigate() builds SQL; keep empty for run_sql / simple_query gate

            if metric_name:
                show_step("📌", f"Using metric: **{metric_name}**")

            report = None
            simple_query = qo.analysis_type in ("metric", "segment", "same_month_anchor") and bool(sql)

            if simple_query:
                show_step("⚡", "Running query...")
                df, sql = run_sql_with_retry(sql, prompt, qo)
                # Smart fallback for sparse ratio trends:
                # if "per user" metric has no rows in default 30d window,
                # retry with a wider lookback before declaring no data.
                if (
                    df is not None
                    and df.empty
                    and qo is not None
                    and (str(getattr(qo, "metric_variant", "")).startswith("per_user_"))
                    and getattr(qo, "time_source", "default") == "default"
                    and int(getattr(qo, "time_range_days", 30) or 30) <= 30
                ):
                    try:
                        qo.time_range_days = 180
                        qo.time_source = "inferred_fallback"
                        sql_retry, metric_name_retry = compile_query(qo, METRICS)
                        if sql_retry:
                            show_step("↻", "No rows in 30d; retrying with 180d window...")
                            df_retry = run_sql(sql_retry)
                            if df_retry is not None and not df_retry.empty:
                                df = df_retry
                                sql = sql_retry
                                if metric_name_retry:
                                    metric_name = metric_name_retry
                                assistant_msg["sql"] = sql_retry
                                assistant_msg["compiler_sql"] = sql_retry
                    except Exception:
                        pass
                setattr(qo, "_active_metric_name", metric_name or "")
                rv = validate_result_shape(qo, df)
                cmp_intent = bool(getattr(qo, "_comparison_intent_debug", False))
                cmp_guard = (
                    cmp_intent
                    and getattr(qo, "analysis_type", "") == "metric"
                    and not getattr(qo, "breakdown", None)
                )
                if (
                    rv.ok
                    and cmp_guard
                    and df is not None
                    and not df.empty
                    and len(df) < 2
                ):
                    rv.ok = False
                    rv.reason = "comparison_output_not_comparative"
                    rv.clarify_message = (
                        "I understood this as a comparison, but only one cohort resolved. "
                        "Please specify both sides explicitly (for example: "
                        "'Jupiter active users vs transacting users in Jan')."
                    )
                if not rv.ok:
                    step_box.empty()
                    st.info(rv.clarify_message or "I may have misinterpreted this question. Could you clarify?")
                    assistant_msg["content"] = rv.clarify_message or "I may have misinterpreted this question. Could you clarify?"
                    turn["answer"] = assistant_msg["content"]
                    turn["sql"] = sql
                    assistant_msg["sql"] = sql
                    turn["memory_summary"] = assistant_msg["content"][:220]
                    _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
                    assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)
                    st.session_state.messages.append(assistant_msg)
                    st.stop()
                quality_clarify = _analysis_quality_clarify_message(qo, df)
                if quality_clarify:
                    step_box.empty()
                    st.info(quality_clarify)
                    assistant_msg["content"] = quality_clarify
                    turn["answer"] = assistant_msg["content"]
                    turn["sql"] = sql
                    assistant_msg["sql"] = sql
                    turn["memory_summary"] = assistant_msg["content"][:220]
                    _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
                    assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)
                    st.session_state.messages.append(assistant_msg)
                    st.stop()
                summary = small_narration(prompt, df, qo=qo, metric_name=metric_name)
                step_box.empty()
                st.markdown(summary)
                assistant_msg["content"] = summary
                if not df.empty:
                    if _is_scalar_like(df):
                        _render_scalar_metric(df)
                    else:
                        st.markdown('<div class="evidence-header">Supporting Evidence</div>',
                                    unsafe_allow_html=True)
                        show_chart, _ = _should_show_chart(df, compact=False)
                        if show_chart:
                            _cht = evidence_chart_title_for_qo(qo, metric_name) or (
                                (metric_name or "").strip() or None
                            )
                            if _cht:
                                assistant_msg["evidence_chart_title"] = _cht
                            display_evidence_chart(
                                df.copy(),
                                metric_name or "Trend",
                                chart_title=_cht,
                                key=f"live_{SESSION_ID}_{len(st.session_state.messages)}_simple_ev",
                            )
                    with st.expander("View data", expanded=False):
                        st.dataframe(
                            format_rate_columns_for_display(df),
                            use_container_width=True,
                            hide_index=True,
                        )
                    assistant_msg["table"] = {
                        "columns": list(df.columns),
                        "data":    df.head(500).values.tolist(),
                    }
                    turn["table"] = assistant_msg["table"]
                turn["sql"] = sql
                assistant_msg["sql"] = sql
                if metric_name:
                    assistant_msg["metric_name"] = metric_name
                _attach_and_render_refine_chips(assistant_msg, qo, "simple")
            else:
                show_step("🧠", f"Planning investigation · `{qo.analysis_type}`")
                show_step("⚡", "Running queries...")
                # Preserve compiler SQL for visibility/debug even when the analyst path
                # returns no usable investigations.
                if sql and sql not in ("__diagnose__", "__analyst__"):
                    assistant_msg["sql"] = sql

                _narrative_thread = get_narrative_thread(SESSION_ID, limit=3)
                report = investigate(
                    question=prompt,
                    qo=qo,
                    db_path=str(DB_PATH),
                    openai_key=None,
                    catalog=CATALOG,
                    event_sampled_values=SAMPLED_VALUES.get("events", {}),
                    user_sampled_values=SAMPLED_VALUES.get("users", {}),
                    hypothesis_doc=hypothesis_doc,
                    narrative_thread=_narrative_thread,
                )
                valid_invs = [
                    inv for inv in (report.investigations or [])
                    if not getattr(inv, "error", None)
                    and getattr(inv, "df", None) is not None
                    and not inv.df.empty
                ]

                # If analyst has no plan (edge case), fall back to single-query path
                if not report.investigations or not valid_invs:
                    show_step("✍️", "Summarising...")
                    df, sql = run_sql_with_retry(sql, prompt, qo)
                    if sql:
                        turn["sql"] = sql
                        assistant_msg["sql"] = sql
                    setattr(qo, "_active_metric_name", metric_name or "")
                    rv = validate_result_shape(qo, df)
                    cmp_intent = bool(getattr(qo, "_comparison_intent_debug", False))
                    cmp_guard = (
                        cmp_intent
                        and getattr(qo, "analysis_type", "") == "metric"
                        and not getattr(qo, "breakdown", None)
                    )
                    if (
                        rv.ok
                        and cmp_guard
                        and df is not None
                        and not df.empty
                        and len(df) < 2
                    ):
                        rv.ok = False
                        rv.reason = "comparison_output_not_comparative"
                        rv.clarify_message = (
                            "I understood this as a comparison, but only one cohort resolved. "
                            "Please specify both sides explicitly."
                        )
                    if not rv.ok:
                        step_box.empty()
                        # After __analyst__, sql was cleared; empty-SQL retry makes df None and
                        # validate_result_shape returns a generic message — prefer analyst narrative.
                        fb_msg = rv.clarify_message or "I may have misinterpreted this question. Could you clarify?"
                        if not (sql or "").strip() and report and (report.narrative or "").strip():
                            fb_msg = report.narrative.strip()
                        st.info(fb_msg)
                        assistant_msg["content"] = fb_msg
                        turn["answer"] = assistant_msg["content"]
                        if not (sql or "").strip() and report and report.investigations:
                            fail_sql = next(
                                (getattr(i, "sql", "") or "" for i in report.investigations if getattr(i, "sql", "")),
                                "",
                            )
                            if fail_sql:
                                assistant_msg["sql"] = fail_sql
                                turn["sql"] = fail_sql
                        else:
                            turn["sql"] = sql
                            assistant_msg["sql"] = sql
                        turn["memory_summary"] = assistant_msg["content"][:220]
                        _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
                        assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)
                        st.session_state.messages.append(assistant_msg)
                        st.stop()
                    quality_clarify = _analysis_quality_clarify_message(qo, df)
                    if quality_clarify:
                        step_box.empty()
                        st.info(quality_clarify)
                        assistant_msg["content"] = quality_clarify
                        turn["answer"] = assistant_msg["content"]
                        turn["sql"] = sql
                        assistant_msg["sql"] = sql
                        turn["memory_summary"] = assistant_msg["content"][:220]
                        _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
                        assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)
                        st.session_state.messages.append(assistant_msg)
                        st.stop()
                    summary = small_narration(prompt, df, qo=qo, metric_name=metric_name)
                    step_box.empty()
                    st.markdown(summary)
                    assistant_msg["content"] = summary
                    if not df.empty:
                        if _is_scalar_like(df):
                            _render_scalar_metric(df)
                        else:
                            show_chart, _ = _should_show_chart(df, compact=False)
                            if show_chart:
                                _cht2 = evidence_chart_title_for_qo(qo, metric_name) or (
                                    (metric_name or "").strip() or None
                                )
                                if _cht2:
                                    assistant_msg["evidence_chart_title"] = _cht2
                                display_evidence_chart(
                                    df.copy(),
                                    metric_name or "Trend",
                                    chart_title=_cht2,
                                    key=f"live_{SESSION_ID}_{len(st.session_state.messages)}_afb_ev",
                                )
                        with st.expander("View data", expanded=False):
                            st.dataframe(
                                format_rate_columns_for_display(df),
                                use_container_width=True,
                                hide_index=True,
                            )
                        assistant_msg["table"] = {
                            "columns": list(df.columns),
                            "data":    df.head(500).values.tolist(),
                        }
                        turn["table"] = assistant_msg["table"]
                    _attach_and_render_refine_chips(assistant_msg, qo, "analyst_fb")
                else:
                    show_step("✍️", "Synthesising findings...")
                    step_box.empty()

                    if metric_name:
                        st.markdown(
                            f'<div style="font-size:0.78rem;color:#3b82f6;margin-bottom:0.6rem;'
                            f'font-weight:500">📌 {metric_name}</div>',
                            unsafe_allow_html=True,
                        )
                        assistant_msg["metric_name"] = metric_name

                    _render_analyst_report(
                        report, assistant_msg, turn, SESSION_ID, qo=qo,
                    )
                    _attach_and_render_refine_chips(assistant_msg, qo, "analyst")

            turn["answer"]      = assistant_msg.get("content", "")
            turn["memory_summary"] = (assistant_msg.get("content", "") or "")[:220]
            turn["sql"]         = assistant_msg.get("sql")
            turn["metric_name"] = assistant_msg.get("metric_name")
            turn["steps"]       = steps
            if report and getattr(report, "hypothesis_verdict", ""):
                turn["hypothesis_verdict"] = report.hypothesis_verdict
            _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
            assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)

        except Exception as e:
            err = traceback.format_exc()
            st.error(f"Error: {e}")
            assistant_msg["error"] = err
            turn["error"] = err
            turn["steps"] = steps
            _flush_assistant_debug_panel(assistant_msg, sql, metric_name)
            assistant_msg["turn_id"] = save_turn(SESSION_ID, turn)

    st.markdown('</div>', unsafe_allow_html=True)  # close response-card

    # Attach feedback-bar metadata so the bar works on next render
    if qo is not None:
        assistant_msg.setdefault("_question",      prompt)
        assistant_msg.setdefault("_event",         getattr(qo, "event", None))
        assistant_msg.setdefault("_metric_id",     getattr(qo, "metric_id", None))
        assistant_msg.setdefault("_analysis_type", getattr(qo, "analysis_type", None))
    else:
        assistant_msg.setdefault("_question", prompt)

    st.session_state.messages.append(assistant_msg)
    st.rerun()
