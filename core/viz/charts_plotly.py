"""
Plotly evidence charts — interactive defaults closer to product analytics UIs
(Mitzu / Kubit style: unified hover, zoom, readable lines).

Used as the primary renderer for heuristic query results; Altair remains the
fallback for specialized charts (retention heatmap, waterfall, etc.).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import plotly.graph_objects as go

from core.viz.charts import _is_temporal_col, _is_rate, _median_spacing_days

_PALETTE = [
    "#2563eb",
    "#ea580c",
    "#059669",
    "#7c3aed",
    "#dc2626",
    "#4f46e5",
    "#0891b2",
    "#ca8a04",
    "#16a34a",
    "#db2777",
    "#0d9488",
    "#e11d48",
]


def _num_fmt(col: str) -> str:
    return ".1f" if _is_rate(col) else ",.0f"


def _preferred_chart_metric_col(num_cols: list[str]) -> str | None:
    """Pick the primary rate metric for charts (not cohort_size / user counts)."""
    if not num_cols:
        return None
    rate_cols = [c for c in num_cols if _is_rate(c)]
    if rate_cols:
        for pref in (
            "retention_pct",
            "activation_rate",
            "pct",
            "rate_pct",
            "status_rate_pct",
        ):
            if pref in rate_cols:
                return pref
        return rate_cols[0]
    return num_cols[0]


def _chart_metric_columns(num_cols: list[str]) -> list[str]:
    """Columns to plot on a time axis — prefer a single rate over raw counts."""
    pref = _preferred_chart_metric_col(num_cols)
    if pref and _is_rate(pref):
        return [pref]
    return num_cols


def _apply_base_layout(
    fig: go.Figure,
    *,
    title: str | None = None,
    hovermode: str = "x unified",
) -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Inter, system-ui, sans-serif", size=13, color="#334155"),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8fafc",
        margin=dict(l=72, r=28, t=52 if title else 36, b=64),
        hovermode=hovermode,
        hoverlabel=dict(bgcolor="white", font_size=13, bordercolor="#e2e8f0"),
        title=(
            dict(text=title, x=0.0, xanchor="left", font=dict(size=15, color="#0f172a"))
            if title
            else None
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            bgcolor="rgba(255,255,255,0.92)",
            bordercolor="#e2e8f0",
            borderwidth=1,
        ),
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor="#e2e8f0",
        zeroline=False,
        showline=True,
        linecolor="#cbd5e1",
        tickfont=dict(size=12),
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor="#e2e8f0",
        zeroline=False,
        showline=True,
        linecolor="#cbd5e1",
        tickfont=dict(size=12),
    )
    return fig


def _plotly_funnel(df: pd.DataFrame) -> Optional[go.Figure]:
    if "step_name" not in df.columns or "users" not in df.columns:
        return None
    d = df.sort_values("step_num", na_position="last") if "step_num" in df.columns else df.copy()
    steps = d["step_name"].astype(str).str.replace("_", " ").tolist()
    users = pd.to_numeric(d["users"], errors="coerce").fillna(0).tolist()
    fig = go.Figure(
        go.Bar(
            y=steps,
            x=users,
            orientation="h",
            marker=dict(color=_PALETTE[0], line=dict(width=0)),
            text=[f"{int(u):,}" for u in users],
            textposition="inside",
            insidetextanchor="end",
            textfont=dict(color="white", size=13),
            hovertemplate="%{y}<br>users: %{x:,.0f}<extra></extra>",
        )
    )
    fig.update_yaxes(autorange="reversed")
    return _apply_base_layout(fig, title="Funnel", hovermode="closest")


def _plotly_hbar_categories(df: pd.DataFrame, cat: str, y: str, *, title: str | None = None) -> Optional[go.Figure]:
    d = df[[cat, y]].dropna().copy()
    d[y] = pd.to_numeric(d[y], errors="coerce")
    d = d.dropna(subset=[y]).sort_values(y, ascending=True)
    if d.empty:
        return None
    cats = d[cat].astype(str).str.replace("_", " ")
    vals = d[y].tolist()
    if _is_rate(y):
        text_lbl = [f"{float(v):.1f}%" for v in vals]
        hfmt = ".1f"
        hover = f"%{{y}}<br>{y}: %{{x:{hfmt}}}%<extra></extra>"
    else:
        text_lbl = [f"{int(round(float(v))):,}" for v in vals]
        hfmt = ",.0f"
        hover = f"%{{y}}<br>{y}: %{{x:{hfmt}}}<extra></extra>"
    fig = go.Figure(
        go.Bar(
            y=cats.tolist(),
            x=vals,
            orientation="h",
            marker=dict(color=_PALETTE[0], line=dict(width=0)),
            text=text_lbl,
            textposition="inside",
            insidetextanchor="end",
            textfont=dict(color="white", size=12),
            hovertemplate=hover,
        )
    )
    fig.update_yaxes(autorange="reversed", title="")
    fig.update_xaxes(title=y.replace("_", " ").title(), rangemode="tozero")
    if _is_rate(y):
        fig.update_xaxes(ticksuffix="%")
    return _apply_base_layout(fig, title=title, hovermode="closest")


def _coarse_period_axis_labels(d: pd.DataFrame, x: str) -> tuple[list[str], str]:
    """Human-readable categorical x labels for weekly / monthly-ish series."""
    med = _median_spacing_days(d[x])
    name_l = str(x).lower()
    multi_year = int(d[x].dt.year.nunique()) > 1

    if "week" in name_l or (med is not None and 6 <= med < 27):
        starts = d[x].dt.to_period("W-MON").dt.start_time
        x_labels = (
            starts.dt.strftime("%b %d, %Y").tolist()
            if multi_year
            else starts.dt.strftime("%b %d").tolist()
        )
        x_axis_title = "Week starting (Mon)"
    elif med is not None and med >= 27:
        starts = d[x].dt.to_period("M").dt.start_time
        x_labels = (
            starts.dt.strftime("%b %Y").tolist()
            if multi_year
            else starts.dt.strftime("%b '%y").tolist()
        )
        x_axis_title = ""
    else:
        x_labels = (
            d[x].dt.strftime("%b %d, %Y").tolist()
            if multi_year or len(d) > 18
            else d[x].dt.strftime("%b %d").tolist()
        )
        x_axis_title = ""
    return x_labels, x_axis_title


def _plotly_coarse_period_bars(
    d: pd.DataFrame, x: str, y: str, *, chart_title: str | None = None
) -> Optional[go.Figure]:
    """Weekly / monthly / sparse periods as clear vertical bars with human date labels."""
    d = d[[x, y]].dropna().copy()
    d[x] = pd.to_datetime(d[x], errors="coerce")
    d = d.dropna(subset=[x]).sort_values(x)
    if d.empty:
        return None
    x_labels, x_axis_title = _coarse_period_axis_labels(d, x)

    y_disp = y.replace("_", " ").title()
    ys = pd.to_numeric(d[y], errors="coerce").astype(float)
    y_list = ys.tolist()
    if _is_rate(y):
        text = [f"{float(v):.1f}%" for v in y_list]
        hov = "%{x}<br>" + y_disp + ": %{y:.1f}%<extra></extra>"
    else:
        text = [f"{int(round(float(v))):,}" for v in y_list]
        hov = "%{x}<br>" + y_disp + ": %{y:,.0f}<extra></extra>"
    # "x unified" hover reliably breaks single-trace categorical bars in Plotly + Streamlit
    # (bars disappear; value labels may still show). Use closest + in-bar labels.
    fig = go.Figure(
        go.Bar(
            x=list(x_labels),
            y=y_list,
            text=text,
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(color="white", size=13),
            marker=dict(
                color=_PALETTE[0],
                opacity=1.0,
                line=dict(width=0),
            ),
            hovertemplate=hov,
        )
    )
    fig.update_layout(
        bargap=0.14,
        xaxis_title=x_axis_title or None,
        yaxis_title=y_disp,
        hovermode="closest",
    )
    fig.update_yaxes(rangemode="tozero")
    if _is_rate(y):
        fig.update_yaxes(ticksuffix="%")
    return _apply_base_layout(fig, title=chart_title, hovermode="closest")


def _plotly_time_multi(
    df: pd.DataFrame,
    x: str,
    y_cols: list[str],
    *,
    chart_title: str | None = None,
) -> Optional[go.Figure]:
    """Multiple numeric series over one time axis (e.g. active vs transacted MOM)."""
    cols = [x] + [c for c in y_cols if c in df.columns]
    d = df[cols].dropna(how="all", subset=[x]).copy()
    d[x] = pd.to_datetime(d[x], errors="coerce")
    d = d.dropna(subset=[x]).sort_values(x)
    if d.empty or len(y_cols) < 2:
        return None

    med = _median_spacing_days(d[x])
    n = len(d)
    use_bars = n <= 14 or (med is not None and med >= 6)
    x_labels, x_axis_title = _coarse_period_axis_labels(d, x)

    fig = go.Figure()
    if use_bars:
        for i, y in enumerate(y_cols):
            if y not in d.columns:
                continue
            ys = pd.to_numeric(d[y], errors="coerce").astype(float)
            y_list = ys.tolist()
            if _is_rate(y):
                text = [f"{float(v):.1f}%" for v in y_list]
                hov = "%{x}<br>" + y.replace("_", " ").title() + ": %{y:.1f}%<extra></extra>"
            else:
                text = [f"{int(round(float(v))):,}" for v in y_list]
                hov = "%{x}<br>" + y.replace("_", " ").title() + ": %{y:,.0f}<extra></extra>"
            fig.add_trace(
                go.Bar(
                    x=x_labels,
                    y=y_list,
                    name=y.replace("_", " ").title(),
                    text=text,
                    textposition="inside",
                    insidetextanchor="middle",
                    textfont=dict(color="white", size=12),
                    marker=dict(
                        color=_PALETTE[i % len(_PALETTE)],
                        line=dict(width=0),
                    ),
                    offsetgroup=i,
                    hovertemplate=hov,
                )
            )
        fig.update_layout(
            barmode="group",
            bargap=0.22,
            bargroupgap=0.06,
            xaxis_title=x_axis_title or None,
            yaxis_title="Users",
            hovermode="closest",
        )
        fig.update_yaxes(rangemode="tozero")
        if any(_is_rate(y) for y in y_cols):
            fig.update_yaxes(ticksuffix="%")
        return _apply_base_layout(fig, title=chart_title, hovermode="closest")

    yfmt = ",.0f"
    for i, y in enumerate(y_cols):
        if y not in d.columns:
            continue
        ys = pd.to_numeric(d[y], errors="coerce")
        fig.add_trace(
            go.Scatter(
                x=d[x],
                y=ys,
                mode="lines+markers",
                name=y.replace("_", " ").title(),
                line=dict(width=2.8, color=_PALETTE[i % len(_PALETTE)], shape="spline"),
                marker=dict(size=9, color=_PALETTE[i % len(_PALETTE)], line=dict(width=1.5, color="white")),
                hovertemplate=(
                    f"%{{x|%b %d, %Y}}<br>{y}: %{{y:{yfmt}}}"
                    + ("%" if _is_rate(y) else "")
                    + "<extra></extra>"
                ),
            )
        )
    y_title = y_cols[0].replace("_", " ").title() if len(y_cols) == 1 else "Value"
    fig.update_yaxes(title=y_title, rangemode="tozero")
    if len(y_cols) == 1 and _is_rate(y_cols[0]):
        fig.update_yaxes(ticksuffix="%")
    return _apply_base_layout(fig, title=chart_title, hovermode="x unified")


def _plotly_time_single(df: pd.DataFrame, x: str, y: str) -> Optional[go.Figure]:
    d = df[[x, y]].dropna().copy()
    d[x] = pd.to_datetime(d[x], errors="coerce")
    d = d.dropna(subset=[x]).sort_values(x)
    if d.empty:
        return None
    med = _median_spacing_days(d[x])
    n = len(d)
    # Sparse / weekly / monthly: bars (matches product analytics defaults)
    if n <= 14 or (med is not None and med >= 6):
        return _plotly_coarse_period_bars(d, x, y, chart_title=None)

    xs = d[x]
    ys = pd.to_numeric(d[y], errors="coerce")
    yfmt = _num_fmt(y)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="lines+markers",
            name=y.replace("_", " "),
            line=dict(color=_PALETTE[0], width=3, shape="spline"),
            marker=dict(size=11, color=_PALETTE[0], line=dict(width=2, color="white")),
            fill="tozeroy",
            fillcolor="rgba(37, 99, 235, 0.12)",
            hovertemplate=(
                f"%{{x|%b %d, %Y}}<br>{y}: %{{y:{yfmt}}}"
                + ("%" if _is_rate(y) else "")
                + "<extra></extra>"
            ),
        )
    )
    y_title = y.replace("_", " ").title()
    fig.update_yaxes(title=y_title, tickformat=yfmt if "%" not in yfmt else None)
    if _is_rate(y):
        fig.update_yaxes(ticksuffix="%")
    return _apply_base_layout(fig, title=None)


def _plotly_time_by_category(
    df: pd.DataFrame, x: str, y: str, cat: str, *, barnorm_pct: bool = False
) -> Optional[go.Figure]:
    d = df[[x, y, cat]].dropna().copy()
    try:
        d[x] = pd.to_datetime(d[x])
    except Exception:
        pass
    d[y] = pd.to_numeric(d[y], errors="coerce")
    d = d.dropna(subset=[y])
    # Top series by total volume for readability
    totals = d.groupby(cat, observed=True)[y].sum().sort_values(ascending=False)
    top_cats = totals.head(12).index.tolist()
    d = d[d[cat].isin(top_cats)]
    if d.empty:
        return None

    n_time = int(d[x].nunique(dropna=True))
    # Coarse time (monthly/weekly columns or ≤18 unique periods) → stacked bar chart
    use_stacked = n_time <= 18 or x in ("month", "week")

    # 100% stacked: show share instead of absolute, tooltip shows both
    use_pct = use_stacked and barnorm_pct and not _is_rate(y)

    yfmt = _num_fmt(y)
    fig = go.Figure()
    for i, cval in enumerate(top_cats):
        sub = d[d[cat] == cval].sort_values(x)
        color = _PALETTE[i % len(_PALETTE)]
        if use_stacked:
            hover = (
                f"<b>{str(cval)}</b><br>%{{x|%b %Y}}<br>"
                + ("%{customdata:.1f}% share<br>" if use_pct else "")
                + f"{y}: %{{y:{yfmt}}}<extra></extra>"
            )
            trace_kwargs: dict = dict(
                x=sub[x],
                y=sub[y],
                name=str(cval).replace("_", " "),
                marker_color=color,
                hovertemplate=hover,
            )
            if use_pct:
                # Pass raw count as customdata so hover shows absolute count
                trace_kwargs["customdata"] = None  # will be filled by barnorm
            fig.add_trace(go.Bar(**trace_kwargs))
        else:
            fig.add_trace(
                go.Scatter(
                    x=sub[x],
                    y=sub[y],
                    mode="lines+markers",
                    name=str(cval).replace("_", " "),
                    line=dict(width=2.8, color=color, shape="spline"),
                    marker=dict(size=9, color=color, line=dict(width=1.5, color="white")),
                    hovertemplate=(
                        f"{cat}={str(cval)}<br>"
                        f"%{{x|%b %d, %Y}}<br>{y}: %{{y:{yfmt}}}<extra></extra>"
                    ),
                )
            )
    if use_stacked:
        layout_kwargs: dict = dict(barmode="stack", bargap=0.18)
        if use_pct:
            layout_kwargs["barnorm"] = "percent"
            layout_kwargs["yaxis"] = dict(title="% share", ticksuffix="%", range=[0, 100])
        else:
            fig.update_yaxes(title=y.replace("_", " ").title())
        fig.update_layout(**layout_kwargs)
    else:
        fig.update_yaxes(title=y.replace("_", " ").title())
    return _apply_base_layout(fig, title=None)


_DONUT_MAX_SLICES = 8  # top N categories shown; remainder collapsed to "Others"


def _plotly_donut(
    df: pd.DataFrame, cat: str, val: str, *, title: str | None = None
) -> Optional[go.Figure]:
    """Donut chart with percentage labels for categorical user distributions.

    Caps at _DONUT_MAX_SLICES named slices; any remaining rows are grouped as
    "Others" so the chart stays readable regardless of category count.
    """
    d = df.copy()
    d[val] = pd.to_numeric(d[val], errors="coerce").fillna(0)
    d = d[d[val] > 0].sort_values(val, ascending=False)
    if d.empty:
        return None

    if len(d) > _DONUT_MAX_SLICES:
        top = d.head(_DONUT_MAX_SLICES)
        others_val = float(d.iloc[_DONUT_MAX_SLICES:][val].sum())
        others_row = pd.DataFrame({cat: ["Others"], val: [others_val]})
        d = pd.concat([top, others_row], ignore_index=True)

    n = len(d)
    colors = _PALETTE[:_DONUT_MAX_SLICES] + ["#94a3b8"]  # grey for "Others"
    labels = d[cat].astype(str).str.replace("_", " ").tolist()
    values = d[val].tolist()
    fig = go.Figure(
        go.Pie(
            labels=labels,
            values=values,
            hole=0.52,
            marker=dict(colors=colors[:n], line=dict(color="white", width=2)),
            texttemplate="%{label}<br><b>%{percent:.1%}</b>",
            textposition="outside",
            hovertemplate="%{label}<br>Users: %{value:,.0f}<br>Share: %{percent:.1%}<extra></extra>",
            sort=False,
        )
    )
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Inter, system-ui, sans-serif", size=12, color="#334155"),
        paper_bgcolor="#ffffff",
        margin=dict(l=20, r=20, t=52 if title else 32, b=20),
        showlegend=True,
        legend=dict(
            orientation="v",
            yanchor="middle",
            y=0.5,
            xanchor="left",
            x=1.05,
            font=dict(size=12),
        ),
        title=(
            dict(text=title, x=0.0, xanchor="left", font=dict(size=14, color="#0f172a"))
            if title
            else None
        ),
    )
    return fig


def _plotly_auto(df: pd.DataFrame, *, chart_title: str | None = None) -> Optional[go.Figure]:
    if df is None or df.empty or len(df.columns) < 2:
        return None

    if "step_name" in df.columns and "users" in df.columns:
        return _plotly_funnel(df)

    date_cols = [c for c in df.columns if _is_temporal_col(df, c)]
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cat_cols = [c for c in df.columns if c not in date_cols and c not in num_cols]

    if not num_cols:
        return None

    if date_cols and num_cols:
        x = date_cols[0]
        try:
            n_time = int(df[x].nunique(dropna=True))
        except Exception:
            n_time = len(df)
        if cat_cols:
            y = _preferred_chart_metric_col(num_cols) or num_cols[0]
            cat = cat_cols[0]
            if n_time <= 1:
                default_t = f"{y.replace('_', ' ')} by {cat.replace('_', ' ')}"
                return _plotly_hbar_categories(df, cat, y, title=chart_title or default_t)
            # Use 100% stacked bars for categorical distribution over time
            return _plotly_time_by_category(df, x, y, cat, barnorm_pct=True)
        plot_cols = _chart_metric_columns(num_cols)
        if len(plot_cols) >= 2:
            multi = _plotly_time_multi(df, x, plot_cols, chart_title=chart_title)
            if multi is not None:
                return multi
        y = plot_cols[0]
        return _plotly_time_single(df, x, y)

    if cat_cols and num_cols:
        cat = cat_cols[0]
        y = _preferred_chart_metric_col(num_cols) or num_cols[0]
        default_t = f"{y.replace('_', ' ')} by {cat.replace('_', ' ')}"
        # Non-rate categorical distributions always use donut (top 8 + Others)
        if not _is_rate(y) and len(df) > 1:
            fig = _plotly_donut(df, cat, y, title=chart_title or default_t)
            if fig is not None:
                return fig
        return _plotly_hbar_categories(df, cat, y, title=chart_title or default_t)

    return None


def _plotly_lifecycle_stages(df: pd.DataFrame) -> Optional[go.Figure]:
    """
    Colored donut chart for user lifecycle stage distribution.
    Expects columns: lifecycle_stage, users (+ optional pct_of_total).
    """
    if "lifecycle_stage" not in df.columns or "users" not in df.columns:
        return None
    d = df.copy()
    d = d[d["users"] > 0].sort_values("lifecycle_stage")
    if d.empty:
        return None

    stage_colors = {
        "1.": "#22c55e",   # New — green
        "2.": "#86efac",   # Casual — light green
        "3.": "#2563eb",   # Engaged — blue
        "4.": "#7c3aed",   # Power User — purple
        "5.": "#f97316",   # At Risk — orange
        "6.": "#ef4444",   # Churned — red
    }
    colors = []
    for stage in d["lifecycle_stage"].astype(str):
        prefix = stage[:2]
        colors.append(stage_colors.get(prefix, "#64748b"))

    labels = d["lifecycle_stage"].astype(str).str.replace(r"^\d+\.\s*", "", regex=True)
    values = pd.to_numeric(d["users"], errors="coerce").fillna(0).tolist()

    pct_text = []
    total = sum(values) or 1
    for v in values:
        pct_text.append(f"{v/total*100:.1f}%")

    fig = go.Figure(go.Pie(
        labels=labels.tolist(),
        values=values,
        hole=0.55,
        marker=dict(colors=colors, line=dict(color="white", width=2)),
        texttemplate="%{label}<br>%{value:,.0f} (%{percent:.1%})",
        textposition="outside",
        hovertemplate="%{label}<br>Users: %{value:,.0f}<br>Share: %{percent:.1%}<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Inter, system-ui, sans-serif", size=12, color="#334155"),
        paper_bgcolor="#ffffff",
        margin=dict(l=20, r=20, t=50, b=20),
        showlegend=True,
        legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.0),
        annotations=[dict(
            text="Lifecycle<br>Stages",
            x=0.5, y=0.5, font_size=13, showarrow=False,
            font=dict(color="#64748b"),
        )],
    )
    return fig


def _plotly_survival_curves(df: pd.DataFrame) -> Optional[go.Figure]:
    """
    Multi-line retention survival curves: D1, D7, D30 per cohort week.
    Expects columns: cohort_week, d1_retention_pct, d7_retention_pct, d30_retention_pct.
    """
    rate_cols = [c for c in ("d1_retention_pct", "d7_retention_pct", "d30_retention_pct")
                 if c in df.columns]
    time_col  = next((c for c in df.columns if "cohort" in c.lower()), None)
    if not time_col or not rate_cols:
        return None

    d = df[[time_col] + rate_cols].copy()
    try:
        d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    except Exception:
        pass
    d = d.dropna(subset=[time_col]).sort_values(time_col)
    if d.empty:
        return None

    colors = {"d1_retention_pct": "#22c55e", "d7_retention_pct": "#2563eb", "d30_retention_pct": "#7c3aed"}
    labels = {"d1_retention_pct": "D1 Retention", "d7_retention_pct": "D7 Retention", "d30_retention_pct": "D30 Retention"}

    fig = go.Figure()
    for col in rate_cols:
        y = pd.to_numeric(d[col], errors="coerce")
        fig.add_trace(go.Scatter(
            x=d[time_col],
            y=y,
            mode="lines+markers",
            name=labels.get(col, col),
            line=dict(width=2.5, color=colors.get(col, _PALETTE[0]), shape="spline"),
            marker=dict(size=7, color=colors.get(col, _PALETTE[0]), line=dict(width=1.5, color="white")),
            hovertemplate=f"%{{x|%b %d, %Y}}<br>{labels.get(col, col)}: %{{y:.1f}}%<extra></extra>",
        ))

    fig.update_yaxes(title="Retention %", rangemode="tozero", ticksuffix="%")
    fig.update_xaxes(title="Cohort Week")
    return _apply_base_layout(fig, title="Survival Curves (D1 / D7 / D30)", hovermode="x unified")


def _plotly_retention_heatmap(df: pd.DataFrame) -> Optional[go.Figure]:
    """
    Retention heatmap: cohort period (y-axis) × breakdown dimension (x-axis) × retention_pct (color).
    Expects columns: cohort_week|cohort_month, <dim_col>, retention_pct.
    """
    if "retention_pct" not in df.columns:
        return None
    cols = list(df.columns)
    time_col = next((c for c in cols if "cohort" in c.lower()), None)
    if not time_col:
        return None
    dim_cols = [c for c in cols if c not in (time_col, "retention_pct", "cohort_size", "retained_users")
                and not pd.api.types.is_numeric_dtype(df[c])]
    if not dim_cols:
        return None
    dim_col = dim_cols[0]

    d = df[[time_col, dim_col, "retention_pct"]].copy()
    try:
        d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    except Exception:
        pass
    d = d.dropna(subset=[time_col]).sort_values(time_col)
    if d.empty:
        return None

    pivot = d.pivot_table(index=time_col, columns=dim_col, values="retention_pct", aggfunc="mean")
    pivot = pivot.sort_index(ascending=False)
    y_labels = [str(r)[:10] if hasattr(r, "strftime") else str(r)[:10] for r in pivot.index]

    z_text = [[f"{v:.1f}%" if pd.notna(v) else "—" for v in row] for row in pivot.values.tolist()]

    fig = go.Figure(go.Heatmap(
        z=pivot.values.tolist(),
        x=[str(c).replace("_", " ").title() for c in pivot.columns],
        y=y_labels,
        text=z_text,
        texttemplate="%{text}",
        colorscale="RdYlGn",
        zmin=0,
        zmax=100,
        hovertemplate="Cohort: %{y}<br>%{x}<br>Retention: %{z:.1f}%<extra></extra>",
        colorbar=dict(title="Retention %", titleside="right", ticksuffix="%"),
    ))
    time_label = "Cohort Week" if "week" in time_col.lower() else "Cohort Month"
    fig.update_xaxes(title=dim_col.replace("_", " ").title())
    fig.update_yaxes(title=time_label)
    return _apply_base_layout(
        fig,
        title=f"Retention by {dim_col.replace('_', ' ').title()} × {time_label}",
        hovermode="closest",
    )


def _plotly_cohort_retention_simple_heatmap(
    df: pd.DataFrame, win: Optional[int] = None
) -> Optional[go.Figure]:
    """
    Single-column retention heatmap for simple cohort retention (no breakdown dimension).
    Y-axis = cohort month/week ordered newest-first, color = retention_pct.
    Immature cohorts (retention_pct == 0 in the most-recent rows) are greyed out.
    """
    if "retention_pct" not in df.columns:
        return None
    time_col = next((c for c in df.columns if "cohort" in c.lower()), None)
    if not time_col:
        return None

    d = df[[time_col, "retention_pct"]].copy()
    try:
        d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    except Exception:
        pass
    d = d.dropna(subset=[time_col]).sort_values(time_col)
    if d.empty:
        return None

    from datetime import date, timedelta
    today = date.today()
    _win = win or 7

    def _is_immature(cohort_dt) -> bool:
        if pd.isna(cohort_dt):
            return False
        try:
            import calendar
            y, m = cohort_dt.year, cohort_dt.month
            last_day = date(y, m, calendar.monthrange(y, m)[1])
            return (last_day + timedelta(days=_win)) > today
        except Exception:
            return False

    d["_immature"] = d[time_col].apply(_is_immature)

    y_labels = []
    for r in d[time_col]:
        try:
            y_labels.append(r.strftime("%b %Y"))
        except Exception:
            y_labels.append(str(r)[:10])

    z_vals = d["retention_pct"].tolist()
    z_text = []
    for v, imm in zip(z_vals, d["_immature"]):
        if imm:
            z_text.append("pending")
        elif pd.isna(v):
            z_text.append("—")
        else:
            z_text.append(f"{v:.1f}%")

    # Grey out immature cohorts by masking their z value for colour
    z_display = [None if imm else v for v, imm in zip(z_vals, d["_immature"])]

    col_label = f"D{_win} Retention" if win else "Retention"
    fig = go.Figure(go.Heatmap(
        z=[[v] for v in z_display],
        x=[col_label],
        y=y_labels,
        text=[[t] for t in z_text],
        texttemplate="%{text}",
        colorscale="RdYlGn",
        zmin=0,
        zmax=100,
        hovertemplate="Cohort: %{y}<br>Retention: %{text}<extra></extra>",
        colorbar=dict(title="Retention %", titleside="right", ticksuffix="%"),
    ))

    time_label = "Week" if "week" in time_col.lower() else "Month"
    fig.update_yaxes(title=f"Cohort {time_label}", autorange="reversed")
    fig.update_xaxes(title="")
    title_win = f"D{_win} " if win else ""
    return _apply_base_layout(
        fig,
        title=f"{title_win}Retention by Cohort {time_label}",
        hovermode="closest",
    )


def _plotly_xyz_heatmap(df: pd.DataFrame) -> Optional[go.Figure]:
    """
    XYZ matrix heatmap: cohort_month (y-axis) × dimension (x-axis) × users (color).
    Expects columns: cohort_month, <dim_col>, users.
    """
    cols = list(df.columns)
    num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    str_cols = [c for c in cols if c not in num_cols]
    time_cols = [c for c in str_cols if "cohort" in c.lower() or "month" in c.lower() or "week" in c.lower()]
    dim_cols  = [c for c in str_cols if c not in time_cols]

    if not time_cols or not dim_cols or "users" not in df.columns:
        return None

    time_col = time_cols[0]
    dim_col  = dim_cols[0]

    pivot = df.pivot_table(index=time_col, columns=dim_col, values="users", aggfunc="sum", fill_value=0)
    pivot = pivot.sort_index(ascending=False)

    # Limit to top 10 dimension values by total users
    top_dims = pivot.sum(axis=0).nlargest(10).index.tolist()
    pivot = pivot[top_dims]

    z_text = [[f"{int(v):,}" for v in row] for row in pivot.values.tolist()]

    fig = go.Figure(go.Heatmap(
        z=pivot.values.tolist(),
        x=[str(c).replace("_", " ") for c in pivot.columns],
        y=[str(r) for r in pivot.index],
        text=z_text,
        texttemplate="%{text}",
        colorscale="Blues",
        hovertemplate="Cohort: %{y}<br>%{x}<br>Users: %{z:,.0f}<extra></extra>",
        colorbar=dict(title="Users", titleside="right"),
    ))
    fig.update_xaxes(title=dim_col.replace("_", " ").title(), tickangle=-30)
    fig.update_yaxes(title="Cohort Month")
    return _apply_base_layout(fig, title=f"Cohort × {dim_col.replace('_', ' ').title()} Matrix", hovermode="closest")


def _plotly_sankey_journey(df: pd.DataFrame) -> Optional[go.Figure]:
    """
    Sankey flow diagram for two-hop user journeys.
    Expects columns: source, target, users.
    """
    if not all(c in df.columns for c in ("source", "target", "users")):
        return None
    d = df[df["users"] > 0].copy()
    if d.empty:
        return None

    # Build node list (deduplicated)
    all_nodes = pd.unique(d[["source", "target"]].values.ravel("K")).tolist()
    node_idx  = {n: i for i, n in enumerate(all_nodes)}

    sources = [node_idx[s] for s in d["source"]]
    targets = [node_idx[t] for t in d["target"]]
    values  = pd.to_numeric(d["users"], errors="coerce").fillna(0).astype(int).tolist()

    node_labels = [str(n).replace("_", " ") for n in all_nodes]

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=12,
            thickness=18,
            label=node_labels,
            color=[_PALETTE[i % len(_PALETTE)] for i in range(len(all_nodes))],
            line=dict(color="white", width=0.5),
        ),
        link=dict(
            source=sources,
            target=targets,
            value=values,
            hovertemplate="%{source.label} → %{target.label}<br>%{value:,.0f} users<extra></extra>",
        ),
    ))
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Inter, system-ui, sans-serif", size=11, color="#334155"),
        paper_bgcolor="#ffffff",
        margin=dict(l=20, r=20, t=50, b=20),
        title=dict(text="User Journey Flow", x=0.0, xanchor="left",
                   font=dict(size=14, color="#0f172a")),
    )
    return fig


def _plotly_power_user_curve(df: pd.DataFrame) -> Optional[go.Figure]:
    """
    Power user frequency distribution as a horizontal bar chart.
    Expects columns: frequency_bucket, users, pct_of_total.
    """
    if "frequency_bucket" not in df.columns or "users" not in df.columns:
        return None
    d = df.copy()
    bucket_colors = [
        "#e2e8f0", "#cbd5e1", "#93c5fd", "#60a5fa", "#2563eb", "#7c3aed"
    ]
    vals   = pd.to_numeric(d["users"],         errors="coerce").fillna(0).tolist()
    labels = d["frequency_bucket"].astype(str).tolist()
    colors = [bucket_colors[min(i, len(bucket_colors)-1)] for i in range(len(d))]

    pct = pd.to_numeric(d.get("pct_of_total", pd.Series([0]*len(d))), errors="coerce").fillna(0).tolist()
    text_lbl = [f"{int(v):,}  ({p:.1f}%)" for v, p in zip(vals, pct)]

    fig = go.Figure(go.Bar(
        y=labels,
        x=vals,
        orientation="h",
        marker=dict(color=colors, line=dict(width=0)),
        text=text_lbl,
        textposition="outside",
        textfont=dict(size=11, color="#334155"),
        hovertemplate="%{y}<br>Users: %{x:,.0f}<extra></extra>",
    ))
    fig.update_yaxes(autorange="reversed", title="")
    fig.update_xaxes(title="Users", rangemode="tozero")
    return _apply_base_layout(fig, title="Power User Distribution (Event Frequency)", hovermode="closest")


def _plotly_funnel_drilldown(df: pd.DataFrame) -> Optional[go.Figure]:
    """
    Funnel property drilldown: grouped bars showing entered vs converted per dimension value.
    Expects columns: <dim>, entered, converted, cvr_pct.
    """
    cols = list(df.columns)
    num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    cat_cols = [c for c in cols if c not in num_cols]
    if not cat_cols or "entered" not in cols or "converted" not in cols:
        return None

    d = df.sort_values("entered", ascending=False).head(15)
    cat  = cat_cols[0]
    cats = d[cat].astype(str).str.replace("_", " ").tolist()
    entered   = pd.to_numeric(d["entered"],   errors="coerce").fillna(0).tolist()
    converted = pd.to_numeric(d["converted"], errors="coerce").fillna(0).tolist()
    cvr       = pd.to_numeric(d.get("cvr_pct", pd.Series([0]*len(d))), errors="coerce").fillna(0).tolist()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=cats, x=entered, name="Entered",
        orientation="h", marker=dict(color="#93c5fd", line=dict(width=0)),
        hovertemplate="%{y}<br>Entered: %{x:,.0f}<extra></extra>",
        offsetgroup=0,
    ))
    fig.add_trace(go.Bar(
        y=cats, x=converted, name="Converted",
        orientation="h", marker=dict(color="#2563eb", line=dict(width=0)),
        text=[f"{c:.1f}%" for c in cvr], textposition="outside",
        textfont=dict(size=11, color="#2563eb"),
        hovertemplate="%{y}<br>Converted: %{x:,.0f} (%{text} CVR)<extra></extra>",
        offsetgroup=1,
    ))
    fig.update_layout(barmode="overlay", bargroupgap=0.1)
    fig.update_yaxes(autorange="reversed", title="")
    fig.update_xaxes(title="Users", rangemode="tozero")
    return _apply_base_layout(
        fig, title=f"Funnel Conversion by {cats[0][:30] if cats else 'Dimension'} — Step 1 → Step 2",
        hovermode="closest",
    )


def _route_by_semantic_chart_type(
    chart_type: str, df: pd.DataFrame, qo_semantics=None
) -> Optional[go.Figure]:
    """Dispatch to a specific chart builder from a preferred_chart token."""
    if chart_type == "retention_heatmap":
        fig = _plotly_retention_heatmap(df)
        if fig is not None:
            return fig
        _win = None
        if qo_semantics is not None:
            _ret = getattr(getattr(qo_semantics, "retention", None), "return_window_days", None)
            _win = int(_ret) if _ret else None
        return _plotly_cohort_retention_simple_heatmap(df, win=_win)
    if chart_type == "retention_line":
        # Fall through to _plotly_auto which handles retention_pct time series
        return None
    if chart_type == "funnel_bar":
        return _plotly_funnel(df)
    if chart_type == "lifecycle_stages":
        return _plotly_lifecycle_stages(df)
    return None


def evidence_chart_plotly(
    inv_name: str,
    df: pd.DataFrame,
    *,
    chart_title: str | None = None,
    qo_semantics=None,
) -> Optional[go.Figure]:
    """
    Build a Plotly figure for query / investigation evidence.
    Returns None to let the caller fall back to Altair (heatmaps, custom routes).
    """
    if df is None or df.empty or len(df.columns) < 2:
        return None

    # Semantics-driven chart routing takes priority over column-name heuristics
    if qo_semantics is not None:
        pref = getattr(qo_semantics, "preferred_chart", None)
        if pref:
            fig = _route_by_semantic_chart_type(pref, df, qo_semantics=qo_semantics)
            if fig is not None:
                return fig

    name = (inv_name or "").lower()
    if name == "retention_matrix":
        return None
    if name.startswith("driver_contribution_"):
        return None
    if name == "forecast_trend":
        return None

    # New specialized chart routes
    if name == "lifecycle_stages" or "lifecycle_stage" in df.columns:
        fig = _plotly_lifecycle_stages(df)
        if fig is not None:
            return fig

    if name == "retention_survival_curve" or all(
        c in df.columns for c in ("d1_retention_pct", "d7_retention_pct")
    ):
        fig = _plotly_survival_curves(df)
        if fig is not None:
            return fig

    # Retention heatmap: cohort_week/month × breakdown dimension × retention_pct
    if "retention_pct" in df.columns and any("cohort" in c.lower() for c in df.columns):
        fig = _plotly_retention_heatmap(df)
        if fig is not None:
            return fig
        # No breakdown — fall back to single-column cohort heatmap
        _win = None
        if qo_semantics is not None:
            _ret = getattr(getattr(qo_semantics, "retention", None), "return_window_days", None)
            _win = int(_ret) if _ret else None
        fig = _plotly_cohort_retention_simple_heatmap(df, win=_win)
        if fig is not None:
            return fig

    if name == "xyz_cohort_matrix" or (
        any("cohort" in c.lower() for c in df.columns)
        and "users" in df.columns
        and len([c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]) >= 2
    ):
        fig = _plotly_xyz_heatmap(df)
        if fig is not None:
            return fig

    if name in ("journey_two_hop",) or all(c in df.columns for c in ("source", "target", "users")):
        fig = _plotly_sankey_journey(df)
        if fig is not None:
            return fig

    if name == "power_user_curve" or "frequency_bucket" in df.columns:
        fig = _plotly_power_user_curve(df)
        if fig is not None:
            return fig

    if name == "funnel_property_drilldown" or (
        "entered" in df.columns and "converted" in df.columns and "cvr_pct" in df.columns
    ):
        fig = _plotly_funnel_drilldown(df)
        if fig is not None:
            return fig

    title = (chart_title or "").strip() or (inv_name.strip() if inv_name.strip() else None)
    return _plotly_auto(df, chart_title=title)
