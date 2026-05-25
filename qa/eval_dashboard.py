"""
eval_dashboard.py — Streamlit UI for benchmark reports.

Run:
  streamlit run qa/eval_dashboard.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.eval_env import load_project_dotenv

load_project_dotenv(ROOT)
OUT_DIR = ROOT / "qa" / "eval_results"

st.set_page_config(page_title="Eval Dashboard", page_icon="🧪", layout="wide")
st.title("Eval Dashboard")

if not OUT_DIR.exists():
    st.warning("No eval results found. Run: `python qa/eval_benchmark.py`")
    st.stop()

files = sorted(
    [p for p in OUT_DIR.iterdir()
     if p.is_file() and not p.name.endswith(".log") and not p.name.endswith(".txt")],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
if not files:
    st.warning("No eval result files found.")
    st.stop()

selected = st.selectbox("Eval run", files, format_func=lambda p: p.name)
payload  = json.loads(selected.read_text())
meta     = payload.get("run_metadata", {})

if int(meta.get("invalid_api_key_error_count", 0) or 0):
    st.error("Cases failed due to invalid OpenAI API key — scores are not meaningful.")
elif int(meta.get("connection_error_count", 0) or 0):
    st.error("Cases failed because OpenAI was unreachable — scores are not meaningful.")
elif int(meta.get("fatal_error_count", 0) or 0):
    st.warning("Some cases hit runtime errors — inspect before trusting scores.")

# ── Aggregate headline ────────────────────────────────────────────────────────
agg = payload.get("aggregate", {})
n   = agg.get("n_cases", 0)
passed = agg.get("pass_count", 0)
failed = n - passed
pct  = round(100 * passed / n, 1) if n else 0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Questions run", n)
c2.metric("Passed", f"{passed} / {n}  ({pct}%)")
c3.metric("Failed", failed)
c4.metric("Avg overall score", f"{agg.get('avg_answer_relevance', 0.0):.2f}  (pass = 0.75)")

# ── Answer renderer — chart + narration + SQL ────────────────────────────────
def _render_answer(case: dict) -> None:
    """Render the chart the system generated, the narration, and the SQL."""
    stages     = case.get("stages", {})
    sql        = stages.get("compile", {}).get("sql", "")
    summary    = case.get("summary", "")
    chart_json = case.get("chart_json")
    ex         = stages.get("execution", {})
    sample_rows = ex.get("sample_rows", [])
    cols       = ex.get("columns", [])

    has_chart = bool(chart_json)
    has_data  = bool(sample_rows)
    has_sql   = bool(sql) and sql not in ("__analyst__", "")

    if not has_chart and not has_data and not has_sql:
        st.caption("No output — query did not produce results.")
        return

    col_chart, col_text = st.columns([3, 2])

    with col_chart:
        if has_chart:
            fig = go.Figure(json.loads(chart_json))
            fig.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=320)
            st.plotly_chart(fig, use_container_width=True)
        elif has_data:
            st.caption("Chart not available for this run — showing raw data")
            st.dataframe(pd.DataFrame(sample_rows), hide_index=True)
        else:
            st.caption("No chart — query routed to specialist path or returned no rows.")

    with col_text:
        st.markdown("**Narration**")
        st.write(summary or "*(no summary generated)*")

    if has_sql:
        with st.expander("SQL", expanded=False):
            st.code(sql, language="sql")


# ── Diagnosis helper ──────────────────────────────────────────────────────────
def _diagnose(case: dict) -> dict:
    sc      = case.get("scores", {})
    stages  = case.get("stages", {})
    stage   = case.get("failure_stage") or "unknown"
    hf      = bool(case.get("hard_fail"))
    sql     = stages.get("compile", {}).get("sql", "")
    ex      = stages.get("execution", {})
    row_count   = ex.get("row_count", 0)
    exec_ok     = ex.get("ok", False)
    exec_error  = ex.get("error", "")
    sample_rows = ex.get("sample_rows", [])
    cols        = ex.get("columns", [])
    gold_qo_d   = sc.get("gold_qo_detail", {})
    gold_sql_d  = stages.get("gold_sql", {})
    jrel  = sc.get("llm_judge_relevance")
    jcomp = sc.get("llm_judge_completeness")
    ar    = sc.get("answer_relevance", 0.0)
    qo_raw   = stages.get("orchestrator_raw", {})
    qo_fixed = stages.get("qo_after_fixups", {})

    if sql == "__analyst__":
        ia = sc.get("intent_accuracy", 0)
        qr = sc.get("query_routing", 0)
        return dict(
            headline="Routed to specialist path — only routing was scored",
            badge="🔀 Routing",
            what_happened=(
                f"The question was sent to the `__analyst__` specialist (retention/funnel). "
                f"No SQL was compiled or executed."
            ),
            why_failed=(
                f"Score is capped at `0.5 × intent_accuracy + 0.5 × routing` = **{ar:.2f}**. "
                f"Intent accuracy = {ia:.2f}, routing score = {qr:.2f}. "
                "For vague or bad-window questions this ceiling can't be raised without structural changes."
            ),
            fix_hint=(
                "Add `expected_analysis_type` to this case so the routing assertion fires. "
                "If the question is genuinely unanswerable (bad time window, too vague), mark it as a known ceiling."
            ),
            sql_snippet=None,
            data=None,
        )

    if hf and stage == "compiler":
        failed = [k for k, v in gold_sql_d.items() if isinstance(v, dict) and not v.get("match", True)]
        passed_a = [k for k, v in gold_sql_d.items() if isinstance(v, dict) and v.get("match", True)]
        return dict(
            headline="SQL structure assertion failed (hard fail — judge was fooled)",
            badge="🔴 Hard Fail",
            what_happened=(
                f"SQL compiled and **returned {row_count} rows**. "
                f"Judge scored relevance={jrel:.1f}, completeness={jcomp:.1f} — it thought the answer was correct. "
                "But the hard assertion caught a structural bug the judge missed."
            ),
            why_failed=(
                "**Failed assertions:**\n"
                + "".join(f"\n- ✗ `{a}`" for a in failed)
                + ("\n\n**Passed assertions:**\n" + "".join(f"\n- ✓ `{a}`" for a in passed_a) if passed_a else "")
            ),
            fix_hint=(
                "The compiler is generating wrong SQL structure even though it looks right to the eye. "
                "Find the forbidden pattern in the compiled SQL and trace it back to the compiler function."
            ),
            sql_snippet=sql,
            data={"columns": cols, "rows": sample_rows[:3]},
        )

    if not exec_ok and exec_error:
        return dict(
            headline="SQL failed to execute",
            badge="💥 Execution Error",
            what_happened="The query compiled but crashed when DuckDB tried to run it.",
            why_failed=f"**Error:** `{exec_error}`",
            fix_hint="Fix the SQL generation in the compiler for this query type. Add an L3 regression test.",
            sql_snippet=sql,
            data=None,
        )

    if stage == "orchestrator":
        mismatches = {
            k: v for k, v in gold_qo_d.items()
            if isinstance(v, dict) and not v.get("match", True)
        }
        matches = {
            k: v for k, v in gold_qo_d.items()
            if isinstance(v, dict) and v.get("match", True)
        }
        mismatch_text = "\n".join(
            f"- `{k}`: expected **{v.get('expected')}** — got **{v.get('got')}**"
            for k, v in mismatches.items()
        )
        match_text = "\n".join(f"- `{k}`: ✓ `{v.get('got')}`" for k, v in matches.items())
        return dict(
            headline="Orchestrator picked the wrong metric or event",
            badge="🎯 Wrong Intent",
            what_happened=(
                f"The orchestrator parsed the question but mapped it to the wrong slot(s). "
                f"SQL ran and returned {row_count} rows, but based on wrong inputs."
            ),
            why_failed=(
                "**Slot mismatches:**\n" + mismatch_text
                + ("\n\n**Correct slots:**\n" + match_text if match_text else "")
            ),
            fix_hint=(
                "Add this case as a regression test (L2). "
                "Improve the orchestrator prompt or catalog description for this metric/event to make the right choice unambiguous."
            ),
            sql_snippet=sql,
            data={"columns": cols, "rows": sample_rows[:3]},
        )

    if stage == "answer_quality":
        fixup_diff = {
            k: {"before": qo_raw.get(k), "after": qo_fixed.get(k)}
            for k in set(list(qo_raw.keys()) + list(qo_fixed.keys()))
            if qo_raw.get(k) != qo_fixed.get(k)
        }
        return dict(
            headline="Pipeline correct — judge found the answer incomplete or irrelevant",
            badge="📝 Answer Quality",
            what_happened=(
                f"Everything worked: SQL compiled, returned **{row_count} rows**, "
                f"correct metric/event selected. "
                f"But the LLM judge scored: relevance = **{jrel:.2f}**, completeness = **{jcomp:.2f}**."
            ),
            why_failed=(
                f"The judge thought the answer didn't fully address the question. "
                f"{'Low completeness (jcomp=' + str(jcomp) + ') usually means only part of the question was answered.' if jcomp is not None and jcomp < 0.5 else ''} "
                f"{'Low relevance (jrel=' + str(jrel) + ') usually means the summary was off-topic.' if jrel is not None and jrel < 0.5 else ''}"
                + (f"\n\n**Fixup chain changed:** {json.dumps(fixup_diff, indent=2)}" if fixup_diff else "")
            ),
            fix_hint=(
                "Check the narrative/summary generation for this `analysis_type`. "
                "If completeness is the issue, the system may be answering only one part of the question. "
                "This can also be judge variance — run the case again to see if the score is stable."
            ),
            sql_snippet=sql,
            data={"columns": cols, "rows": sample_rows[:3]},
        )

    # Fallback: low AR, no specific stage
    fixup_diff = {
        k: {"before": qo_raw.get(k), "after": qo_fixed.get(k)}
        for k in set(list(qo_raw.keys()) + list(qo_fixed.keys()))
        if qo_raw.get(k) != qo_fixed.get(k)
    }
    return dict(
        headline=f"Score too low ({ar:.2f}) — no single stage is the clear culprit",
        badge="🟡 Low Score",
        what_happened=(
            f"SQL ran and returned {row_count} rows. "
            f"Judge: relevance = {jrel if jrel is not None else 'N/A'}, "
            f"completeness = {jcomp if jcomp is not None else 'N/A'}. "
            f"No gold assertions failed."
        ),
        why_failed=(
            f"Overall score {ar:.2f} fell just below 0.75. "
            "Common causes: multi-intent question (system answered only one metric), "
            "adversarial/ambiguous phrasing, or judge variance."
            + (f"\n\n**Fixup chain changed:** {json.dumps(fixup_diff, indent=2)}" if fixup_diff else "")
        ),
        fix_hint=(
            "If this is multi-intent or adversarial, mark it as a known ceiling. "
            "If it's close (AR > 0.70), add `gold_qo` or `expected_analysis_type` to make "
            "the score deterministic and reduce judge noise."
        ),
        sql_snippet=sql,
        data={"columns": cols, "rows": sample_rows[:3]},
    )


# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_fail, tab_all, tab_chart, tab_inspect = st.tabs([
    f"🔴 Failures ({failed})",
    "📋 All cases",
    "📊 Score chart",
    "🔍 Inspect case",
])

cases = payload.get("cases", [])

# ── TAB 1: Failure diagnosis ──────────────────────────────────────────────────
with tab_fail:
    failing = [c for c in cases if not c.get("pass")]
    if not failing:
        st.success("All cases passed.")
    else:
        st.caption(
            "Each card shows what the system actually did, why the score was low, and what to change. "
            "Hard fails (🔴) are deterministic bugs. Other failures may include judge noise."
        )
        for i, case in enumerate(failing):
            d = _diagnose(case)
            idx = cases.index(case) + 1
            ar  = case["scores"].get("answer_relevance", 0)

            with st.expander(
                f"{d['badge']}  #{idx} — {case['question'][:80]}{'…' if len(case['question']) > 80 else ''}  ·  score {ar:.2f}",
                expanded=(i == 0),
            ):
                st.markdown(f"**{d['headline']}**")
                st.divider()

                col_a, col_b = st.columns([1, 1])
                with col_a:
                    st.markdown("**What the system did**")
                    st.markdown(d["what_happened"])

                with col_b:
                    st.markdown("**Why it failed**")
                    st.markdown(d["why_failed"])

                st.markdown("**Fix hint**")
                st.info(d["fix_hint"])

                st.markdown("**What the system returned**")
                _render_answer(case)


# ── TAB 2: All cases table ────────────────────────────────────────────────────
with tab_all:
    _STAGE_REASON = {
        "orchestrator":       "Wrong metric or event selected",
        "compiler":           "SQL structure bug",
        "execution":          "SQL execution error",
        "execution_semantic": "Result values out of bounds",
        "answer_quality":     "Answer incomplete/irrelevant to judge",
        "unknown":            "Score too low — no specific stage",
    }
    rows = []
    for i, case in enumerate(cases, start=1):
        sc    = case.get("scores", {})
        tags  = case.get("tags") or (case.get("expected") or {}).get("tags") or []
        stage = case.get("failure_stage") or ""
        hf    = bool(case.get("hard_fail"))
        passed = bool(case.get("pass"))
        reason = ("" if passed else
                  "Hard fail: SQL assertion" if hf else
                  _STAGE_REASON.get(stage, "Score too low"))
        rows.append({
            "#":              i,
            "Passed?":        passed,
            "Score":          round(sc.get("answer_relevance", 0), 2),
            "Question":       case.get("question", ""),
            "Category":       ", ".join(tags),
            "Compiled & ran": round(sc.get("query_correctness", 0), 2),
            "Judge relevant": round(sc.get("llm_judge_relevance") or 0, 2),
            "Right metric":   round(sc.get("gold_qo_score", 0), 2),
            "Why failed":     reason,
        })
    df = pd.DataFrame(rows)
    filt = st.selectbox("Show", ["All", "Passed", "Failed"], key="all_filter")
    if filt == "Passed":
        df = df[df["Passed?"]]
    elif filt == "Failed":
        df = df[~df["Passed?"]]
    st.dataframe(df, use_container_width=True, hide_index=True)

# ── TAB 3: Score chart ────────────────────────────────────────────────────────
with tab_chart:
    _WEIGHTS = {
        "query_correctness":      0.30,
        "llm_judge_relevance":    0.15,
        "gold_qo_score":          0.10,
        "llm_judge_completeness": 0.10,
        "query_routing":          0.10,
        "gold_sql_score":         0.10,
        "gold_result_score":      0.10,
        "data_presentation":      0.05,
    }
    _PLAIN = {
        "query_correctness":      "Compiled & ran (30%)",
        "llm_judge_relevance":    "Answer relevant (15%)",
        "gold_qo_score":          "Right metric/event (10%)",
        "llm_judge_completeness": "Answer complete (10%)",
        "query_routing":          "Correct route (10%)",
        "gold_sql_score":         "SQL structure ok (10%)",
        "gold_result_score":      "Result values ok (10%)",
        "data_presentation":      "Good presentation (5%)",
    }
    _COLORS = {
        "query_correctness":      "#4C8BF5",
        "llm_judge_relevance":    "#34A853",
        "gold_qo_score":          "#FBBC04",
        "llm_judge_completeness": "#81C995",
        "query_routing":          "#8AB4F8",
        "gold_sql_score":         "#F6AE2D",
        "gold_result_score":      "#F28B82",
        "data_presentation":      "#CE93D8",
    }

    chart_rows = []
    for i, case in enumerate(cases, start=1):
        sc = case.get("scores", {})
        chart_rows.append({
            "idx": i, "pass": bool(case.get("pass")),
            "hard_fail": bool(case.get("hard_fail")),
            "question": case.get("question", ""),
            "overall_score": sc.get("answer_relevance", 0),
            **{k: sc.get(k) or 0 for k in _WEIGHTS},
        })
    df_c = pd.DataFrame(chart_rows)

    col_s, col_f = st.columns([2, 2])
    with col_s:
        sort_opt = st.radio("Sort", ["Failures first", "Score ↑", "Score ↓", "Original order"], horizontal=True)
    with col_f:
        cf = st.radio("Show", ["All", "Passed", "Failed"], horizontal=True, key="chart_filt")

    if cf == "Passed":
        df_c = df_c[df_c["pass"]]
    elif cf == "Failed":
        df_c = df_c[~df_c["pass"]]
    if sort_opt == "Failures first":
        df_c = df_c.sort_values(["pass", "overall_score"], ascending=[True, True])
    elif sort_opt == "Score ↑":
        df_c = df_c.sort_values("overall_score")
    elif sort_opt == "Score ↓":
        df_c = df_c.sort_values("overall_score", ascending=False)

    records = df_c.to_dict(orient="records")

    def _qlabel(r):
        q = r["question"][:52] + "…" if len(r["question"]) > 52 else r["question"]
        icon = "✓" if r["pass"] else ("✗!" if r["hard_fail"] else "✗")
        return f"#{r['idx']} {icon}  {q}"

    y_labels = [_qlabel(r) for r in records]
    y_colors = ["#2E7D32" if r["pass"] else ("#E65100" if r["hard_fail"] else "#C62828") for r in records]

    fig = go.Figure()
    for key, weight in _WEIGHTS.items():
        contribs = [round(r[key] * weight, 4) for r in records]
        hover = [
            f"<b>{_PLAIN[key]}</b><br>Raw: {r[key]:.2f}  ×  {int(weight*100)}%  =  {contribs[i]:.3f}"
            for i, r in enumerate(records)
        ]
        fig.add_trace(go.Bar(
            name=_PLAIN[key], x=contribs, y=y_labels, orientation="h",
            marker_color=_COLORS[key],
            hovertemplate="%{customdata}<extra></extra>", customdata=hover,
        ))
    # Score label at bar end
    fig.add_trace(go.Scatter(
        x=[r["overall_score"] + 0.015 for r in records], y=y_labels, mode="text",
        text=[f"<b>{r['overall_score']:.2f}</b>" for r in records],
        textfont=dict(size=11, color=y_colors), showlegend=False, hoverinfo="skip",
    ))
    fig.add_vline(x=0.75, line_dash="dash", line_color="black", line_width=1.5,
                  annotation_text="pass (0.75)", annotation_position="top right", annotation_font_size=11)
    fig.update_layout(
        barmode="stack",
        height=max(400, len(df_c) * 38 + 100),
        margin=dict(l=0, r=60, t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=11)),
        xaxis=dict(title="Contribution to score", range=[0, 1.05], tickformat=".2f"),
        yaxis=dict(tickfont=dict(size=11)),
        plot_bgcolor="white", paper_bgcolor="white",
    )
    st.caption(
        "Each segment = one check's weighted contribution. Hover for details. "
        "Green = passed · Red = failed · Orange = hard fail."
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Raw score heatmap (un-weighted)", expanded=False):
        hm_cols  = list(_WEIGHTS.keys()) + ["overall_score"]
        hm_plain = [_PLAIN.get(c, c) for c in hm_cols[:-1]] + ["Overall score"]
        hm_data  = df_c[hm_cols].round(2)
        fig2 = go.Figure(go.Heatmap(
            z=hm_data.values, x=hm_plain, y=y_labels,
            colorscale="RdYlGn", zmin=0, zmax=1,
            text=hm_data.values.round(2), texttemplate="%{text}",
            hovertemplate="<b>%{y}</b><br>%{x}: %{z:.2f}<extra></extra>",
        ))
        fig2.update_layout(
            height=max(400, len(df_c) * 38 + 100),
            margin=dict(l=0, r=20, t=20, b=80),
            xaxis=dict(tickangle=-35, tickfont=dict(size=11)),
            plot_bgcolor="white", paper_bgcolor="white",
        )
        st.plotly_chart(fig2, use_container_width=True)

# ── Feedback store (shared across tabs) ──────────────────────────────────────
FEEDBACK_FILE = OUT_DIR / f"feedback_{selected.stem}.json"

def _load_feedback() -> dict:
    if FEEDBACK_FILE.exists():
        try:
            return json.loads(FEEDBACK_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_feedback(store: dict) -> None:
    FEEDBACK_FILE.write_text(json.dumps(store, indent=2))

feedback_store = _load_feedback()

# ── TAB 4: Case inspector ─────────────────────────────────────────────────────
with tab_inspect:
    _WEIGHTS_I = {
        "query_correctness": 0.30, "llm_judge_relevance": 0.15,
        "gold_qo_score": 0.10, "llm_judge_completeness": 0.10,
        "query_routing": 0.10, "gold_sql_score": 0.10,
        "gold_result_score": 0.10, "data_presentation": 0.05,
    }
    _PLAIN_I = {
        "query_correctness": "Compiled & ran",
        "llm_judge_relevance": "Answer relevant",
        "gold_qo_score": "Right metric/event",
        "llm_judge_completeness": "Answer complete",
        "query_routing": "Correct route",
        "gold_sql_score": "SQL structure ok",
        "gold_result_score": "Result values ok",
        "data_presentation": "Good presentation",
    }

    # Case picker — selectbox with question text so you don't need to know the number
    fb_status = {
        k.split(":", 1)[0]: v.get("label", "")
        for k, v in feedback_store.items()
    }
    case_options = [
        f"#{i}  {'✓' if c.get('pass') else '✗'}  "
        f"[{fb_status.get(str(i), '·')}]  "
        f"{c.get('question', '')[:70]}"
        for i, c in enumerate(cases, 1)
    ]
    sel = st.selectbox("Select question", case_options, key="inspect_sel")
    idx = int(sel.split()[0].lstrip("#"))
    case   = cases[idx - 1]
    sc     = case.get("scores", {})
    stages = case.get("stages", {})
    passed = bool(case.get("pass"))

    # Status bar
    score_val = sc.get("answer_relevance", 0)
    status_md = f"{'✅ Passed' if passed else '❌ Failed'}  ·  score **{score_val:.2f}**"
    if case.get("hard_fail"):
        status_md += "  🔴 **Hard fail**"
    st.markdown(status_md)

    if not passed:
        d = _diagnose(case)
        st.info(f"**{d['headline']}**\n\n{d['fix_hint']}")

    # ── Sub-tabs: Output / Scores / Trace ─────────────────────────────────────
    sub_out, sub_scores, sub_trace = st.tabs(["Output", "Scores & Assertions", "Pipeline trace"])

    with sub_out:
        _render_answer(case)
        ex = stages.get("execution", {})
        if ex.get("ok") is False and ex.get("error"):
            st.error(f"Execution error: {ex.get('error')}")

    with sub_scores:
        score_rows = [
            {
                "Check": _PLAIN_I.get(k, k),
                "Weight": f"{int(w*100)}%",
                "Raw score": round(sc.get(k) or 0, 3),
                "Contribution": round((sc.get(k) or 0) * w, 3),
            }
            for k, w in _WEIGHTS_I.items()
        ]
        score_rows.append({"Check": "TOTAL", "Weight": "100%", "Raw score": "", "Contribution": round(score_val, 3)})
        st.dataframe(pd.DataFrame(score_rows), hide_index=True, use_container_width=True)

        # Gold SQL assertion details
        gold_sql_d = {k: v for k, v in stages.get("gold_sql", {}).items() if k != "skipped"}
        if gold_sql_d:
            st.markdown("**SQL assertions**")
            a_rows = [
                {
                    "Assertion": k,
                    "Pass?": "✓" if (v.get("match", True) if isinstance(v, dict) else True) else "✗",
                    "Detail": str(v),
                }
                for k, v in gold_sql_d.items()
            ]
            st.dataframe(pd.DataFrame(a_rows), hide_index=True, use_container_width=True)

        # Gold result assertion details
        gold_res_d = {k: v for k, v in stages.get("gold_result", {}).items() if k != "skipped"}
        if gold_res_d:
            st.markdown("**Result assertions**")
            r_rows = []
            for k, v in gold_res_d.items():
                if isinstance(v, dict):
                    detail = ""
                    if "expected" in v:
                        detail = f"expected {v['expected']}"
                    if "got" in v:
                        detail += f", got {v['got']}"
                    if "got_range" in v:
                        detail += f", range {v['got_range']}"
                    r_rows.append({"Assertion": k, "Pass?": "✓" if v.get("match", True) else "✗", "Detail": detail or str(v)})
            if r_rows:
                st.dataframe(pd.DataFrame(r_rows), hide_index=True, use_container_width=True)

        # Orchestrator slot summary
        orch = stages.get("orchestrator", {})
        st.markdown("**Orchestrator slots**")
        st.markdown(
            f"- Analysis type: `{orch.get('analysis_type')}`  ·  "
            f"Metric: `{orch.get('metric_id')}`  ·  Event: `{orch.get('event')}`\n"
            f"- Breakdown: `{orch.get('breakdown')}`  ·  Filters: `{orch.get('filters')}`\n"
            f"- Time: `{orch.get('date_from')}` → `{orch.get('date_to')}`"
        )

    with sub_trace:
        exec_no_rows = {k: v for k, v in (stages.get("execution") or {}).items() if k != "sample_rows"}
        st.json({
            "orchestrator_raw":   stages.get("orchestrator_raw"),
            "qo_after_fixups":    stages.get("qo_after_fixups"),
            "orchestrator_final": stages.get("orchestrator", {}),
            "resolver":           stages.get("resolver", {}),
            "execution":          exec_no_rows,
        })
        with st.expander("Full trace JSON", expanded=False):
            st.json(case)

    # ── Feedback ──────────────────────────────────────────────────────────────
    st.divider()
    st.markdown("**Feedback on this case**")
    fb_key   = f"{idx}:{case.get('question', '')}"
    existing = feedback_store.get(fb_key, {})

    _LABEL_OPTS = [
        "— not reviewed —",
        "✓ Correct",
        "~ Partially correct",
        "✗ Wrong output",
        "✗ Wrong analysis type",
        "✗ Missing / wrong filter",
        "✗ SQL structural bug",
        "✗ Other issue",
    ]
    col_fb_l, col_fb_r = st.columns([1, 2])
    with col_fb_l:
        cur_label = existing.get("label", "— not reviewed —")
        sel_label = st.selectbox(
            "Label",
            _LABEL_OPTS,
            index=_LABEL_OPTS.index(cur_label) if cur_label in _LABEL_OPTS else 0,
            key=f"fb_lbl_{idx}",
        )
    with col_fb_r:
        sel_notes = st.text_area(
            "Notes",
            value=existing.get("notes", ""),
            placeholder="What went wrong? What should have happened?",
            height=80,
            key=f"fb_notes_{idx}",
        )

    if st.button("Save feedback", key=f"fb_save_{idx}"):
        if sel_label == "— not reviewed —" and not sel_notes.strip():
            feedback_store.pop(fb_key, None)
        else:
            feedback_store[fb_key] = {"label": sel_label, "notes": sel_notes.strip()}
        _save_feedback(feedback_store)
        st.success(f"Saved feedback for case #{idx}.")

    if feedback_store:
        with st.expander(f"All feedback collected ({len(feedback_store)} cases)", expanded=False):
            fb_rows = []
            for k, v in sorted(feedback_store.items(), key=lambda x: int(x[0].split(":")[0])):
                parts = k.split(":", 1)
                fb_rows.append({
                    "#": parts[0],
                    "Question": (parts[1] if len(parts) > 1 else k)[:70],
                    "Label": v.get("label", ""),
                    "Notes": v.get("notes", ""),
                })
            st.dataframe(pd.DataFrame(fb_rows), use_container_width=True, hide_index=True)
