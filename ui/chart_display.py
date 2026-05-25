"""Streamlit entry: Plotly-first evidence charts with Altair fallback."""

from __future__ import annotations

import streamlit as st

from core.viz.charts import chart_for


_PLOTLY_CONFIG = {
    "displayModeBar": True,
    "displaylogo": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    "scrollZoom": True,
    "responsive": True,
}


def display_evidence_chart(
    df,
    inv_name: str = "",
    *,
    chart_title: str | None = None,
    key: str | None = None,
    qo_semantics=None,
) -> bool:
    """
    Render a metric / breakdown / time-series frame as an interactive chart.
    Plotly when a figure is available; otherwise Altair (specialized visuals).

    ``key`` must be unique among charts on the same Streamlit run (e.g. session_id + slug);
    if omitted, a best-effort default is used (may collide if many identical frames).

    Returns True if a chart was drawn (Plotly or Altair).
    """
    if df is None or getattr(df, "empty", True):
        return False

    widget_key = key or f"evc_{inv_name or 'df'}_{hash(tuple(str(c) for c in df.columns))}_{len(df)}"

    fig = None
    try:
        from core.viz.charts_plotly import evidence_chart_plotly

        fig = evidence_chart_plotly(
            inv_name, df,
            chart_title=chart_title or (inv_name.strip() or None),
            qo_semantics=qo_semantics,
        )
    except ImportError:
        pass
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CONFIG, key=widget_key)
        return True

    chart = chart_for(inv_name, df)
    if chart is not None:
        st.altair_chart(chart, use_container_width=True, key=f"{widget_key}_alt")
        return True
    return False
