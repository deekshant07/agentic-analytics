"""ui/render.py — Streamlit investigation and report rendering components."""
import streamlit as st
import pandas as pd

from core.analysis.analyst import AnalystReport
from core.agents.story_architect import DeepStoryArc
from ui.legacy_charts import _should_show_chart
from ui.chart_display import display_evidence_chart
from ui.deep_period_table import build_deep_period_comparison_table
from core.semantic.presentation import (
    format_rate_columns_for_display,
    is_rate_column_name,
    presentation_from_qo,
)

def _render_lifecycle_stages(df: pd.DataFrame):
    """
    Render lifecycle stage distribution as a colored donut + metric cards row.
    Falls back to the generic chart if the donut fails.
    """
    from core.viz.charts_plotly import _plotly_lifecycle_stages
    import plotly.express as px

    fig = _plotly_lifecycle_stages(df)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

    # Metric cards row for key stages
    stage_map = {
        "1.": ("New",        "#22c55e"),
        "3.": ("Engaged",    "#2563eb"),
        "4.": ("Power User", "#7c3aed"),
        "5.": ("At Risk",    "#f97316"),
        "6.": ("Churned",    "#ef4444"),
    }
    total = int(df["users"].sum()) if "users" in df.columns else 0
    cards = []
    for _, row in df.iterrows():
        stage_str = str(row.get("lifecycle_stage", ""))
        prefix = stage_str[:2]
        if prefix in stage_map:
            name, color = stage_map[prefix]
            users = int(row["users"])
            pct   = round(users * 100 / total, 1) if total else 0
            cards.append((name, users, pct, color))

    if cards:
        cols = st.columns(len(cards))
        for col, (name, users, pct, color) in zip(cols, cards):
            with col:
                st.markdown(
                    f'<div style="background:#1a1d27;border-left:3px solid {color};'
                    f'border-radius:6px;padding:0.7rem 0.9rem;text-align:center">'
                    f'<div style="font-size:1.3rem;font-weight:700;color:{color}">{users:,}</div>'
                    f'<div style="font-size:0.7rem;color:#94a3b8">{name} ({pct}%)</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


def _render_survival_curves(df: pd.DataFrame):
    """Render D1/D7/D30 survival curves as an interactive Plotly multi-line chart."""
    from core.viz.charts_plotly import _plotly_survival_curves
    fig = _plotly_survival_curves(df)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
        # Summary stats below
        for col, label in [
            ("d1_retention_pct",  "Avg D1"),
            ("d7_retention_pct",  "Avg D7"),
            ("d30_retention_pct", "Avg D30"),
        ]:
            if col in df.columns:
                avg = float(pd.to_numeric(df[col], errors="coerce").mean())
                st.caption(f"**{label}**: {avg:.1f}%")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)


def _render_xyz_matrix(df: pd.DataFrame):
    """
    Render XYZ cohort matrix as a Plotly heatmap + expandable pivot table.
    """
    from core.viz.charts_plotly import _plotly_xyz_heatmap
    fig = _plotly_xyz_heatmap(df)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        return

    # Pivot table view
    with st.expander("View pivot table", expanded=False):
        cols = list(df.columns)
        time_col = next((c for c in cols if "cohort" in c.lower() or "month" in c.lower()), None)
        dim_col  = next((c for c in cols if c not in (time_col,) and
                         not pd.api.types.is_numeric_dtype(df[c])), None)
        if time_col and dim_col and "users" in df.columns:
            pivot = df.pivot_table(
                index=time_col, columns=dim_col, values="users",
                aggfunc="sum", fill_value=0
            )
            st.dataframe(pivot.sort_index(ascending=False), use_container_width=True)
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)


def _render_sankey_journey(df: pd.DataFrame):
    """Render user journey as a Sankey flow diagram."""
    from core.viz.charts_plotly import _plotly_sankey_journey
    fig = _plotly_sankey_journey(df)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)


def _render_power_user_curve(df: pd.DataFrame):
    """Render power user frequency distribution."""
    from core.viz.charts_plotly import _plotly_power_user_curve
    fig = _plotly_power_user_curve(df)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
        # Highlight the top tier
        if not df.empty and "frequency_bucket" in df.columns and "users" in df.columns:
            total = int(pd.to_numeric(df["users"], errors="coerce").sum())
            power = df[df["frequency_bucket"].str.contains("31+", na=False)]
            if not power.empty:
                n = int(pd.to_numeric(power["users"].iloc[0], errors="coerce"))
                pct = round(n * 100 / total, 1) if total else 0
                st.caption(f"Power users (31+ events): **{n:,}** ({pct}% of all users)")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)


def _render_funnel_property_drilldown(df: pd.DataFrame):
    """Render funnel property drilldown as overlaid bars + CVR table."""
    from core.viz.charts_plotly import _plotly_funnel_drilldown
    fig = _plotly_funnel_drilldown(df)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
    # Always show the table for precise CVR numbers
    display_cols = [c for c in ("entered", "converted", "dropped", "cvr_pct") if c in df.columns]
    cat_cols = [c for c in df.columns if c not in display_cols]
    if cat_cols and display_cols:
        show_df = df[cat_cols + display_cols].copy()
        st.dataframe(show_df, use_container_width=True, hide_index=True)


def _is_lifecycle_df(df: pd.DataFrame) -> bool:
    return df is not None and not df.empty and "lifecycle_stage" in df.columns and "users" in df.columns


def _is_survival_curve_df(df: pd.DataFrame) -> bool:
    return (df is not None and not df.empty and
            any(c in df.columns for c in ("d1_retention_pct", "d7_retention_pct", "d30_retention_pct")))


def _is_xyz_matrix_df(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False
    cols = list(df.columns)
    has_cohort = any("cohort" in c.lower() or "month" in c.lower() for c in cols)
    has_dim    = len([c for c in cols if not pd.api.types.is_numeric_dtype(df[c])]) >= 2
    return has_cohort and has_dim and "users" in cols


def _is_sankey_df(df: pd.DataFrame) -> bool:
    return (df is not None and not df.empty and
            all(c in df.columns for c in ("source", "target", "users")))


def _is_power_user_df(df: pd.DataFrame) -> bool:
    return df is not None and not df.empty and "frequency_bucket" in df.columns


def _is_funnel_drilldown_df(df: pd.DataFrame) -> bool:
    return (df is not None and not df.empty and
            "entered" in df.columns and "converted" in df.columns and "cvr_pct" in df.columns)


def _render_scalar_metric(df: pd.DataFrame):
    """Render a single-row result as big metric card(s) instead of a table."""
    row   = df.iloc[0]
    cols  = st.columns(min(len(df.columns), 4))
    for i, col_name in enumerate(df.columns):
        val = row[col_name]
        with cols[i % 4]:
            if isinstance(val, float) and is_rate_column_name(col_name) and 0 <= val <= 100:
                fmt = f"{val:.1f}%"
            elif isinstance(val, float):
                fmt = f"{val:,.1f}"
            elif isinstance(val, int):
                fmt = f"{val:,}"
            else:
                fmt = str(val)
            st.markdown(f"""
<div class="metric-card">
  <div class="metric-value">{fmt}</div>
  <div class="metric-label">{col_name.replace("_"," ").title()}</div>
</div>""", unsafe_allow_html=True)


def _is_scalar_like(df: pd.DataFrame) -> bool:
    """
    True when the frame is effectively a single KPI point.
    Includes one-row + one numeric + optional date/category context columns.
    """
    if df is None or df.empty or len(df) != 1:
        return False
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    return len(num_cols) == 1


def _render_retention_matrix(df: pd.DataFrame):
    """
    Render cohort-retention matrix (Amplitude-style table).
    Expects columns: cohort_*, period_idx, retention_pct (+ optional retained/cohort counts).
    """
    cols = list(df.columns)
    cohort_cols = [c for c in cols if c.startswith("cohort_")]
    if not cohort_cols or "period_idx" not in cols or "retention_pct" not in cols:
        st.dataframe(df, use_container_width=True, hide_index=True)
        return
    cohort_col = cohort_cols[0]
    dfx = df.copy()
    try:
        dfx[cohort_col] = pd.to_datetime(dfx[cohort_col]).dt.strftime("%Y-%m-%d")
    except Exception:
        dfx[cohort_col] = dfx[cohort_col].astype(str)
    dfx["period"] = "P" + dfx["period_idx"].astype(int).astype(str)
    pct_matrix = dfx.pivot_table(
        index=cohort_col,
        columns="period",
        values="retention_pct",
        aggfunc="mean",
    ).sort_index(ascending=False)
    # Ensure period columns are ordered P0, P1, ...
    ordered_cols = sorted(pct_matrix.columns, key=lambda c: int(str(c).replace("P", "")))
    pct_matrix = pct_matrix.reindex(columns=ordered_cols)
    st.markdown(
        '<div style="font-size:0.74rem;color:#64748b;margin-bottom:0.35rem;">'
        "Retention matrix (cohort x period)</div>",
        unsafe_allow_html=True,
    )
    retained_matrix = None
    cohort_matrix = None
    if "retained_users" in dfx.columns:
        retained_matrix = dfx.pivot_table(
            index=cohort_col, columns="period", values="retained_users", aggfunc="sum"
        ).reindex(columns=ordered_cols)
    elif "retained" in dfx.columns:
        retained_matrix = dfx.pivot_table(
            index=cohort_col, columns="period", values="retained", aggfunc="sum"
        ).reindex(columns=ordered_cols)
    if "cohort_size" in dfx.columns:
        cohort_matrix = dfx.pivot_table(
            index=cohort_col, columns="period", values="cohort_size", aggfunc="max"
        ).reindex(columns=ordered_cols)

    # Build a string/object display frame (do not mutate float matrix in-place).
    display = pct_matrix.copy().astype(object)
    for r in display.index:
        for c in display.columns:
            pct = display.loc[r, c]
            if pd.isna(pct):
                display.loc[r, c] = ""
                continue
            if retained_matrix is not None and cohort_matrix is not None:
                rv = retained_matrix.loc[r, c] if c in retained_matrix.columns else None
                cv = cohort_matrix.loc[r, c] if c in cohort_matrix.columns else None
                if pd.notna(rv) and pd.notna(cv):
                    display.loc[r, c] = f"{int(rv):,}/{int(cv):,} ({float(pct):.1f}%)"
                else:
                    display.loc[r, c] = f"{float(pct):.1f}%"
            else:
                display.loc[r, c] = f"{float(pct):.1f}%"
    st.dataframe(display, use_container_width=True, hide_index=False)
    with st.expander("View retention matrix numeric data", expanded=False):
        st.dataframe(pct_matrix.round(1), use_container_width=True, hide_index=False)


def _is_retention_matrix_df(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False
    cols = set(df.columns)
    has_cohort = any(c.startswith("cohort_") for c in cols)
    return has_cohort and "period_idx" in cols and "retention_pct" in cols


def _render_investigation(
    inv,
    compact: bool = False,
    show_title: bool = False,
    *,
    chart_key: str | None = None,
):
    """Render one investigation's DataFrame as a chart or scalar cards."""
    if inv.df is None or inv.df.empty:
        st.caption("No data.")
        return
    df = inv.df.copy()
    inv_name = getattr(inv, "name", "")
    inv_qo = getattr(inv, "qo", None)
    pres = presentation_from_qo(inv_qo) if inv_qo is not None else None

    # ── Specialized renderers (highest priority) ──────────────────────────────
    if inv_name == "retention_matrix" or _is_retention_matrix_df(df):
        _render_retention_matrix(df)
        return

    if _is_lifecycle_df(df):
        _render_lifecycle_stages(df)
        return

    if inv_name == "retention_survival_curve" or _is_survival_curve_df(df):
        _render_survival_curves(df)
        return

    if inv_name == "xyz_cohort_matrix" or _is_xyz_matrix_df(df):
        _render_xyz_matrix(df)
        return

    if inv_name == "journey_two_hop" or _is_sankey_df(df):
        _render_sankey_journey(df)
        return

    if inv_name == "power_user_curve" or _is_power_user_df(df):
        _render_power_user_curve(df)
        return

    if inv_name == "funnel_property_drilldown" or _is_funnel_drilldown_df(df):
        _render_funnel_property_drilldown(df)
        return

    # ── Generic renderers ─────────────────────────────────────────────────────
    is_scalar = _is_scalar_like(df)
    if is_scalar:
        _render_scalar_metric(df)
    else:
        show_chart, _ = _should_show_chart(df, inv_name=inv_name, compact=compact)
        if show_chart:
            if show_title and getattr(inv, "purpose", ""):
                st.markdown(
                    f'<div class="chart-title">{inv.purpose}</div>',
                    unsafe_allow_html=True,
                )
            ck = chart_key or f"inv_{inv_name or 'chart'}_{id(inv)}"
            _qo_sem = getattr(inv_qo, "_query_semantics", None) if inv_qo is not None else None
            if not display_evidence_chart(df, inv_name, key=ck, qo_semantics=_qo_sem):
                st.dataframe(
                    format_rate_columns_for_display(df, pres),
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.dataframe(
                format_rate_columns_for_display(df, pres),
                use_container_width=True,
                hide_index=True,
            )
        with st.expander("View data", expanded=False):
            st.dataframe(
                format_rate_columns_for_display(df, pres),
                use_container_width=True,
                hide_index=True,
            )


def _render_dim_grid(invs: list, label: str, session_id: str = ""):
    """Render a labeled 2-column grid of demographic investigations."""
    if not invs:
        return
    st.markdown(
        f'<div style="font-size:0.68rem;font-weight:700;color:#6b7280;'
        f'text-transform:uppercase;letter-spacing:0.08em;'
        f'margin:1.2rem 0 0.6rem;border-bottom:1px solid #f1f5f9;padding-bottom:0.4rem;'
        f'font-family:Inter,sans-serif">'
        f'{label}</div>',
        unsafe_allow_html=True,
    )
    pairs = [invs[i:i+2] for i in range(0, len(invs), 2)]
    _gi = 0
    for pair in pairs:
        cols = st.columns(len(pair))
        for col, inv in zip(cols, pair):
            with col:
                st.markdown(
                    f'<div class="chart-title" style="font-size:0.72rem">'
                    f'{inv.purpose}</div>',
                    unsafe_allow_html=True,
                )
                slug = f"{session_id}_demo_{label[:16].replace(' ', '_')}_{_gi}_{getattr(inv, 'name', 'inv')}"
                _render_investigation(inv, compact=True, chart_key=slug)
                _gi += 1
                if inv.insight:
                    st.caption(inv.insight)


def _render_deep_report(
    arc: "DeepStoryArc",
    sub_results: list,
    assistant_msg: dict,
    turn: dict,
    user_question: str = "",
):
    """
    Renders a Deep Analysis report:
      Headline → Executive Summary → N investigation sections → Recommendations → Assumptions
    """
    # ── Headline ──────────────────────────────────────────────────────────────
    if arc.headline:
        st.markdown(
            f'<div style="font-size:1.25rem;font-weight:700;color:#e2e8f0;'
            f'line-height:1.4;margin-bottom:0.8rem">{arc.headline}</div>',
            unsafe_allow_html=True,
        )

    # ── Hypothesis verdict badge ───────────────────────────────────────────────
    _VERDICT_COLORS = {
        "product_change":   ("#7c6af7", "#1e1b3a"),
        "technical_issue":  ("#fb7185", "#2d1a1e"),
        "external_factor":  ("#f97316", "#2a1a0a"),
        "mix_shift":        ("#34d399", "#0a2318"),
        "inconclusive":     ("#64748b", "#1a1d27"),
    }
    verdict = arc.hypothesis_verdict or "inconclusive"
    fg, bg  = _VERDICT_COLORS.get(verdict, ("#64748b", "#1a1d27"))
    badge_label = verdict.replace("_", " ").upper()
    st.markdown(
        f'<span style="font-size:0.65rem;font-weight:700;color:{fg};background:{bg};'
        f'border:1px solid {fg}33;border-radius:999px;padding:0.1rem 0.55rem;'
        f'margin-bottom:0.6rem;display:inline-block">⚡ {badge_label}</span>',
        unsafe_allow_html=True,
    )

    # ── Executive summary ─────────────────────────────────────────────────────
    if arc.executive_summary:
        st.markdown(
            f'<div style="background:#1a1d27;border-left:3px solid #7c6af7;'
            f'border-radius:0 8px 8px 0;padding:0.9rem 1.2rem;margin-bottom:1.2rem;'
            f'font-size:0.88rem;color:#cbd5e1;line-height:1.65">'
            f'{arc.executive_summary}</div>',
            unsafe_allow_html=True,
        )

    # ── Named-month comparison (Feb / Mar / Apr, …) from sub-query frames ─────
    if user_question and sub_results:
        try:
            _cmp = build_deep_period_comparison_table(user_question, sub_results)
        except Exception:
            _cmp = None
        if _cmp is not None and not _cmp.empty:
            st.markdown(
                '<div style="font-size:0.72rem;font-weight:600;color:#64748b;'
                'text-transform:uppercase;letter-spacing:0.07em;margin:0.2rem 0 0.5rem">'
                "Period comparison (from investigation results)</div>",
                unsafe_allow_html=True,
            )
            st.dataframe(_cmp, use_container_width=True, hide_index=True)

    # ── Investigation sections ────────────────────────────────────────────────
    ok_results = [r for r in sub_results if r.ok]
    if ok_results:
        st.markdown(
            '<div style="font-size:0.72rem;font-weight:600;color:#64748b;'
            'text-transform:uppercase;letter-spacing:0.07em;margin-bottom:0.6rem">'
            f'Investigation findings · {len(ok_results)} queries</div>',
            unsafe_allow_html=True,
        )

    for i, section in enumerate(arc.sections):
        # Match section to its sub_result for chart data
        src = ok_results[i] if i < len(ok_results) else None

        with st.expander(f"**{section.title}**", expanded=(i == 0)):
            # Key number pill
            if section.key_number:
                st.markdown(
                    f'<div style="font-size:1.5rem;font-weight:700;color:#7c6af7;'
                    f'margin-bottom:0.4rem">{section.key_number}</div>',
                    unsafe_allow_html=True,
                )
            # Finding text
            if section.finding:
                st.markdown(
                    f'<div style="font-size:0.86rem;color:#94a3b8;margin-bottom:0.6rem">'
                    f'{section.finding}</div>',
                    unsafe_allow_html=True,
                )
            # Chart — render the raw DataFrame if available
            if src and not src.df.empty:
                cols = src.df.columns.tolist()
                # Try simple line chart (time series) or bar chart (categorical)
                time_cols = [c for c in cols if any(k in c.lower() for k in ("date", "day", "week", "month", "period", "time"))]
                num_cols  = [c for c in cols if src.df[c].dtype in ("int64", "float64") and c not in time_cols]
                if time_cols and num_cols:
                    try:
                        chart_df = src.df[[time_cols[0], num_cols[0]]].rename(
                            columns={time_cols[0]: "date", num_cols[0]: "value"}
                        )
                        st.line_chart(chart_df.set_index("date"), height=160)
                    except Exception:
                        st.dataframe(src.df.head(8), use_container_width=True, hide_index=True)
                else:
                    st.dataframe(src.df.head(8), use_container_width=True, hide_index=True)

            # Rationale tag
            if section.rationale:
                st.caption(f"Why investigated: {section.rationale}")

    # ── Recommendations ───────────────────────────────────────────────────────
    if arc.recommendations:
        st.markdown(
            '<div style="font-size:0.72rem;font-weight:600;color:#64748b;'
            'text-transform:uppercase;letter-spacing:0.07em;margin:1.2rem 0 0.5rem">'
            'Recommended actions</div>',
            unsafe_allow_html=True,
        )
        for rec in arc.recommendations:
            st.markdown(
                f'<div style="display:flex;gap:0.6rem;align-items:flex-start;'
                f'margin-bottom:0.5rem">'
                f'<span style="color:#7c6af7;font-size:0.9rem;margin-top:0.05rem">→</span>'
                f'<span style="font-size:0.85rem;color:#cbd5e1">{rec}</span></div>',
                unsafe_allow_html=True,
            )

    # ── Assumptions ───────────────────────────────────────────────────────────
    if arc.assumptions:
        with st.expander("Assumptions & caveats", expanded=False):
            for a in arc.assumptions:
                st.caption(f"• {a}")

    # Persist answer for chat history
    summary = arc.executive_summary or arc.headline or "Deep analysis complete."
    assistant_msg["content"] = summary
    turn["answer"]           = summary
    turn["memory_summary"]   = summary[:220]
    if arc.hypothesis_verdict:
        turn["hypothesis_verdict"] = arc.hypothesis_verdict


def _render_analyst_report(
    report: AnalystReport,
    assistant_msg: dict,
    turn: dict,
    session_id: str,
    *,
    qo=None,
):
    """
    Renders a full analyst report in document style:
      1. Synthesis narrative
      2a. For demographic_breakdown: two labeled grids (User Profile / Event Properties)
      2b. For all other types: primary chart full-width + evidence grid
      3. "Investigate further" chips
      4. SQL queries (collapsible)
    """
    if getattr(report, "data_quality_blocked", False):
        st.warning(report.narrative or "Data quality issue detected — results may not be reliable.")
        assistant_msg["content"] = report.narrative or report.executive_summary
        return

    valid = [inv for inv in report.investigations if not inv.error and not inv.df.empty]
    if not valid:
        st.warning("Investigations returned no data.")
        return

    primary     = valid[0]
    secondaries = valid[1:]

    # ── 3-act narrative (What happened / Why / What to do next) ──────────────
    _BEAT_META = {
        "context":    ("What happened",     "#3b82f6", "📊"),
        "tension":    ("Why it happened",   "#f97316", "🔍"),
        "resolution": ("What to do next",   "#34d399", "✅"),
    }
    beats = getattr(report, "beats", [])
    if beats:
        for beat in beats:
            phase = getattr(beat, "phase", "") or ""
            label, color, icon = _BEAT_META.get(phase, (phase.title(), "#64748b", "•"))
            headline   = getattr(beat, "headline",   "") or ""
            evidence   = getattr(beat, "evidence",   "") or ""
            implication = getattr(beat, "implication", "") or ""
            st.markdown(
                f'<div style="border-left:3px solid {color};padding:0.7rem 1rem 0.7rem 1rem;'
                f'margin-bottom:0.75rem;background:#1a1d27;border-radius:0 8px 8px 0">'
                f'<div style="font-size:0.62rem;font-weight:700;color:{color};'
                f'text-transform:uppercase;letter-spacing:0.08em;margin-bottom:0.25rem">'
                f'{icon} {label}</div>'
                f'<div style="font-size:0.92rem;font-weight:600;color:#e2e8f0;'
                f'margin-bottom:0.25rem">{headline}</div>'
                + (f'<div style="font-size:0.82rem;color:#94a3b8;margin-bottom:0.2rem">{evidence}</div>' if evidence else "")
                + (f'<div style="font-size:0.8rem;color:#64748b;font-style:italic">{implication}</div>' if implication else "")
                + '</div>',
                unsafe_allow_html=True,
            )
        # Next steps as action pills after resolution beat
        next_steps = getattr(report, "next_steps", [])
        if next_steps:
            st.markdown(
                '<div style="font-size:0.62rem;font-weight:700;color:#7c6af7;'
                'text-transform:uppercase;letter-spacing:0.08em;margin:0.8rem 0 0.4rem">Actions</div>',
                unsafe_allow_html=True,
            )
            for step in next_steps[:3]:
                st.markdown(
                    f'<div style="display:flex;gap:0.5rem;align-items:flex-start;margin-bottom:0.35rem">'
                    f'<span style="color:#7c6af7;font-size:0.85rem">→</span>'
                    f'<span style="font-size:0.83rem;color:#cbd5e1">{step}</span></div>',
                    unsafe_allow_html=True,
                )
    else:
        # Fallback: prose narrative when beats aren't available
        st.markdown(report.narrative)
    assistant_msg["content"] = report.narrative or report.executive_summary

    # ── Demographic breakdown — two labeled sections ──────────────────────────
    if report.analysis_type == "demographic_breakdown":
        user_invs  = [i for i in valid if i.name.startswith("user_")]
        event_invs = [i for i in valid if i.name.startswith("event_")]
        other_invs = [i for i in valid if not i.name.startswith(("user_", "event_"))]

        _render_dim_grid(user_invs,  "User Profile", session_id)
        _render_dim_grid(event_invs, "Event Properties", session_id)
        if other_invs:
            _render_dim_grid(other_invs, "Other", session_id)

    else:
        # ── Primary chart (full width) ────────────────────────────────────────
        st.markdown('<div class="evidence-header">Supporting Evidence</div>',
                    unsafe_allow_html=True)
        if qo is not None:
            setattr(primary, "qo", qo)
        _render_investigation(
            primary,
            compact=False,
            show_title=True,
            chart_key=f"{session_id}_analyst_primary_{getattr(primary, 'name', 'p')}",
        )
        if primary.insight:
            st.markdown(
                f'<div class="insight-callout">'
                f'<div class="insight-callout-title">Key Finding</div>'
                f'{primary.insight}</div>',
                unsafe_allow_html=True,
            )

        # ── Evidence grid (secondary investigations) ──────────────────────────
        if secondaries:
            with st.expander("Additional evidence", expanded=False):
                pairs = [secondaries[i:i+2] for i in range(0, len(secondaries), 2)]
                for si, pair in enumerate(pairs):
                    cols = st.columns(len(pair))
                    for pi, inv in enumerate(pair):
                        with cols[pi]:
                            st.markdown(
                                f'<div class="chart-title" style="font-size:0.72rem">'
                                f'{inv.purpose}</div>',
                                unsafe_allow_html=True,
                            )
                            _render_investigation(
                                inv,
                                compact=True,
                                chart_key=f"{session_id}_analyst_sec_{si}_{pi}_{getattr(inv, 'name', 's')}",
                            )
                            if inv.insight:
                                st.caption(inv.insight)

    # ── SQL queries (collapsible) ─────────────────────────────────────────────
    with st.expander("Queries run", expanded=False):
        for inv in valid:
            st.markdown(f"**{inv.name}** — {inv.purpose}")
            st.code(inv.sql, language="sql")

    # Save data for history replay
    if report.analysis_type == "demographic_breakdown":
        demo_cards = [
            {
                "name":    inv.name,
                "purpose": inv.purpose,
                "insight": inv.insight or "",
                "columns": list(inv.df.columns),
                "data":    inv.df.head(200).values.tolist(),
            }
            for inv in valid
        ]
        assistant_msg["demo_cards"]    = demo_cards
        assistant_msg["analysis_type"] = "demographic_breakdown"
        turn["demo_cards"]             = demo_cards
        turn["analysis_type"]          = "demographic_breakdown"
    else:
        if not primary.df.empty:
            assistant_msg["table"] = {
                "columns": list(primary.df.columns),
                "data":    primary.df.head(500).values.tolist(),
            }
            turn["table"] = assistant_msg["table"]

    turn["sql"] = primary.sql
    assistant_msg["sql"] = primary.sql

    # ── Investigate further chips ─────────────────────────────────────────────
    if report.next_steps:
        st.markdown(
            '<div style="font-size:0.75rem;color:#64748b;margin-top:1rem;'
            'margin-bottom:0.4rem">Investigate further:</div>',
            unsafe_allow_html=True,
        )
        chip_cols = st.columns(min(len(report.next_steps), 3))
        for i, step in enumerate(report.next_steps):
            with chip_cols[i]:
                if st.button(step, key=f"ns_{session_id}_{i}_{step[:20]}",
                             use_container_width=True):
                    st.session_state["pending"] = step
                    st.rerun()

