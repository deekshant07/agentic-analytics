"""ui/debug_panel.py — Debug pipeline panel + answer contract rendering."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
from typing import Any, Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from ui.interpretation import interpretation_light_html

def _canonical_metric_contract(qo, metrics: list[dict]) -> dict:
    """Build debug contract for metric semantics after all overrides/hydration."""
    contract = {
        "metric_id": getattr(qo, "metric_id", None),
        "analysis_type": getattr(qo, "analysis_type", None),
        "resolved_event": getattr(qo, "event", None),
        "resolved_event_b": getattr(qo, "event_b", None),
        "resolved_filters": getattr(qo, "filters", {}) or {},
        "resolved_date_from": getattr(qo, "date_from", None),
        "resolved_date_to": getattr(qo, "date_to", None),
        "resolved_time_range_days": getattr(qo, "time_range_days", None),
        "resolved_time_granularity": getattr(qo, "time_granularity", None),
        "resolved_retention_window_days": getattr(qo, "retention_window_days", None),
    }
    sem = getattr(qo, "_query_semantics", None)
    if sem is not None:
        try:
            contract["query_semantics"] = sem.to_dict()
        except Exception:
            contract["query_semantics"] = str(sem)
    mid = contract["metric_id"]
    if not mid:
        return contract
    m = next((x for x in (metrics or []) if x.get("id") == mid), None)
    if not m:
        return contract
    hint = (m.get("sql") or "").strip()
    contract["metric_name"] = m.get("name")
    contract["metric_sql_hint"] = hint[:1200]
    evs = re.findall(r'["`]?event_name["`]?\s*=\s*\'([^\']+)\'', hint, flags=re.IGNORECASE)
    if evs:
        contract["hint_events"] = sorted(set(evs))
    conds = re.findall(
        r'(?:\b\w+\.)?["`]?([a-zA-Z_][a-zA-Z0-9_]*)["`]?\s*=\s*\'([^\']+)\'',
        hint,
        flags=re.IGNORECASE,
    )
    fixed = {}
    for col, val in conds:
        cl = (col or "").strip().lower()
        if cl != "event_name":
            fixed[cl] = val
    if fixed:
        contract["hint_fixed_filters"] = fixed
    return contract


def _format_time_window_phrase(qo) -> str:
    if getattr(qo, "date_from", None) and getattr(qo, "date_to", None):
        return f"{qo.date_from} → {qo.date_to} (end exclusive)"
    gran = getattr(qo, "time_granularity", None) or "day"
    days = getattr(qo, "time_range_days", None) or 30
    return f"Last ~{days} days · grain={gran}"


def _build_public_answer_contract(qo, metrics: list[dict], metric_name: str | None = None) -> dict:
    """User-visible interpretation contract (metric, window, filters) — not debug-only."""
    c = _canonical_metric_contract(qo, metrics)
    ev = c.get("resolved_event")
    evb = c.get("resolved_event_b")
    parts = {
        "analysis": getattr(qo, "analysis_type", None),
        "metric": metric_name or c.get("metric_name"),
        "metric_id": c.get("metric_id"),
        "event": ev,
        "event_b": evb,
        "breakdown": getattr(qo, "breakdown", None),
        "funnel_steps": list(getattr(qo, "funnel_steps", None) or []),
        "filters": c.get("resolved_filters") or {},
        "time_window": _format_time_window_phrase(qo),
        "time_source": getattr(qo, "time_source", None),
        "retention_window_days": getattr(qo, "retention_window_days", None),
    }
    return parts


def _answer_contract_html(contract: dict) -> str:
    """Light-theme panel for the main response card (readable on white)."""
    return interpretation_light_html(contract)


def _render_answer_contract_section(qo, metric_name: str | None = None) -> dict:
    contract = _build_public_answer_contract(qo, st.session_state.get("metrics", []), metric_name)
    st.markdown(_answer_contract_html(contract), unsafe_allow_html=True)
    return contract


def _debug_html_escape(s: str) -> str:
    return html.escape(s or "", quote=True)


def _debug_trunc(s: str, limit: int = 480) -> str:
    s = s or ""
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _debug_kv(label: str, value, mono: bool = False, escape_value: bool = True) -> str:
    raw = "—" if value is None else str(value)
    val_str = _debug_html_escape(raw) if escape_value else raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    style = "font-family:monospace;font-size:0.82rem" if mono else "font-size:0.82rem"
    return (
        f'<div style="display:flex;gap:0.5rem;line-height:1.5">'
        f'<span style="color:#64748b;min-width:160px;flex-shrink:0">{_debug_html_escape(label)}</span>'
        f'<span style="{style};color:#e2e8f0;word-break:break-word">{val_str}</span>'
        f'</div>'
    )


def _debug_step_html(
    number: int,
    title: str,
    status: str,          # "ok" | "warn" | "skip" | "block"
    rows: list[str],      # list of HTML row strings
    extra_html: str = "",
) -> str:
    color_map = {"ok": "#22c55e", "warn": "#f59e0b", "skip": "#64748b", "block": "#ef4444"}
    icon_map  = {"ok": "✓", "warn": "!", "skip": "–", "block": "✗"}
    color = color_map.get(status, "#64748b")
    icon  = icon_map.get(status, "·")
    rows_html = "".join(rows)
    return f"""
<div style="display:flex;gap:0.75rem;margin-bottom:0.6rem">
  <div style="display:flex;flex-direction:column;align-items:center;gap:0">
    <div style="width:22px;height:22px;border-radius:50%;background:{color};color:#000;
                display:flex;align-items:center;justify-content:center;
                font-size:0.72rem;font-weight:700;flex-shrink:0">{icon}</div>
    <div style="width:2px;flex:1;background:#1e293b;min-height:8px"></div>
  </div>
  <div style="flex:1;padding-bottom:0.6rem">
    <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.3rem">
      <span style="font-size:0.65rem;color:#475569;font-family:monospace">S{number}</span>
      <span style="font-size:0.85rem;font-weight:600;color:#cbd5e1">{title}</span>
    </div>
    <div class="dp-box" style="font-size:0.82rem">
      {rows_html}
      {extra_html}
    </div>
  </div>
</div>"""


def _debug_candidates_table(candidates: list[dict]) -> str:
    if not candidates:
        return ""
    rows = []
    for c in candidates:
        is_winner = c.get("priority") == max(x.get("priority", 0) for x in candidates)
        bg = "#1e3a2f" if is_winner else "transparent"
        border = "border:1px solid #22c55e;border-radius:4px;" if is_winner else ""
        rows.append(
            f'<tr style="background:{bg};{border}">'
            f'<td style="padding:2px 8px;color:#94a3b8;font-family:monospace">{c.get("name","")}</td>'
            f'<td style="padding:2px 8px;color:#7c3aed;font-family:monospace">{c.get("route","")}</td>'
            f'<td style="padding:2px 8px;color:#f59e0b;font-family:monospace">p={c.get("priority","")}</td>'
            f'<td style="padding:2px 8px;color:#22c55e;font-family:monospace">{round(c.get("confidence",0),2)}</td>'
            f'<td style="padding:2px 8px;color:#64748b;font-size:0.78rem">{c.get("reason","")}</td>'
            f'</tr>'
        )
    return (
        '<table style="width:100%;border-collapse:collapse;margin-top:0.3rem">'
        '<tr>'
        '<th style="text-align:left;color:#475569;font-size:0.72rem;padding:2px 8px">Name</th>'
        '<th style="text-align:left;color:#475569;font-size:0.72rem;padding:2px 8px">Route</th>'
        '<th style="text-align:left;color:#475569;font-size:0.72rem;padding:2px 8px">Priority</th>'
        '<th style="text-align:left;color:#475569;font-size:0.72rem;padding:2px 8px">Conf</th>'
        '<th style="text-align:left;color:#475569;font-size:0.72rem;padding:2px 8px">Reason</th>'
        '</tr>'
        + "".join(rows)
        + '</table>'
    )


def _parse_flow_arrow_step(raw: str) -> tuple[str, str]:
    """Split ``'orchestrate → metric'`` into (stage, outcome)."""
    s = str(raw).strip()
    sep = "\u2192"  # →
    if sep in s:
        left, _, right = s.partition(sep)
        return left.strip(), right.strip()
    if "->" in s:
        left, _, right = s.partition("->")
        return left.strip(), right.strip()
    return s, ""


def _flow_fingerprint(flow: list[str]) -> str:
    return hashlib.sha256("\n".join(str(s) for s in flow).encode("utf-8", errors="replace")).hexdigest()[:16]


def _pipeline_flow_sankey_figure(flow: list[str]) -> Optional[Any]:
    """
    Interactive flow: each ``component_flow`` line is one node, consecutive nodes
    linked (Plotly Sankey).  Lazy-imports plotly so imports succeed if plotly is absent.
    """
    try:
        import plotly.graph_objects as go
    except ImportError:
        return None
    if not flow:
        return None
    n = len(flow)
    labels: list[str] = []
    for raw in flow:
        stage, outcome = _parse_flow_arrow_step(str(raw))
        if outcome:
            labels.append(f"{stage}\n{outcome}")
        else:
            labels.append(str(raw).strip()[:96])
    colors: list[str] = []
    for raw in flow:
        r = str(raw).lower()
        if "skipped" in r or "(skipped)" in r:
            colors.append("#475569")
        elif "resolver" in r:
            colors.append("#7c3aed")
        elif "compiler" in r:
            colors.append("#059669")
        elif "orchestrate" in r:
            colors.append("#2563eb")
        else:
            colors.append("#0ea5e9")

    if n == 1:
        fig = go.Figure()
        fig.add_annotation(
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            text=labels[0],
            showarrow=False,
            font=dict(size=13, color="#e2e8f0"),
            align="center",
        )
        fig.update_xaxes(visible=False, range=[0, 1])
        fig.update_yaxes(visible=False, range=[0, 1])
    else:
        src = list(range(n - 1))
        tgt = list(range(1, n))
        fig = go.Figure(
            data=[
                go.Sankey(
                    arrangement="snap",
                    valueformat=".0f",
                    node=dict(
                        pad=20,
                        thickness=24,
                        line=dict(color="rgba(148,163,184,0.35)", width=1),
                        label=labels,
                        color=colors,
                    ),
                    link=dict(
                        source=src,
                        target=tgt,
                        value=[1.0] * len(src),
                        color=["rgba(148,163,184,0.35)"] * len(src),
                    ),
                )
            ]
        )

    fig.update_layout(
        title=dict(
            text="Execution path (Sankey · hover a node)",
            font=dict(size=13, color="#cbd5e1"),
            x=0.02,
            xanchor="left",
        ),
        font=dict(family="ui-sans-serif, system-ui, sans-serif", size=11, color="#e2e8f0"),
        paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a",
        height=min(520, 160 + n * 48),
        margin=dict(l=8, r=8, t=48, b=8),
    )
    return fig


def _component_flow_graph_html(flow: list[str]) -> str:
    """
    Horizontal node graph for ``_debug_flow`` / ``component_flow`` (read left → right).
    """
    if not flow:
        return ""
    parts: list[str] = []
    for i, raw in enumerate(flow):
        stage, outcome = _parse_flow_arrow_step(raw)
        raw_s = str(raw).strip()
        muted = "skipped" in raw_s.lower() or "(skipped)" in raw_s.lower()
        op = "0.52" if muted else "1"
        if not outcome:
            body = (
                f'<div style="font-size:0.78rem;color:#cbd5e1;font-family:ui-monospace,monospace;'
                f'line-height:1.25;word-break:break-word">{_debug_html_escape(raw_s)}</div>'
            )
        else:
            stage_esc = _debug_html_escape(stage)
            out_esc = _debug_html_escape(outcome)
            body = (
                f'<div style="font-size:0.58rem;letter-spacing:0.07em;color:#64748b;'
                f'text-transform:uppercase;margin-bottom:0.22rem;word-break:break-word">{stage_esc}</div>'
                f'<div style="font-size:0.78rem;color:#f1f5f9;font-family:ui-monospace,monospace;'
                f'line-height:1.25;word-break:break-word">{out_esc}</div>'
            )
        parts.append(
            f'<div style="opacity:{op};flex:0 1 auto;min-width:5.2rem;max-width:12rem;'
            f'padding:0.42rem 0.5rem;border-radius:9px;border:1px solid #334155;'
            f'background:linear-gradient(165deg,#1e293b 0%,#0f172a 100%);text-align:left">{body}</div>'
        )
        if i < len(flow) - 1:
            parts.append(
                '<div aria-hidden="true" style="display:flex;align-items:center;align-self:center;'
                'color:#475569;font-size:1rem;font-weight:300;padding:0 0.05rem 0.9rem">→</div>'
            )
    return (
        '<div style="margin-bottom:0.95rem;padding:0.65rem 0.45rem 0.55rem;'
        'background:rgba(15,23,42,0.75);border-radius:10px;border:1px solid #1e293b">'
        '<div style="font-size:0.62rem;letter-spacing:0.1em;color:#64748b;'
        'text-transform:uppercase;margin:0 0 0.45rem 0.15rem">Route graph · read left → right</div>'
        '<div style="display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:flex-start;'
        'column-gap:0.05rem;row-gap:0.45rem">'
        f'{"".join(parts)}'
        "</div></div>"
    )


def _build_full_debug_signature(qo) -> dict:
    """Single dict for pipeline UI + optional raw JSON export (JSON-serializable values)."""
    if not qo:
        return {}
    pre = getattr(qo, "_qo_pre_override_debug", None) or {}
    cand = getattr(qo, "_route_candidates_debug", []) or []
    cand_json = json.loads(json.dumps(cand, default=str)) if cand else []
    return {
        "user_prompt": getattr(qo, "_debug_user_prompt", None),
        "orchestrator_input": getattr(qo, "_debug_orchestrator_input", None),
        "component_flow": list(getattr(qo, "_debug_flow", None) or []),
        "_pre_override": pre,
        "clarify_ctx_injected": getattr(qo, "_clarify_ctx_injected_debug", False),
        "hypothesis_generated": getattr(qo, "_hypothesis_generated_debug", False),
        "route": getattr(qo, "_route_debug", "compiler"),
        "effective_execution_mode": getattr(qo, "_effective_execution_mode_debug", "compiler"),
        "route_confidence": getattr(qo, "_route_confidence_debug", None),
        "route_reasons": getattr(qo, "_route_reasons_debug", []) or [],
        "route_conflicts": getattr(qo, "_route_conflicts_debug", []) or [],
        "route_candidates": cand_json,
        "route_custom_events": getattr(qo, "_route_custom_events_debug", []) or [],
        "comparison_intent": getattr(qo, "_comparison_intent_debug", False),
        "comparison_entities": getattr(qo, "_comparison_entities_debug", []) or [],
        "comparison_guard_status": getattr(qo, "_comparison_guard_status_debug", "not_applicable"),
        "arbiter_allow_execute": getattr(qo, "_arbiter_allow_debug", True),
        "arbiter_confidence": getattr(qo, "_arbiter_confidence_debug", None),
        "arbiter_reasons": getattr(qo, "_arbiter_reasons_debug", []) or [],
        "analysis_type": getattr(qo, "analysis_type", None),
        "metric_id": getattr(qo, "metric_id", None),
        "event": getattr(qo, "event", None),
        "event_b": getattr(qo, "event_b", None),
        "filters": getattr(qo, "filters", None) or {},
        "date_from": getattr(qo, "date_from", None),
        "date_to": getattr(qo, "date_to", None),
        "time_range_days": getattr(qo, "time_range_days", None),
        "time_granularity": getattr(qo, "time_granularity", None),
        "time_source": getattr(qo, "time_source", None),
        "retention_window_days": getattr(qo, "retention_window_days", None),
        "breakdown": getattr(qo, "breakdown", None),
        "funnel_steps": list(getattr(qo, "funnel_steps", None) or []),
        "metric_variant": getattr(qo, "metric_variant", None),
        "metric_value_col": getattr(qo, "metric_value_col", None),
        "metric_status_col": getattr(qo, "metric_status_col", None),
        "metric_status_target": getattr(qo, "metric_status_target", None),
        "narration_metric_col": getattr(qo, "_narration_metric_col_debug", None),
        "narration_value_mode": getattr(qo, "_narration_value_mode_debug", None),
    }


def _finalize_debug_signature_outputs(
    assistant_msg: dict,
    *,
    sql: str | None = None,
    metric_name: str | None = None,
) -> None:
    """Attach what the user ultimately saw (narrative, SQL flags) to the debug signature."""
    sig = assistant_msg.get("debug_signature")
    if not sig:
        return
    content = assistant_msg.get("content") or ""
    eff_sql = assistant_msg.get("sql") if assistant_msg.get("sql") is not None else sql
    bad_sentinel = eff_sql in (None, "", "__diagnose__", "__analyst__")
    sig["outputs"] = {
        "narrative_preview": _debug_trunc(content, 600),
        "sql_attached": bool(eff_sql and not bad_sentinel),
        "sql_first_line": (
            (eff_sql.split("\n")[0][:160] if eff_sql and not bad_sentinel else None)
        ),
        "metric_name": assistant_msg.get("metric_name") or metric_name,
        "had_error": bool(assistant_msg.get("error")),
        "agent_steps": list(assistant_msg.get("steps") or []),
    }
    table = assistant_msg.get("table") or {}
    if table:
        cols = list(table.get("columns") or [])
        rows = list(table.get("data") or [])
        sig["outputs"]["table_columns"] = cols
        sig["outputs"]["table_row_count"] = len(rows)
        if rows:
            sig["outputs"]["table_first_row"] = rows[0]


def _format_full_debug_export(debug_sig: dict, metric_contract: dict, sql: str | None) -> str:
    """Single text blob for download / clipboard (JSON + SQL)."""
    raw_payload = {
        "component_flow": debug_sig.get("component_flow"),
        "signature": {k: v for k, v in debug_sig.items() if not str(k).startswith("_")},
        "signature_internal": {k: v for k, v in debug_sig.items() if str(k).startswith("_")},
        "metric_contract": metric_contract,
    }
    parts = [json.dumps(raw_payload, indent=2, default=str)]
    if sql and sql not in ("__diagnose__", "__analyst__"):
        parts.append("\n--- SQL ---\n")
        parts.append(sql)
    return "".join(parts)


def _clipboard_copy_trace_button(export_text: str) -> None:
    """One-click copy via iframe (clipboard API); falls back message if blocked."""
    b64 = base64.b64encode(export_text.encode("utf-8")).decode("ascii")
    uid = secrets.token_hex(4)
    components.html(
        f"""<!DOCTYPE html><html><body style="margin:0;font-family:system-ui,sans-serif;">
<button id="cpbtn_{uid}" type="button"
  style="padding:0.35rem 0.85rem;cursor:pointer;border-radius:8px;border:1px solid #cbd5e1;
  background:#f1f5f9;color:#0f172a;font-size:0.82rem;font-weight:600;">
  Copy full trace
</button>
<span id="cphint_{uid}" style="font-size:12px;margin-left:8px;color:#64748b;"></span>
<script>
const b64 = "{b64}";
const btn = document.getElementById("cpbtn_{uid}");
const hint = document.getElementById("cphint_{uid}");
btn.addEventListener("click", async () => {{
  try {{
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const txt = new TextDecoder("utf-8").decode(bytes);
    await navigator.clipboard.writeText(txt);
    hint.textContent = "Copied.";
  }} catch (e) {{
    hint.textContent = "Blocked — use Download";
  }}
}});
</script></body></html>""",
        height=52,
    )


def _render_semantics_debug_panel(
    debug_sig: dict,
    metric_contract: dict,
    sql: str | None,
    *,
    expanded: bool = True,
    widget_key_suffix: str | None = None,
) -> None:
    """One place for flow trace + pipeline + raw JSON (tabs avoid nested-expanders glitches)."""
    with st.expander("Pipeline debug (full flow)", expanded=expanded):
        export_text = _format_full_debug_export(debug_sig, metric_contract, sql)
        trace_key = widget_key_suffix or secrets.token_hex(8)
        dl_col, cp_col = st.columns([1, 1])
        with dl_col:
            st.download_button(
                label="Download full trace (.txt)",
                data=export_text,
                file_name="pipeline_debug_trace.txt",
                mime="text/plain",
                use_container_width=True,
                key=f"dl_trace_{trace_key}",
            )
        with cp_col:
            _clipboard_copy_trace_button(export_text)
        tab_summary, tab_trace, tab_raw = st.tabs(["Summary (v2)", "Visual trace", "Raw JSON"])
        with tab_summary:
            _render_debug_summary_v2(debug_sig, metric_contract, sql=sql)
        with tab_trace:
            _render_debug_pipeline(debug_sig, metric_contract, sql=sql, widget_key=trace_key)
        with tab_raw:
            raw_payload = {
                "component_flow": debug_sig.get("component_flow"),
                "signature": {k: v for k, v in debug_sig.items() if not str(k).startswith("_")},
                "signature_internal": {k: v for k, v in debug_sig.items() if str(k).startswith("_")},
                "metric_contract": metric_contract,
            }
            st.json(raw_payload)


def _render_debug_summary_v2(debug_sig: dict, metric_contract: dict, sql: str | None = None) -> None:
    """
    Compact, stage-based debug summary:
      Intent → Routing → Compile → Execution → Narration → Guardrails
    """
    out = debug_sig.get("outputs") or {}
    table_cols = out.get("table_columns") or []
    row_count = out.get("table_row_count")
    metric_name = metric_contract.get("metric_name") or debug_sig.get("metric_id") or "—"

    def _bool(v):
        return "yes" if v else "no"

    compiler_mode = "compiler"
    if (sql or "").lstrip().startswith("-- % of"):
        compiler_mode = "prebuilt_scalar_ratio_hint"
    elif sql and "numerator_users" in sql and "denominator_users" in sql and " AS pct" in sql:
        compiler_mode = "ratio_trend_from_contract"
    elif debug_sig.get("effective_execution_mode"):
        compiler_mode = debug_sig.get("effective_execution_mode")

    narration_col = debug_sig.get("narration_metric_col")
    narration_mode = debug_sig.get("narration_value_mode")
    expected_rate = bool(
        metric_contract.get("metric_id")
        and any(k in str(metric_contract.get("metric_id", "")).lower() for k in ("rate", "ratio", "retention", "activation", "churn"))
    )
    rate_cols = {"pct", "status_rate_pct", "retention_pct", "d1_retention_pct", "d7_retention_pct", "d30_retention_pct"}
    rate_mismatch = expected_rate and narration_col and str(narration_col).lower() not in rate_cols
    comparison_intent = bool(debug_sig.get("comparison_intent"))
    comparison_output_bad = bool(comparison_intent and isinstance(row_count, int) and row_count < 2)

    st.markdown("**1) Intent**")
    st.markdown(
        f"- prompt: `{debug_sig.get('user_prompt') or '—'}`\n"
        f"- parsed: `{debug_sig.get('analysis_type')}` · metric_id=`{debug_sig.get('metric_id')}` · event=`{debug_sig.get('event')}`\n"
        f"- time: granularity=`{debug_sig.get('time_granularity')}` · range_days=`{debug_sig.get('time_range_days')}` · source=`{debug_sig.get('time_source')}`"
    )

    st.markdown("**2) Routing**")
    st.markdown(
        f"- route: `{debug_sig.get('route')}` · mode=`{debug_sig.get('effective_execution_mode')}`\n"
        f"- confidence: `{debug_sig.get('route_confidence')}` · reasons: `{', '.join(debug_sig.get('route_reasons') or []) or '—'}`\n"
        f"- gate allow execute: `{_bool(debug_sig.get('arbiter_allow_execute', True))}` · gate reasons: `{', '.join(debug_sig.get('arbiter_reasons') or []) or '—'}`\n"
        f"- comparison intent: `{_bool(debug_sig.get('comparison_intent'))}` · entities: `{', '.join(debug_sig.get('comparison_entities') or []) or '—'}` · guard: `{debug_sig.get('comparison_guard_status')}`"
    )

    st.markdown("**3) Compile**")
    st.markdown(
        f"- compiler mode: `{compiler_mode}`\n"
        f"- metric contract: `{metric_name}`\n"
        f"- SQL first line: `{(out.get('sql_first_line') or '—')}`"
    )

    st.markdown("**4) Execution**")
    st.markdown(
        f"- table rows: `{row_count if row_count is not None else '—'}`\n"
        f"- columns: `{', '.join(table_cols) if table_cols else '—'}`"
    )

    st.markdown("**5) Narration**")
    st.markdown(
        f"- selected metric column: `{narration_col or '—'}`\n"
        f"- value mode: `{narration_mode or '—'}`\n"
        f"- preview: `{_debug_trunc(out.get('narrative_preview') or '—', 260)}`"
    )

    st.markdown("**6) Guardrails**")
    guard_msgs = []
    if expected_rate:
        guard_msgs.append("expected rate metric: yes")
    if rate_mismatch:
        guard_msgs.append("RATE_NARRATION_COLUMN_MISMATCH")
    if compiler_mode == "prebuilt_scalar_ratio_hint" and debug_sig.get("time_granularity") in ("week", "month"):
        guard_msgs.append("SCALAR_RATIO_HINT_USED_FOR_BUCKETED_QUERY")
    if comparison_output_bad:
        guard_msgs.append("COMPARISON_OUTPUT_NOT_COMPARATIVE")
    warning_tokens = {
        "RATE_NARRATION_COLUMN_MISMATCH",
        "SCALAR_RATIO_HINT_USED_FOR_BUCKETED_QUERY",
        "COMPARISON_OUTPUT_NOT_COMPARATIVE",
    }
    hard_fail = any(msg in warning_tokens for msg in guard_msgs)
    soft_warn = (not hard_fail) and len(guard_msgs) > 1
    if hard_fail:
        badge_bg = "#7f1d1d"
        badge_fg = "#fecaca"
        label = "RED"
    elif soft_warn:
        badge_bg = "#78350f"
        badge_fg = "#fde68a"
        label = "YELLOW"
    else:
        badge_bg = "#14532d"
        badge_fg = "#bbf7d0"
        label = "GREEN"
    if not guard_msgs:
        guard_msgs.append("no guard warnings")
    badge_html = (
        f'<span style="display:inline-block;padding:0.08rem 0.45rem;border-radius:999px;'
        f'font-size:0.68rem;font-weight:700;letter-spacing:0.04em;'
        f'background:{badge_bg};color:{badge_fg};margin-right:0.4rem">{label}</span>'
    )
    st.markdown(f"{badge_html}{_debug_html_escape(' · '.join(guard_msgs))}", unsafe_allow_html=True)


def _sql_for_debug_panel(sql: str | None) -> str | None:
    if not sql or sql in ("__diagnose__", "__analyst__"):
        return None
    return sql


def _flush_assistant_debug_panel(
    assistant_msg: dict,
    sql: str | None,
    metric_name: str | None = None,
    *,
    widget_key_suffix: str | None = None,
) -> None:
    """When Debug semantics is on, finalize outputs and render the structured panel once."""
    if not st.session_state.get("debug_semantics"):
        return
    sig, mc = assistant_msg.get("debug_signature"), assistant_msg.get("metric_contract")
    if not sig or mc is None:
        return
    _finalize_debug_signature_outputs(
        assistant_msg, sql=_sql_for_debug_panel(sql), metric_name=metric_name
    )
    suffix = widget_key_suffix or f"live_{id(assistant_msg)}"
    _render_semantics_debug_panel(
        assistant_msg["debug_signature"],
        mc,
        _sql_for_debug_panel(sql),
        expanded=True,
        widget_key_suffix=suffix,
    )


def _render_debug_pipeline(
    debug_sig: dict,
    metric_contract: dict,
    sql: str | None = None,
    *,
    widget_key: str = "",
) -> None:
    """Render the structured pipeline trace panel (Plotly Sankey + HTML detail)."""
    pre = debug_sig.get("_pre_override") or {}
    post_analysis  = debug_sig.get("analysis_type")
    post_metric_id = debug_sig.get("metric_id")
    post_event     = debug_sig.get("event")
    post_gran      = debug_sig.get("time_granularity")
    post_range     = debug_sig.get("time_range_days")
    post_datefrom  = debug_sig.get("date_from")
    post_dateto    = debug_sig.get("date_to")

    wrapper_open = (
        '<div class="debug-pipeline-trace">'
        '<div style="font-size:0.65rem;letter-spacing:0.1em;color:#94a3b8;'
        'text-transform:uppercase;margin-bottom:0.85rem">Pipeline Trace</div>'
    )
    wrapper_close = '</div>'

    flow = debug_sig.get("component_flow") or []
    flow_block = ""
    if flow:
        items = "".join(
            f'<li style="margin:0.12rem 0;font-family:ui-monospace,monospace;font-size:0.78rem;'
            f'color:#cbd5e1">{_debug_html_escape(str(i + 1))}. {_debug_html_escape(str(s))}</li>'
            for i, s in enumerate(flow)
        )
        flow_block = (
            '<div class="dp-flow">'
            '<div style="font-size:0.62rem;letter-spacing:0.1em;color:#94a3b8;'
            'text-transform:uppercase;margin-bottom:0.35rem">Component call order (same path)</div>'
            f'<ol style="margin:0;padding-left:1.15rem">{items}</ol></div>'
        )

    steps_html = []

    # ── Step 1 · Inputs (what you typed vs what the orchestrator saw) ─────────
    clarify_injected = debug_sig.get("clarify_ctx_injected", False)
    hyp_generated    = debug_sig.get("hypothesis_generated", False)
    up = debug_sig.get("user_prompt")
    oi = debug_sig.get("orchestrator_input")
    orch_extra = ""
    if oi and len(str(oi)) > 360:
        orch_extra = (
            '<details style="margin-top:0.35rem">'
            '<summary style="font-size:0.75rem;color:#3b82f6;cursor:pointer">'
            "Full orchestrator input</summary>"
            f'<pre style="white-space:pre-wrap;word-break:break-all;font-size:0.74rem;'
            f'color:#a5b4fc;margin:0.3rem 0 0">{_debug_html_escape(str(oi))}</pre></details>'
        )
    rows = [
        _debug_kv("User prompt (raw)", _debug_trunc(up, 500) if up else "—", mono=True),
        _debug_kv(
            "Orchestrator input",
            _debug_trunc(oi, 360) if oi else "—",
            mono=True,
        ),
    ]
    steps_html.append(
        _debug_step_html(
            1,
            "Inputs",
            "ok",
            rows,
            extra_html=orch_extra,
        )
    )
    rows2 = [
        _debug_kv("Clarification context", "injected ✓" if clarify_injected else "none"),
        _debug_kv("Hypothesis doc passed in", "yes ✓" if hyp_generated else "no (orchestrator only)"),
    ]
    steps_html.append(_debug_step_html(2, "Pre-orchestrator context", "ok", rows2))

    # ── Step 2 · Orchestrator output (pre-override) ───────────────────────────
    pre_analysis  = pre.get("analysis_type") or "—"
    pre_metric_id = pre.get("metric_id") or "—"
    pre_event     = pre.get("event") or "—"
    pre_gran      = pre.get("time_granularity") or "—"
    pre_range     = pre.get("time_range_days") or "—"
    pre_datefrom  = pre.get("date_from") or "—"
    pre_dateto    = pre.get("date_to") or "—"
    pre_breakdown = pre.get("breakdown") or "—"
    pre_funnel    = " → ".join(pre.get("funnel_steps") or []) or "—"
    rows = [
        _debug_kv("analysis_type", pre_analysis, mono=True),
        _debug_kv("metric_id", pre_metric_id, mono=True),
        _debug_kv("event", pre_event, mono=True),
        _debug_kv("breakdown", pre_breakdown, mono=True),
        _debug_kv("funnel_steps", pre_funnel, mono=True),
        _debug_kv("time_granularity", pre_gran, mono=True),
        _debug_kv("time_range_days", str(pre_range)),
        _debug_kv("date_from", pre_datefrom, mono=True),
        _debug_kv("date_to", pre_dateto, mono=True),
    ]
    steps_html.append(_debug_step_html(3, "Orchestrator output (pre-override)", "ok", rows))

    # ── Step 3 · Time overrides ───────────────────────────────────────────────
    overrides_applied: list[tuple[str, str, str]] = []
    for field, old, new in [
        ("analysis_type",   pre.get("analysis_type"),   post_analysis),
        ("metric_id",       pre.get("metric_id"),        post_metric_id),
        ("time_granularity", pre.get("time_granularity"), post_gran),
        ("time_range_days", pre.get("time_range_days"),  post_range),
        ("date_from",       pre.get("date_from"),        post_datefrom),
        ("date_to",         pre.get("date_to"),          post_dateto),
    ]:
        if str(old or "") != str(new or ""):
            overrides_applied.append((field, str(old or "—"), str(new or "—")))
    if overrides_applied:
        rows = [
            (
                f'<div style="display:flex;gap:0.4rem;line-height:1.5;font-size:0.82rem">'
                f'<span style="color:#64748b;min-width:160px;flex-shrink:0">{f}</span>'
                f'<span style="color:#ef4444;font-family:monospace;text-decoration:line-through">{old}</span>'
                f'<span style="color:#94a3b8;margin:0 0.25rem">→</span>'
                f'<span style="color:#22c55e;font-family:monospace">{new}</span>'
                f'</div>'
            )
            for f, old, new in overrides_applied
        ]
        steps_html.append(_debug_step_html(4, "Time overrides", "warn", rows))
    else:
        steps_html.append(_debug_step_html(4, "Time overrides", "skip",
                                           [_debug_kv("Changes", "none")]))

    # ── Step 4 · Resolver ─────────────────────────────────────────────────────
    route         = debug_sig.get("route") or "—"
    conf          = debug_sig.get("route_confidence")
    reasons       = debug_sig.get("route_reasons") or []
    conflicts     = debug_sig.get("route_conflicts") or []
    custom_events = debug_sig.get("route_custom_events") or []
    candidates    = debug_sig.get("route_candidates") or []
    conf_str      = f"{round(float(conf), 2)}" if conf is not None else "—"
    rows = [
        _debug_kv("Winner route", route, mono=True),
        _debug_kv("Confidence", conf_str),
        _debug_kv("Reason codes", ", ".join(reasons) if reasons else "—", mono=True),
        _debug_kv("Conflicts", ", ".join(conflicts) if conflicts else "none"),
        _debug_kv("Matched custom events", ", ".join(custom_events) if custom_events else "none"),
    ]
    steps_html.append(_debug_step_html(5, "Resolver", "ok", rows,
                                       extra_html=_debug_candidates_table(candidates)))

    # ── Step 5 · Arbiter ─────────────────────────────────────────────────────
    arb_allow   = debug_sig.get("arbiter_allow_execute", True)
    arb_conf    = debug_sig.get("arbiter_confidence")
    arb_reasons = debug_sig.get("arbiter_reasons") or []
    arb_conf_str = f"{round(float(arb_conf), 2)}" if arb_conf is not None else "—"
    arb_status = "ok" if arb_allow else "block"
    rows = [
        _debug_kv("Allow execute", "✓ yes" if arb_allow else "✗ blocked"),
        _debug_kv("Confidence", arb_conf_str),
        _debug_kv("Reasons", ", ".join(arb_reasons) if arb_reasons else "—", mono=True),
    ]
    steps_html.append(_debug_step_html(6, "Arbiter", arb_status, rows))

    # ── Step 6 · Compiler / execution mode ───────────────────────────────────
    exec_mode   = debug_sig.get("effective_execution_mode") or route
    post_event_b = debug_sig.get("event_b") or "—"
    filters      = debug_sig.get("filters") or {}
    filt_str     = ", ".join(f"{k}={v}" for k, v in filters.items()) if filters else "none"
    rows = [
        _debug_kv("Execution mode", exec_mode, mono=True),
        _debug_kv("analysis_type", str(post_analysis or "—"), mono=True),
        _debug_kv("metric_variant", str(debug_sig.get("metric_variant") or "—"), mono=True),
        _debug_kv("metric_value_col", str(debug_sig.get("metric_value_col") or "—"), mono=True),
        _debug_kv("metric_status_col", str(debug_sig.get("metric_status_col") or "—"), mono=True),
        _debug_kv("metric_status_target", str(debug_sig.get("metric_status_target") or "—"), mono=True),
        _debug_kv("event", str(post_event or "—"), mono=True),
        _debug_kv("event_b", str(post_event_b), mono=True),
        _debug_kv("filters", filt_str, mono=True),
        _debug_kv("time_granularity", str(post_gran or "—"), mono=True),
        _debug_kv("breakdown", str(debug_sig.get("breakdown") or "—"), mono=True),
    ]
    sql_preview = ""
    if sql:
        sql_esc = sql[:600].replace("<", "&lt;").replace(">", "&gt;")
        if len(sql) > 600:
            sql_esc += "\n…"
        sql_preview = (
            f'<details style="margin-top:0.4rem">'
            f'<summary style="font-size:0.78rem;color:#3b82f6;cursor:pointer">SQL preview</summary>'
            f'<pre style="margin:0.3rem 0 0;font-size:0.78rem;color:#a5f3fc;'
            f'white-space:pre-wrap;word-break:break-all">{sql_esc}</pre>'
            f'</details>'
        )
    steps_html.append(_debug_step_html(7, "Compiler", "ok", rows, extra_html=sql_preview))

    # ── Step 8 · Metric contract ─────────────────────────────────────────────
    mc_rows = [
        _debug_kv("metric_id", metric_contract.get("metric_id") or "—", mono=True),
        _debug_kv("metric_name", metric_contract.get("metric_name") or "—"),
        _debug_kv("resolved_event", metric_contract.get("resolved_event") or "—", mono=True),
        _debug_kv("hint_events", ", ".join(metric_contract.get("hint_events") or []) or "—"),
        _debug_kv("fixed_filters", str(metric_contract.get("hint_fixed_filters") or {}) or "—"),
    ]
    steps_html.append(_debug_step_html(8, "Metric contract", "ok", mc_rows))

    # ── Step 9 · What was shown to the user (filled after response completes) ─
    out = debug_sig.get("outputs") or {}
    if out:
        steps_list = out.get("agent_steps") or []
        steps_txt = " → ".join(str(s) for s in steps_list[:12]) if steps_list else "—"
        if len(steps_list) > 12:
            steps_txt += " …"
        o_rows = [
            _debug_kv("Narrative preview", out.get("narrative_preview") or "—"),
            _debug_kv("SQL attached to message", "yes" if out.get("sql_attached") else "no"),
            _debug_kv("SQL first line", out.get("sql_first_line") or "—", mono=True),
            _debug_kv("Metric name (UI)", out.get("metric_name") or "—", mono=True),
            _debug_kv("Had error", "yes" if out.get("had_error") else "no"),
            _debug_kv("Agent step log", steps_txt),
        ]
        steps_html.append(_debug_step_html(9, "Delivered output", "ok", o_rows))

    st.markdown(wrapper_open, unsafe_allow_html=True)
    if flow:
        fig = _pipeline_flow_sankey_figure(flow)
        if fig is not None:
            fk = f"pl_pipe_{widget_key or _flow_fingerprint(flow)}"
            st.plotly_chart(
                fig,
                use_container_width=True,
                key=fk,
                config={
                    "displayModeBar": True,
                    "scrollZoom": True,
                    "displaylogo": False,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                },
            )
            st.caption(
                "Tip: hover nodes for full labels · drag to pan · box-zoom from the mode bar · "
                "scroll wheel zooms when the chart is focused."
            )
            with st.expander("Static rail diagram (same path, HTML)", expanded=False):
                st.markdown(_component_flow_graph_html(flow), unsafe_allow_html=True)
        else:
            st.info(
                "Install **plotly** (project dependency — `uv sync` / `pip install -e .`) "
                "for an interactive Sankey. Static rail below."
            )
            st.markdown(_component_flow_graph_html(flow), unsafe_allow_html=True)
        rows_tbl = []
        for i, s in enumerate(flow, start=1):
            stg, out = _parse_flow_arrow_step(str(s))
            rows_tbl.append(
                {
                    "#": i,
                    "Stage": stg if out else "(log)",
                    "Outcome / detail": out if out else str(s).strip(),
                }
            )
        st.dataframe(
            pd.DataFrame(rows_tbl),
            use_container_width=True,
            hide_index=True,
            height=min(320, 80 + len(rows_tbl) * 38),
        )

    full_html = flow_block + "".join(steps_html) + wrapper_close
    st.markdown(full_html, unsafe_allow_html=True)


