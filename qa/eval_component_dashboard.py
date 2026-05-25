"""
eval_component_dashboard.py — Streamlit UI for component-isolated evals.

Shows Query Understanding / Text-to-SQL / Business Correctness results.
Each case card shows: question → router decision (QO slots) → generated SQL → assertion results.

T2S and BC can be re-run directly from the UI (no LLM needed).
QU requires an LLM key — run it from the CLI and results load automatically.

Run:
    streamlit run qa/eval_component_dashboard.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.eval_env import load_project_dotenv
from qa.eval_production import build_metrics_like_chat, configure_sql_guards_from_catalog
from qa.eval_benchmark import load_catalog_and_sampled, OUT_DIR

load_project_dotenv(ROOT)

st.set_page_config(page_title="Component Evals", page_icon="🔬", layout="wide")
st.title("🔬 Component Evals")
st.caption(
    "Three isolated eval layers: **orchestrator slots** → **compiler SQL** → **result semantics**. "
    "T2S and Business Correctness run instantly (no LLM). "
    "Query Understanding needs a CLI run first."
)

catalog, sampled = load_catalog_and_sampled()
configure_sql_guards_from_catalog(catalog)
metrics = build_metrics_like_chat(catalog)


# ── File helpers ──────────────────────────────────────────────────────────────

def _latest(prefix: str) -> dict | None:
    if not OUT_DIR.exists():
        return None
    files = sorted(OUT_DIR.glob(f"{prefix}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return json.loads(files[0].read_text()) if files else None


def _save(report: dict, prefix: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"{prefix}{stamp}.json").write_text(json.dumps(report, indent=2, default=str))


# ── Status cards ──────────────────────────────────────────────────────────────

def _status_card(col, label: str, report: dict | None, needs_llm: bool = False) -> None:
    with col:
        if report is None:
            st.metric(label, "—")
            st.caption("Run from CLI (needs LLM)" if needs_llm else "Click Run below")
            return
        n = report.get("n_cases", 0)
        passed = report.get("pass_count", 0)
        rate = report.get("pass_rate", 0.0)
        ts = report.get("generated_at", "")[:16].replace("T", " ")
        color = "normal" if rate >= 0.9 else ("off" if rate >= 0.75 else "inverse")
        st.metric(label, f"{rate:.0%}  ({passed}/{n})", delta=f"{ts} UTC" if ts else None, delta_color=color)


qu_report    = _latest("qu_eval_")
t2s_report   = _latest("t2s_eval_")
bc_report    = _latest("bc_eval_")
gold_report  = _latest("qu_gold_")

c1, c2, c3, c4 = st.columns(4)
_status_card(c1, "🧠 Query Understanding", qu_report, needs_llm=True)
_status_card(c2, "🏗️ Text-to-SQL", t2s_report)
_status_card(c3, "✅ Business Correctness", bc_report)
_status_card(c4, "🎯 Gold Dataset QU", gold_report, needs_llm=True)

st.divider()


# ── Shared renderers ──────────────────────────────────────────────────────────

_QO_LABELS = {
    "analysis_type": "Analysis type",
    "metric_id": "Metric ID",
    "event": "Event",
    "event_b": "Event B",
    "breakdown": "Breakdown",
    "metric_variant": "Variant",
    "filters": "Filters",
    "filter_excludes": "Exclude filters",
    "time_range_days": "Time window (days)",
    "date_from": "Date from",
    "date_to": "Date to",
    "time_granularity": "Granularity",
    "activation_window_days": "Activation window (days)",
}


def _render_qo(qo_kwargs: dict) -> None:
    """Render QO slots as a clean label → value table."""
    rows = [
        {"Slot": _QO_LABELS.get(k, k), "Value": str(v)}
        for k, v in qo_kwargs.items()
        if v is not None and v != {} and v != []
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, height=min(40 + len(rows) * 35, 280))


def _render_assertions(detail: dict, title: str = "Assertions") -> None:
    rows = []
    for k, v in detail.items():
        if not isinstance(v, dict) or "match" not in v:
            continue
        passed = v.get("match", True)
        extra = ""
        if "expected" in v:
            extra = f"expected {v['expected']}"
        if "got" in v:
            extra += f", got {v['got']}"
        if "got_range" in v:
            extra += f", range {v['got_range']}"
        if "found_in_cte" in v and not passed:
            extra += " (found in CTE)"
        rows.append({"": "✓" if passed else "✗", "Check": k, "Detail": extra.strip(", ")})
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, use_container_width=True, height=min(40 + len(rows) * 35, 240))


def _case_icon(c: dict) -> str:
    return "✅" if c.get("pass") else "❌"


def _case_header(c: dict, score_key: str = "sql_score") -> str:
    icon = _case_icon(c)
    score = c.get(score_key, "")
    score_str = f"  ·  score {score:.2f}" if isinstance(score, float) else ""
    return f"{icon}  {c.get('label', '')}  —  {c.get('question', '')[:70]}{score_str}"


def _render_case_card(c: dict, score_key: str = "sql_score", detail_key: str = "sql_detail") -> None:
    """Full case card: question header + 3-column layout (QO | SQL | Assertions)."""
    col_qo, col_sql, col_assert = st.columns([1, 2, 1])

    with col_qo:
        st.markdown("**Router decision (QO)**")
        qo = c.get("qo_kwargs") or {}
        if qo:
            _render_qo(qo)
        else:
            st.caption("QO kwargs not stored in this result — re-run the eval.")

    with col_sql:
        st.markdown("**Generated SQL**")
        sql = c.get("sql_snippet", "")
        if sql:
            st.code(sql, language="sql")
        elif c.get("expected_route"):
            exp = c.get("expected_route", "")
            actual = c.get("actual_route", "?")
            match = actual == exp
            st.info(
                f"Routes to specialist path: `{actual}`\n\n"
                f"Expected: `{exp}` — {'✓ match' if match else '✗ mismatch'}"
            )
        elif not c.get("sql_compiled", True):
            st.caption(f"No SQL compiled — route: `{c.get('specialist_route', '?')}`")
        else:
            st.caption("SQL not available for this case.")

    with col_assert:
        detail = c.get(detail_key) or {}
        if detail and not detail.get("skipped"):
            st.markdown("**Assertions**")
            _render_assertions(detail)
        else:
            pass_label = "✅ All assertions passed" if c.get("pass") else "No assertions defined"
            st.caption(pass_label)


# ══════════════════════════════════════════════════════════════════════════════
# Tabs
# ══════════════════════════════════════════════════════════════════════════════

tab_qu, tab_t2s, tab_bc, tab_gold = st.tabs([
    "🧠 Query Understanding",
    "🏗️ Text-to-SQL",
    "✅ Business Correctness",
    "🎯 Gold Dataset",
])


# ──────────────────────────────────────────────────────────────────────────────
# TAB 1 — Query Understanding
# ──────────────────────────────────────────────────────────────────────────────
with tab_qu:
    st.markdown(
        "Tests whether the **orchestrator** maps natural-language to the right QO slots. "
        "Run from the CLI: `uv run python -m qa.eval_query_understanding`"
    )

    if qu_report is None:
        st.info("No Query Understanding results yet. Run the CLI command above first.")
    else:
        cases = qu_report.get("cases", [])
        sa = qu_report.get("slot_accuracy", {})

        if sa:
            st.subheader("Slot accuracy")
            slots, accs = list(sa.keys()), list(sa.values())
            colors = ["#2E7D32" if a >= 0.9 else "#F9A825" if a >= 0.7 else "#C62828" for a in accs]
            fig = go.Figure(go.Bar(
                x=accs, y=slots, orientation="h",
                marker_color=colors,
                text=[f"{a:.0%}" for a in accs], textposition="outside",
            ))
            fig.update_layout(
                height=max(200, len(slots) * 40 + 60),
                xaxis=dict(range=[0, 1.15], tickformat=".0%"),
                margin=dict(l=0, r=60, t=10, b=10),
                plot_bgcolor="white", paper_bgcolor="white",
            )
            st.plotly_chart(fig, use_container_width=True)

        filt = st.radio("Show", ["All", "Passed", "Failed"], horizontal=True, key="qu_filt")
        visible = [c for c in cases if filt == "All" or (filt == "Passed") == c.get("pass")]

        for c in visible:
            exp = c.get("expected_analysis_type", "?")
            got = c.get("got_analysis_type", "?")
            mismatch = exp and got and exp != got
            header = (
                f"{'✅' if c.get('pass') else '❌'}  {c.get('question', '')[:80]}"
                f"{'  →  expected `' + exp + '` got `' + got + '`' if mismatch else ''}"
            )
            with st.expander(header, expanded=not c.get("pass")):
                col_slots, col_detail = st.columns([1, 2])
                with col_slots:
                    st.markdown("**Orchestrator output**")
                    orch_rows = [
                        {"Slot": "analysis_type", "Got": got, "Expected": exp, "Match": "✓" if not mismatch else "✗"},
                    ]
                    detail = c.get("gold_qo_detail") or {}
                    for slot, d in detail.items():
                        if isinstance(d, dict) and "expected" in d:
                            orch_rows.append({
                                "Slot": slot,
                                "Got": str(d.get("got")),
                                "Expected": str(d.get("expected")),
                                "Match": "✓" if d.get("match") else "✗",
                            })
                    st.dataframe(pd.DataFrame(orch_rows), hide_index=True, use_container_width=True)

                with col_detail:
                    st.markdown(f"**Question**")
                    st.write(f"`{c.get('question', '')}`")
                    st.markdown(f"**Intent accuracy:** `{c.get('intent_accuracy', '?')}`  ·  **Gold QO:** `{c.get('gold_qo_score', '?')}`")
                    if c.get("error"):
                        st.error(c["error"])


# ──────────────────────────────────────────────────────────────────────────────
# TAB 2 — Text-to-SQL
# ──────────────────────────────────────────────────────────────────────────────
with tab_t2s:
    st.markdown(
        "Tests whether the **compiler** produces structurally correct SQL from a known-good QO. "
        "No LLM needed — runs in under a second."
    )

    if st.button("▶ Run Text-to-SQL Eval now", type="primary", key="run_t2s"):
        from qa.eval_text_to_sql import run_text_to_sql_eval
        with st.spinner("Compiling all canonical QOs and checking SQL patterns…"):
            t2s_report = run_text_to_sql_eval(catalog, sampled, metrics)
            _save(t2s_report, "t2s_eval_")
        st.success(f"Done — {t2s_report['pass_count']}/{t2s_report['n_cases']} passed.")

    if t2s_report is None:
        st.info("No results yet. Click Run above.")
    else:
        cases = t2s_report.get("cases", [])
        m1, m2, m3 = st.columns(3)
        m1.metric("Cases", len(cases))
        m2.metric("Passed", sum(1 for c in cases if c.get("pass")))
        m3.metric("Failed", sum(1 for c in cases if not c.get("pass")))

        filt = st.radio("Show", ["All", "Passed", "Failed"], horizontal=True, key="t2s_filt")
        visible = [c for c in cases if filt == "All" or (filt == "Passed") == c.get("pass")]

        for c in visible:
            with st.expander(_case_header(c, "sql_score"), expanded=not c.get("pass")):
                _render_case_card(c, score_key="sql_score", detail_key="sql_detail")


# ──────────────────────────────────────────────────────────────────────────────
# TAB 3 — Business Correctness
# ──────────────────────────────────────────────────────────────────────────────
with tab_bc:
    st.markdown(
        "Tests whether the **executed result** makes business sense: "
        "rates in bounds, expected columns present, no NULLs. No LLM — runs SQL against the live DB."
    )

    db_path = ROOT / "jupiter.duckdb"
    if not db_path.exists():
        st.warning(f"DuckDB not found at `{db_path}`.")
    else:
        if st.button("▶ Run Business Correctness Eval now", type="primary", key="run_bc"):
            import duckdb as _duckdb
            from qa.eval_business_correctness import run_business_correctness_eval
            with st.spinner("Executing canonical QOs against the DB and checking result semantics…"):
                conn = _duckdb.connect(str(db_path), read_only=True)
                try:
                    bc_report = run_business_correctness_eval(catalog, sampled, metrics, conn)
                finally:
                    conn.close()
                _save(bc_report, "bc_eval_")
            st.success(f"Done — {bc_report['pass_count']}/{bc_report['n_cases']} passed.")

        if bc_report is None:
            st.info("No results yet. Click Run above.")
        else:
            cases = bc_report.get("cases", [])
            m1, m2, m3 = st.columns(3)
            m1.metric("Cases", len(cases))
            m2.metric("Passed", sum(1 for c in cases if c.get("pass")))
            m3.metric("Failed", sum(1 for c in cases if not c.get("pass")))

            filt = st.radio("Show", ["All", "Passed", "Failed"], horizontal=True, key="bc_filt")
            visible = [c for c in cases if filt == "All" or (filt == "Passed") == c.get("pass")]

            for c in visible:
                rows_info = f"  ·  {c.get('row_count', '?')} rows" if c.get("execution_ok") else ""
                header = (
                    f"{'✅' if c.get('pass') else '❌'}  {c.get('label', '')}"
                    f"  —  {c.get('question', '')[:65]}{rows_info}"
                )
                with st.expander(header, expanded=not c.get("pass")):
                    col_qo, col_sql, col_result = st.columns([1, 2, 1])

                    with col_qo:
                        st.markdown("**Router decision (QO)**")
                        qo = c.get("qo_kwargs") or {}
                        if qo:
                            _render_qo(qo)

                    with col_sql:
                        st.markdown("**Generated SQL**")
                        sql = c.get("sql_snippet", "")
                        if sql:
                            st.code(sql, language="sql")
                        elif not c.get("sql_compiled"):
                            st.caption(f"Specialist route: `{c.get('specialist_route', '?')}`")
                        if c.get("execution_error"):
                            st.error(f"SQL error: {c['execution_error']}")

                    with col_result:
                        st.markdown("**Result**")
                        if c.get("execution_ok"):
                            st.write(f"Rows: `{c.get('row_count', '?')}`")
                            cols = c.get("columns", [])
                            if cols:
                                st.write(f"Columns: `{', '.join(cols)}`")
                        detail = c.get("result_detail") or {}
                        if detail and not detail.get("skipped"):
                            _render_assertions(detail)


# ──────────────────────────────────────────────────────────────────────────────
# TAB 4 — Gold Dataset QU Eval
# ──────────────────────────────────────────────────────────────────────────────
with tab_gold:
    st.markdown(
        "Scores the **orchestrator** on a labeled dataset of 70+ queries across 5 dimensions: "
        "**intent · metric · filters · time-range · ambiguity**. "
        "Uses Gemini Flash Lite — run from CLI: `uv run python -m qa.eval_qu_gold`"
    )

    if gold_report is None:
        st.info("No Gold QU results yet. Run: `uv run python -m qa.eval_qu_gold`")
    else:
        cases = gold_report.get("cases", [])
        da = gold_report.get("dim_accuracy", {})

        # ── Dimension accuracy bar chart ───────────────────────────────────────
        if da:
            st.subheader("Dimension accuracy")
            _DIM_WEIGHTS = {"intent": 0.40, "metric": 0.25, "filters": 0.20, "time_range": 0.10, "ambiguity": 0.05}
            dims = list(da.keys())
            accs = [da[d] for d in dims]
            weights = [_DIM_WEIGHTS.get(d, 0) for d in dims]
            colors = ["#2E7D32" if a >= 0.9 else "#F9A825" if a >= 0.7 else "#C62828" for a in accs]
            labels = [f"{a:.0%}  (weight {w:.0%})" for a, w in zip(accs, weights)]
            fig = go.Figure(go.Bar(
                x=accs, y=dims, orientation="h",
                marker_color=colors,
                text=labels, textposition="outside",
            ))
            fig.update_layout(
                height=max(200, len(dims) * 50 + 60),
                xaxis=dict(range=[0, 1.3], tickformat=".0%"),
                margin=dict(l=0, r=160, t=10, b=10),
                plot_bgcolor="white", paper_bgcolor="white",
            )
            st.plotly_chart(fig, use_container_width=True)

        # ── Summary metrics ────────────────────────────────────────────────────
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Cases", len(cases))
        m2.metric("Passed", sum(1 for c in cases if c.get("pass")))
        m3.metric("Failed", sum(1 for c in cases if not c.get("pass")))
        m4.metric("Avg score", f"{gold_report.get('avg_composite_score', 0):.2f}")

        # ── Case cards ─────────────────────────────────────────────────────────
        filt = st.radio("Show", ["All", "Passed", "Failed"], horizontal=True, key="gold_filt")
        visible = [c for c in cases if filt == "All" or (filt == "Passed") == c.get("pass")]

        for c in visible:
            icon = "✅" if c.get("pass") else "❌"
            score = c.get("composite_score", 0)
            header = f"{icon}  {c.get('query', '')[:80]}  ·  score {score:.2f}"
            with st.expander(header, expanded=not c.get("pass")):
                col_gold, col_got, col_scores = st.columns([1, 1, 1])

                with col_gold:
                    st.markdown("**Gold (expected)**")
                    rows = []
                    for field, label in [
                        ("gold_analysis_type", "Intent"),
                        ("gold_metric_id", "Metric"),
                        ("gold_filters", "Filters"),
                        ("gold_time_range_days", "Time (days)"),
                        ("gold_date_from", "Date from"),
                        ("gold_date_to", "Date to"),
                        ("gold_breakdown", "Breakdown"),
                    ]:
                        val = c.get(field)
                        if val is not None and val != {} and val != "":
                            rows.append({"Field": label, "Gold": str(val)})
                    if c.get("should_clarify"):
                        rows.append({"Field": "Should clarify", "Gold": "Yes"})
                    if rows:
                        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                                     height=min(40 + len(rows) * 35, 240))

                with col_got:
                    st.markdown("**Got (orchestrator)**")
                    rows = []
                    for field, label in [
                        ("got_analysis_type", "Intent"),
                        ("got_metric_id", "Metric"),
                        ("got_filters", "Filters"),
                        ("got_time_range_days", "Time (days)"),
                        ("got_date_from", "Date from"),
                        ("got_date_to", "Date to"),
                        ("got_breakdown", "Breakdown"),
                    ]:
                        val = c.get(field)
                        if val is not None and val != {} and val != "":
                            rows.append({"Field": label, "Got": str(val)})
                    if rows:
                        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                                     height=min(40 + len(rows) * 35, 240))

                with col_scores:
                    st.markdown("**Dimension scores**")
                    dim_scores = c.get("dim_scores") or {}
                    if dim_scores:
                        score_rows = [
                            {"": "✓" if v >= 0.9 else ("~" if v >= 0.5 else "✗"),
                             "Dimension": k, "Score": f"{v:.0%}"}
                            for k, v in dim_scores.items()
                        ]
                        st.dataframe(pd.DataFrame(score_rows), hide_index=True,
                                     use_container_width=True,
                                     height=min(40 + len(score_rows) * 35, 240))
                    if c.get("error"):
                        st.error(c["error"])
