"""
charts.py — Semantic chart renderers for the analytics agent.

chart_for(inv_name, df) is the single entry point.
It routes to the right chart type based on the investigation name,
then falls back to column-structure heuristics for unknown types.

New chart types vs. the original auto_chart():
  • Retention cohort heatmap       (inv: retention_matrix)
  • Contribution waterfall          (inv: driver_contribution_*)
  • Forecast split chart            (inv: forecast_trend)
  • Funnel with drop-off rates      (inv: funnel)
  • Annotated time series           (peak / trough / biggest-swing labels)

Usage:
    from core.viz.charts import chart_for
    chart = chart_for("retention_matrix", inv.df)
    if chart:
        st.altair_chart(chart, use_container_width=True)
"""

from __future__ import annotations

from typing import Optional

import altair as alt
import pandas as pd

# ── Shared design tokens (match chat.py exactly) ──────────────────────────────

_FONT = "Inter, -apple-system, BlinkMacSystemFont, sans-serif"
_PALETTE = [
    "#3b82f6", "#f97316", "#10b981", "#8b5cf6", "#ef4444",
    "#6366f1", "#06b6d4", "#f59e0b", "#84cc16", "#ec4899",
]
_PALETTE_DUAL = ["#3b82f6", "#f97316"]
_GREEN = "#10b981"
_RED   = "#ef4444"

_AXIS = alt.AxisConfig(
    gridColor="#f1f5f9", domainColor="#e2e8f0", tickColor="#e2e8f0",
    labelColor="#374151", titleColor="#6b7280",
    labelFont=_FONT, titleFont=_FONT,
    labelFontSize=12, titleFontSize=12,
    gridOpacity=0.8,
)


def _theme(chart: alt.Chart, height: int = 360, width: int | None = None) -> alt.Chart:
    """Width caps chart width so Streamlit does not stretch sparse series across the full viewport."""
    t = chart.configure(
        background="#ffffff",
        view=alt.ViewConfig(stroke="#e2e8f0", strokeWidth=1),
        axis=_AXIS, axisX=_AXIS, axisY=_AXIS,
        legend=alt.LegendConfig(
            labelColor="#374151", titleColor="#6b7280",
            labelFont=_FONT, titleFont=_FONT,
            labelFontSize=12, titleFontSize=12,
            orient="top-right",
            fillColor="white", strokeColor="#e2e8f0",
            padding=6,
        ),
        title=alt.TitleConfig(
            color="#111827", font=_FONT, fontSize=14,
            fontWeight=600, anchor="start",
        ),
    ).properties(height=height)
    if width is not None:
        t = t.properties(width=width)
    return t


def _bounded_chart_width(*, n_along_x: int, per_step: int = 52, lo: int = 420, hi: int = 880) -> int:
    return min(hi, max(lo, int(n_along_x) * per_step + 140))


def _is_rate(col: str) -> bool:
    _KW = ("rate", "pct", "ratio", "avg", "mean", "per_user",
           "conversion", "retention", "churn", "score", "share")
    return any(k in col.lower() for k in _KW)


def _fmt(col: str) -> str:
    return ".1f" if _is_rate(col) else ",.0f"


def _median_spacing_days(timestamps: pd.Series) -> float | None:
    ts = pd.to_datetime(timestamps, errors="coerce").dropna().sort_values()
    if len(ts) < 2:
        return None
    return float(ts.diff().dropna().dt.days.median())


# ── Routing table ─────────────────────────────────────────────────────────────

def chart_for(inv_name: str, df: pd.DataFrame) -> Optional[alt.Chart]:
    """
    Route to the right chart renderer based on investigation name.
    Falls back to column-structure heuristics for unknown names.

    Args:
        inv_name: investigation name from Investigation.name
        df:       result DataFrame

    Returns:
        Configured Altair chart, or None if the data isn't chartable.
    """
    if df is None or df.empty or len(df.columns) < 2:
        return None

    name = inv_name.lower()

    # Named routes — exact or prefix match
    if name == "retention_matrix":
        return _chart_retention_heatmap(df)
    if name.startswith("driver_contribution_"):
        dim = name.replace("driver_contribution_", "", 1)
        return _chart_contribution_waterfall(df, dim)
    if name == "forecast_trend":
        return _chart_forecast_split(df)
    if name in ("funnel", "overall_conversion"):
        return _chart_funnel_with_rates(df)

    # Heuristic fallback for everything else
    return _chart_auto(df)


# ── 1. Retention heatmap ──────────────────────────────────────────────────────

def _chart_retention_heatmap(df: pd.DataFrame) -> Optional[alt.Chart]:
    """
    Amplitude-style cohort retention heatmap.
    Expects columns: cohort_* (date/week/month), period_idx, retention_pct.
    Cell color intensity = retention %; cell text = rounded % value.
    """
    try:
        cohort_cols = [c for c in df.columns if c.startswith("cohort_")]
        if not cohort_cols or "period_idx" not in df.columns:
            return None
        if "retention_pct" not in df.columns:
            return None

        cohort_col = cohort_cols[0]
        # Format cohort labels for readability
        df = df.copy()
        try:
            df["_cohort_lbl"] = pd.to_datetime(df[cohort_col]).dt.strftime("%b %d")
        except Exception:
            df["_cohort_lbl"] = df[cohort_col].astype(str)
        df["_period_lbl"] = "D" + df["period_idx"].astype(str)

        # cohort size in tooltip
        has_size = "cohort_size" in df.columns
        has_ret  = "retained_users" in df.columns

        tooltip_fields = [
            alt.Tooltip("_cohort_lbl:N", title="Cohort"),
            alt.Tooltip("_period_lbl:N", title="Period"),
            alt.Tooltip("retention_pct:Q", title="Retention %", format=".1f"),
        ]
        if has_size:
            tooltip_fields.append(alt.Tooltip("cohort_size:Q", title="Cohort size", format=","))
        if has_ret:
            tooltip_fields.append(alt.Tooltip("retained_users:Q", title="Retained", format=","))

        n_cohorts  = df["_cohort_lbl"].nunique()
        cell_h     = max(28, min(52, 400 // max(n_cohorts, 1)))
        n_periods  = df["_period_lbl"].nunique()
        total_h    = max(200, n_cohorts * (cell_h + 4) + 60)

        heat = (
            alt.Chart(df)
            .mark_rect(cornerRadius=3)
            .encode(
                x=alt.X("_period_lbl:O",
                         sort=df["_period_lbl"].unique().tolist(),
                         title="Cohort Age",
                         axis=alt.Axis(labelAngle=0, labelFontWeight=600)),
                y=alt.Y("_cohort_lbl:O",
                         sort=df["_cohort_lbl"].unique().tolist(),
                         title="",
                         axis=alt.Axis(labelFontSize=11)),
                color=alt.Color(
                    "retention_pct:Q",
                    scale=alt.Scale(scheme="blues", domain=[0, 100]),
                    legend=alt.Legend(title="Retention %", orient="right"),
                ),
                tooltip=tooltip_fields,
            )
        )
        # Adaptive text color: white on dark cells, dark on light cells
        text = (
            alt.Chart(df)
            .mark_text(fontSize=10, fontWeight=600, font=_FONT)
            .encode(
                x=alt.X("_period_lbl:O",
                         sort=df["_period_lbl"].unique().tolist()),
                y=alt.Y("_cohort_lbl:O",
                         sort=df["_cohort_lbl"].unique().tolist()),
                text=alt.Text("retention_pct:Q", format=".0f"),
                color=alt.condition(
                    alt.datum.retention_pct > 45,
                    alt.value("white"),
                    alt.value("#374151"),
                ),
            )
        )
        chart = _theme(heat + text, height=total_h)
        return chart.properties(width=min(600, n_periods * 60 + 120))
    except Exception:
        return None


# ── 2. Contribution waterfall ─────────────────────────────────────────────────

def _chart_contribution_waterfall(
    df: pd.DataFrame,
    dim_col: str = "",
) -> Optional[alt.Chart]:
    """
    Horizontal waterfall chart for driver contribution investigations.
    Expects: dim_col, abs_contrib, pct_contrib (+ optionally curr_n, prev_n).
    Positive segments = green, negative = red.
    """
    try:
        needed = {"abs_contrib", "pct_contrib"}
        if not needed.issubset(set(df.columns)):
            return None

        # Infer dimension column
        skip = {"curr_n", "prev_n", "abs_contrib", "pct_contrib"}
        dim_candidates = [c for c in df.columns if c not in skip]
        cat = dim_col if dim_col in df.columns else (dim_candidates[0] if dim_candidates else None)
        if cat is None:
            return None

        df = df.copy().dropna(subset=["abs_contrib"])
        df = df.sort_values("abs_contrib")  # ascending so largest negative is at top

        n        = len(df)
        bar_size = max(22, min(40, 280 // max(n, 1)))

        has_curr = "curr_n" in df.columns
        has_prev = "prev_n" in df.columns
        tooltip  = [
            alt.Tooltip(f"{cat}:N"),
            alt.Tooltip("abs_contrib:Q", title="Change in users", format="+,.0f"),
            alt.Tooltip("pct_contrib:Q", title="% of total delta", format="+.1f"),
        ]
        if has_curr:
            tooltip.append(alt.Tooltip("curr_n:Q", title="Current", format=","))
        if has_prev:
            tooltip.append(alt.Tooltip("prev_n:Q", title="Previous", format=","))

        bars = (
            alt.Chart(df)
            .mark_bar(
                size=bar_size,
                cornerRadiusTopRight=4,
                cornerRadiusBottomRight=4,
            )
            .encode(
                y=alt.Y(
                    f"{cat}:N",
                    sort=df[cat].tolist(),
                    title="",
                    axis=alt.Axis(labelLimit=160, labelFontSize=11),
                ),
                x=alt.X(
                    "abs_contrib:Q",
                    title="Change in users",
                    axis=alt.Axis(format="+,.0f"),
                ),
                color=alt.condition(
                    alt.datum.abs_contrib >= 0,
                    alt.value(_GREEN),
                    alt.value(_RED),
                ),
                tooltip=tooltip,
            )
        )

        # Zero reference line
        rule = (
            alt.Chart(pd.DataFrame({"x": [0]}))
            .mark_rule(color="#374151", strokeWidth=1.5)
            .encode(x="x:Q")
        )

        # Inline labels: "+1,234 (12.3%)" or "−1,234 (−12.3%)"
        df["_lbl"] = df.apply(
            lambda r: f"{int(r['abs_contrib']):+,}  ({r['pct_contrib']:+.1f}%)", axis=1
        )
        labels = (
            alt.Chart(df)
            .mark_text(font=_FONT, fontSize=10, fontWeight=600)
            .encode(
                y=alt.Y(f"{cat}:N", sort=df[cat].tolist()),
                x=alt.X("abs_contrib:Q"),
                text=alt.Text("_lbl:N"),
                align=alt.condition(
                    alt.datum.abs_contrib >= 0,
                    alt.value("left"),
                    alt.value("right"),
                ),
                dx=alt.condition(
                    alt.datum.abs_contrib >= 0,
                    alt.value(6),
                    alt.value(-6),
                ),
                color=alt.condition(
                    alt.datum.abs_contrib >= 0,
                    alt.value("#065f46"),
                    alt.value("#991b1b"),
                ),
            )
        )

        return _theme(bars + rule + labels, height=n * (bar_size + 18) + 40)
    except Exception:
        return None


# ── 3. Forecast split chart ───────────────────────────────────────────────────

def _chart_forecast_split(df: pd.DataFrame) -> Optional[alt.Chart]:
    """
    Actual (solid blue line) + projected (dashed orange line + shaded zone).
    Expects: date/week/month column, 'actual' (nullable), 'forecast' (nullable).
    """
    try:
        date_cols = [c for c in df.columns
                     if any(k in c.lower() for k in ("date", "week", "month", "period"))]
        if not date_cols:
            return None
        x = date_cols[0]
        if "actual" not in df.columns or "forecast" not in df.columns:
            return None

        df = df.copy()
        try:
            df[x] = pd.to_datetime(df[x])
        except Exception:
            pass

        x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
        x_type    = "T" if x_is_temp else "O"
        x_fmt     = "%b %d" if df[x].nunique() > 14 else "%b '%y"

        actual_df   = df.dropna(subset=["actual"]).copy()
        forecast_df = df.dropna(subset=["forecast"]).copy()

        has_actual   = not actual_df.empty
        has_forecast = not forecast_df.empty

        # Find the split point date for the vertical rule
        split_df = None
        if has_forecast:
            split_val = forecast_df[x].min()
            split_df  = pd.DataFrame({x: [split_val], "_lbl": ["Forecast →"]})

        # Common x encoding
        x_enc = alt.X(
            f"{x}:{x_type}",
            title="",
            axis=alt.Axis(
                format=x_fmt if x_is_temp else None,
                labelAngle=-30,
                grid=False,
            ),
        )

        layers = []

        if has_forecast:
            # Shaded forecast zone (light orange fill under the forecast line)
            area_fc = (
                alt.Chart(forecast_df)
                .mark_area(color="#f97316", opacity=0.08, line=False)
                .encode(
                    x=x_enc,
                    y=alt.Y("forecast:Q", title="", axis=alt.Axis(format=",.0f")),
                )
            )
            line_fc = (
                alt.Chart(forecast_df)
                .mark_line(color="#f97316", strokeWidth=2.5, strokeDash=[6, 4])
                .encode(
                    x=x_enc,
                    y=alt.Y("forecast:Q"),
                    tooltip=[
                        alt.Tooltip(f"{x}:{x_type}", format=x_fmt if x_is_temp else None),
                        alt.Tooltip("forecast:Q", title="Forecast", format=",.0f"),
                    ],
                )
            )
            layers.extend([area_fc, line_fc])

        if has_actual:
            # Gradient area under actual
            area_ac = (
                alt.Chart(actual_df)
                .mark_area(
                    color=alt.Gradient(
                        gradient="linear",
                        stops=[
                            alt.GradientStop(color=_PALETTE[0], offset=0),
                            alt.GradientStop(color="#ffffff",    offset=1),
                        ],
                        x1=0, x2=0, y1=0, y2=1,
                    ),
                    opacity=0.12, line=False,
                )
                .encode(
                    x=x_enc,
                    y=alt.Y("actual:Q", title="Users / period", axis=alt.Axis(format=",.0f")),
                )
            )
            line_ac = (
                alt.Chart(actual_df)
                .mark_line(color=_PALETTE[0], strokeWidth=2.5)
                .encode(
                    x=x_enc,
                    y=alt.Y("actual:Q"),
                    tooltip=[
                        alt.Tooltip(f"{x}:{x_type}", format=x_fmt if x_is_temp else None),
                        alt.Tooltip("actual:Q", title="Actual", format=",.0f"),
                    ],
                )
            )
            dots_ac = (
                alt.Chart(actual_df)
                .mark_circle(color=_PALETTE[0], size=45, opacity=0.9)
                .encode(x=x_enc, y=alt.Y("actual:Q"))
            )
            layers.extend([area_ac, line_ac, dots_ac])

        # Vertical split-point rule + annotation
        if split_df is not None:
            rule = (
                alt.Chart(split_df)
                .mark_rule(color="#94a3b8", strokeWidth=1.5, strokeDash=[4, 3])
                .encode(x=alt.X(f"{x}:{x_type}"))
            )
            annot = (
                alt.Chart(split_df)
                .mark_text(
                    align="left", dx=6, dy=-14,
                    color="#f97316", fontSize=11, fontWeight=700, font=_FONT,
                )
                .encode(x=alt.X(f"{x}:{x_type}"), text="_lbl:N")
            )
            layers.extend([rule, annot])

        if not layers:
            return None

        return _theme(alt.layer(*layers).resolve_scale(y="shared"), height=320).interactive()
    except Exception:
        return None


# ── 4. Funnel with conversion rates ──────────────────────────────────────────

def _chart_funnel_with_rates(df: pd.DataFrame) -> Optional[alt.Chart]:
    """
    Horizontal funnel bars with:
    - Inline user count labels
    - Conversion % vs top-of-funnel
    - Drop-off annotation for the biggest losing step

    Expects: step_name, users, [step_num], [pct_of_top].
    """
    try:
        if "step_name" not in df.columns or "users" not in df.columns:
            return None

        df = df.copy()
        if "step_num" in df.columns:
            df = df.sort_values("step_num")
        else:
            df = df.reset_index(drop=True)

        # Compute pct_of_top if missing
        top = float(df["users"].iloc[0]) if not df.empty else 1.0
        if "pct_of_top" not in df.columns:
            df["pct_of_top"] = (df["users"] / max(top, 1) * 100).round(1)

        # Step-over-step conversion rate
        df["_step_rate"] = (
            (df["users"] / df["users"].shift(1) * 100)
            .round(1)
            .fillna(100.0)
        )

        # Combined tooltip label
        df["_lbl"] = df.apply(
            lambda r: (
                f"{int(r['users']):,}  "
                f"({r['pct_of_top']:.1f}% of top)"
            ),
            axis=1,
        )

        n        = len(df)
        bar_size = max(32, min(52, 380 // max(n, 1)))
        order    = df["step_name"].tolist()

        bars = (
            alt.Chart(df)
            .mark_bar(
                size=bar_size,
                cornerRadiusTopRight=5,
                cornerRadiusBottomRight=5,
            )
            .encode(
                y=alt.Y("step_name:N", sort=order, title="",
                         axis=alt.Axis(labelLimit=180, labelFontSize=12)),
                x=alt.X("users:Q", title="Users", axis=alt.Axis(format=",.0f")),
                color=alt.Color(
                    "step_name:N",
                    scale=alt.Scale(range=_PALETTE[:n]),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("step_name:N"),
                    alt.Tooltip("users:Q", format=",.0f"),
                    alt.Tooltip("pct_of_top:Q", title="% of funnel top", format=".1f"),
                    alt.Tooltip("_step_rate:Q", title="vs prev step", format=".1f"),
                ],
            )
        )

        # Inline count + pct-of-top label
        count_labels = (
            alt.Chart(df)
            .mark_text(
                align="left", dx=6, fontSize=11,
                fontWeight=600, color="#1f2937", font=_FONT,
            )
            .encode(
                y=alt.Y("step_name:N", sort=order),
                x=alt.X("users:Q"),
                text=alt.Text("_lbl:N"),
            )
        )

        # Step-over-step rate label (right-aligned above each bar, skip first)
        rate_df = df.iloc[1:].copy()  # skip first step (always 100%)
        rate_labels: list[alt.Chart] = []
        if not rate_df.empty:
            rate_labels = [
                alt.Chart(rate_df)
                .mark_text(
                    align="right", dx=-6, fontSize=10,
                    fontWeight=500, color="#6b7280", font=_FONT,
                )
                .encode(
                    y=alt.Y("step_name:N", sort=order),
                    x=alt.value(0),
                    text=alt.Text("_step_rate:Q", format=".0f",
                                  formatType="number"),
                )
            ]

        all_layers = [bars, count_labels] + rate_labels
        return _theme(alt.layer(*all_layers), height=n * (bar_size + 20))
    except Exception:
        return None


# ── 5. Annotated time series ──────────────────────────────────────────────────

def _chart_timeseries_annotated(
    df: pd.DataFrame,
    x: str,
    y: str,
    cat: Optional[str] = None,
) -> Optional[alt.Chart]:
    """
    Line + gradient-area chart for a single numeric metric over time,
    with auto-detected peak/trough annotations.
    """
    try:
        df = df.copy()
        try:
            df[x] = pd.to_datetime(df[x])
        except Exception:
            pass

        x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
        x_type    = "T" if x_is_temp else "O"
        n_pts     = len(df)
        is_daily  = n_pts > 14 and x_is_temp
        x_fmt     = "%b %d" if is_daily else "%b '%y"
        v_fmt     = ".1f" if _is_rate(y) else ",.0f"
        y_title   = y.replace("_", " ").title()

        # For dense daily data, pin x-axis ticks to week-start Mondays so
        # labels read "Jan 06", "Jan 13", etc. instead of crowded daily entries.
        tick_values = None
        if x_is_temp and n_pts > 21:
            try:
                week_starts = pd.date_range(df[x].min(), df[x].max(), freq="W-MON").tolist()
                if week_starts:
                    tick_values = week_starts
                    x_fmt = "%b %d"
            except Exception:
                pass

        _axis_kw: dict = dict(
            format=x_fmt if x_is_temp else None,
            labelAngle=-30 if (is_daily or n_pts > 16) else 0,
            grid=False,
        )
        if tick_values:
            _axis_kw["values"] = tick_values

        x_enc = alt.X(
            f"{x}:{x_type}",
            title="",
            axis=alt.Axis(**_axis_kw),
        )

        base = alt.Chart(df).encode(
            x=x_enc,
            tooltip=[
                alt.Tooltip(f"{x}:{x_type}", format=x_fmt if x_is_temp else None),
                alt.Tooltip(f"{y}:Q", title=y_title, format=v_fmt),
            ],
        )

        area = base.mark_area(
            color=alt.Gradient(
                gradient="linear",
                stops=[
                    alt.GradientStop(color=_PALETTE[0], offset=0),
                    alt.GradientStop(color="#ffffff",    offset=1),
                ],
                x1=0, x2=0, y1=0, y2=1,
            ),
            opacity=0.13, line=False,
        ).encode(y=alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=v_fmt)))

        line = base.mark_line(
            color=_PALETTE[0], strokeWidth=3.2, interpolate="monotone"
        ).encode(y=alt.Y(f"{y}:Q"))

        dots = base.mark_circle(
            color=_PALETTE[0], size=70, opacity=0.95, stroke="white", strokeWidth=2
        ).encode(y=alt.Y(f"{y}:Q"))

        layers = [area, line, dots]

        # ── Annotations ─────────────────────────────────────────────────────
        # Only annotate when we have enough points to make it meaningful
        if n_pts >= 5:
            num_series = df[y].dropna()
            if len(num_series) >= 2:
                peak_idx   = num_series.idxmax()
                trough_idx = num_series.idxmin()
                peak_val   = num_series.max()
                trough_val = num_series.min()
                swing_pct  = (peak_val - trough_val) / max(abs(trough_val), 1) * 100

                # Only show if there's meaningful variation (>5%)
                if swing_pct > 5:
                    ann_rows = []
                    # Peak label
                    ann_rows.append({
                        x: df[x].iloc[peak_idx],
                        y: peak_val,
                        "_lbl": f"▲ {peak_val:{',d' if not _is_rate(y) else '.1f'}}",
                        "_dy": -14,
                    })
                    # Trough label (only if different from peak and swing is large)
                    if trough_idx != peak_idx and swing_pct > 15:
                        ann_rows.append({
                            x: df[x].iloc[trough_idx],
                            y: trough_val,
                            "_lbl": f"▼ {trough_val:{',d' if not _is_rate(y) else '.1f'}}",
                            "_dy": 14,
                        })
                    if ann_rows:
                        ann_df = pd.DataFrame(ann_rows)
                        ann = (
                            alt.Chart(ann_df)
                            .mark_text(
                                font=_FONT, fontSize=11, fontWeight=700,
                                color="#1f2937",
                            )
                            .encode(
                                x=alt.X(f"{x}:{x_type}"),
                                y=alt.Y(f"{y}:Q"),
                                text="_lbl:N",
                                dy=alt.value(ann_df["_dy"].iloc[0]
                                             if len(ann_df) == 1 else -12),
                            )
                        )
                        layers.append(ann)

        chart = alt.layer(*layers)
        w = _bounded_chart_width(n_along_x=max(3, n_pts))
        return _theme(chart, height=340, width=w).interactive()
    except Exception:
        return None


# ── 6. Auto-chart fallback ────────────────────────────────────────────────────
# (Column-structure heuristics — same logic as the original chat.py auto_chart)

def _chart_auto(df: pd.DataFrame) -> Optional[alt.Chart]:  # noqa: C901
    """
    Heuristic chart selection based on column structure alone.
    Used as fallback when the investigation name is unknown.
    Covers: pct_change diverging bar, pct_share donut/bar,
    period comparison grouped bar, time series, categorical bar.
    """
    try:
        _DATE_KW = ("date", "week", "month", "day", "period", "quarter", "year")
        date_cols = [c for c in df.columns if _is_temporal_col(df, c)]
        num_cols  = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        cat_cols  = [c for c in df.columns if c not in date_cols and c not in num_cols]

        for dc in date_cols:
            try:
                df[dc] = pd.to_datetime(df[dc])
            except Exception:
                pass

        def _single_period(col: str) -> bool:
            try:
                return df[col].nunique(dropna=True) <= 1
            except Exception:
                return len(df) <= 1

        # ── Funnel ────────────────────────────────────────────────────────────
        if "step_name" in df.columns and "users" in df.columns:
            return _chart_funnel_with_rates(df)

        # ── Single scalar → skip (shown as metric card) ───────────────────────
        if len(df) == 1 and num_cols and not date_cols:
            return None

        # ── pct_change diverging bar ──────────────────────────────────────────
        if "pct_change" in df.columns and cat_cols:
            return _chart_pct_change_bar(df, cat_cols[0])

        # ── pct_share + users ─────────────────────────────────────────────────
        if "pct_share" in df.columns and "users" in df.columns and cat_cols:
            return _chart_share(df, cat_cols[0])

        # ── Period comparison grouped bar ─────────────────────────────────────
        _CURR_KW = ("current", "curr", "this_period", "period_2")
        _PREV_KW = ("previous", "prev", "last_period", "prior", "period_1")
        curr_cols = [c for c in num_cols if any(k in c.lower() for k in _CURR_KW)]
        prev_cols = [c for c in num_cols if any(k in c.lower() for k in _PREV_KW)]
        if curr_cols and prev_cols and cat_cols:
            return _chart_period_comparison(df, cat_cols[0], curr_cols[0], prev_cols[0])

        # ── Time series ───────────────────────────────────────────────────────
        if date_cols and num_cols:
            return _chart_timeseries_dispatch(df, date_cols[0], num_cols, cat_cols)

        # ── cat + num fallback → horizontal bar ───────────────────────────────
        if cat_cols and num_cols:
            return _chart_hbar(df, cat_cols[0], num_cols[0])

    except Exception:
        pass
    return None


# ── Chart helpers used by _chart_auto ────────────────────────────────────────

def _is_temporal_col(df: pd.DataFrame, col: str) -> bool:
    if pd.api.types.is_datetime64_any_dtype(df[col]):
        return True
    s = df[col]
    if pd.api.types.is_numeric_dtype(s):
        return False
    name_ok = any(k in col.lower() for k in ("date", "week", "month", "day", "period", "year", "quarter"))
    if not name_ok:
        return False
    try:
        ratio = float(pd.to_datetime(s, errors="coerce").notna().mean())
        return ratio >= 0.7
    except Exception:
        return False


def _hbar_with_labels(df: pd.DataFrame, y_col: str, x_col: str, n: int) -> alt.Chart:
    order    = df.sort_values(x_col, ascending=True)[y_col].tolist()
    bar_size = max(24, min(44, 300 // max(n, 1)))
    fmt      = _fmt(x_col)
    bars = (
        alt.Chart(df)
        .mark_bar(size=bar_size, cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
        .encode(
            y=alt.Y(f"{y_col}:N", sort=order, title="",
                    axis=alt.Axis(labelLimit=180, labelFontSize=12)),
            x=alt.X(f"{x_col}:Q", title=x_col, axis=alt.Axis(format=fmt)),
            color=alt.Color(f"{y_col}:N",
                            scale=alt.Scale(range=_PALETTE[:n]), legend=None),
            tooltip=[alt.Tooltip(f"{y_col}:N"),
                     alt.Tooltip(f"{x_col}:Q", format=fmt)],
        )
    )
    labels = (
        alt.Chart(df)
        .mark_text(align="left", dx=5, fontSize=11, fontWeight=600,
                   color="#1f2937", font=_FONT)
        .encode(
            y=alt.Y(f"{y_col}:N", sort=order),
            x=alt.X(f"{x_col}:Q"),
            text=alt.Text(f"{x_col}:Q", format=fmt),
        )
    )
    return _theme(bars + labels, height=n * (bar_size + 20))


def _chart_pct_change_bar(df: pd.DataFrame, cat: str) -> Optional[alt.Chart]:
    df = df.dropna(subset=["pct_change"]).sort_values("pct_change")
    if df.empty:
        return None
    n        = len(df)
    bar_size = max(24, min(40, 280 // max(n, 1)))
    has_curr = "curr_n" in df.columns
    tooltips = [
        alt.Tooltip(f"{cat}:N"),
        alt.Tooltip("pct_change:Q", title="% change", format="+.1f"),
    ]
    if has_curr:
        tooltips += [
            alt.Tooltip("curr_n:Q", title="current",  format=","),
            alt.Tooltip("prev_n:Q", title="previous", format=","),
        ]
    bar = (
        alt.Chart(df)
        .mark_bar(size=bar_size, cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
        .encode(
            y=alt.Y(f"{cat}:N", sort=df[cat].tolist(), title="",
                    axis=alt.Axis(labelLimit=130, labelFontSize=11)),
            x=alt.X("pct_change:Q", title="% change",
                    axis=alt.Axis(format="+.1f")),
            color=alt.condition(
                alt.datum.pct_change >= 0, alt.value(_GREEN), alt.value(_RED)
            ),
            tooltip=tooltips,
        )
    )
    return _theme(bar, height=n * (bar_size + 20)).interactive()


def _chart_share(df: pd.DataFrame, cat: str) -> Optional[alt.Chart]:
    n       = min(len(df), 10)
    df_plot = df.head(n).sort_values("users", ascending=True).copy()
    pct_sum = float(df_plot["pct_share"].fillna(0).sum())
    looks_share = 90 <= pct_sum <= 110
    if n <= 8 and looks_share:
        return _donut(df_plot.sort_values("pct_share", ascending=False), cat, "pct_share", n)
    order    = df_plot[cat].tolist()
    bar_size = max(28, min(44, 300 // max(n, 1)))
    bars = (
        alt.Chart(df_plot)
        .mark_bar(size=bar_size, cornerRadiusTopRight=6, cornerRadiusBottomRight=6)
        .encode(
            y=alt.Y(f"{cat}:N", sort=order, title="",
                    axis=alt.Axis(labelLimit=180, labelFontSize=12)),
            x=alt.X("users:Q", title="users", axis=alt.Axis(format=",")),
            color=alt.Color(f"{cat}:N", scale=alt.Scale(range=_PALETTE[:n]), legend=None),
            tooltip=[
                alt.Tooltip(f"{cat}:N"),
                alt.Tooltip("users:Q",     title="users",   format=",.0f"),
                alt.Tooltip("pct_share:Q", title="% share", format=".1f"),
            ],
        )
    )
    labels = (
        alt.Chart(df_plot)
        .transform_calculate(lbl="format(datum.users, ',') + '  (' + toString(datum.pct_share) + '%)'")
        .mark_text(align="left", dx=8, fontSize=11, fontWeight=600,
                   color="#1f2937", font=_FONT)
        .encode(y=alt.Y(f"{cat}:N", sort=order), x=alt.X("users:Q"), text="lbl:N")
    )
    return _theme(bars + labels, height=n * (bar_size + 20))


def _donut(df: pd.DataFrame, cat: str, val: str, n: int) -> alt.Chart:
    pie = (
        alt.Chart(df)
        .mark_arc(innerRadius=55, outerRadius=115)
        .encode(
            theta=alt.Theta(f"{val}:Q"),
            color=alt.Color(f"{cat}:N", scale=alt.Scale(range=_PALETTE[:n]),
                            legend=alt.Legend(orient="right", title=cat)),
            tooltip=[alt.Tooltip(f"{cat}:N"), alt.Tooltip(f"{val}:Q", format=".1f")],
        )
    )
    center = (
        alt.Chart(pd.DataFrame({"t": ["Share"]}))
        .mark_text(font=_FONT, fontSize=15, fontWeight=700, color="#111827")
        .encode(text="t:N")
    )
    return _theme((pie + center), height=300).properties(width=420).interactive()


def _chart_period_comparison(
    df: pd.DataFrame, cat: str, curr: str, prev: str
) -> Optional[alt.Chart]:
    melted   = df[[cat, curr, prev]].melt(id_vars=cat, var_name="period", value_name="value")
    n        = len(df)
    v_fmt    = _fmt(curr)
    bar_size = max(20, min(36, 260 // max(n, 1)))
    bar = (
        alt.Chart(melted)
        .mark_bar(size=bar_size, cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
        .encode(
            y=alt.Y(f"{cat}:N", title="",
                    axis=alt.Axis(labelLimit=150, labelFontSize=11)),
            x=alt.X("value:Q", title="", axis=alt.Axis(format=v_fmt)),
            yOffset=alt.YOffset("period:N"),
            color=alt.Color("period:N",
                            scale=alt.Scale(range=[_PALETTE[0], _PALETTE[2]]),
                            legend=alt.Legend(orient="bottom", title="")),
            tooltip=[
                alt.Tooltip(f"{cat}:N"),
                alt.Tooltip("period:N"),
                alt.Tooltip("value:Q", format=v_fmt),
            ],
        )
    )
    return _theme(bar, height=max(200, n * (bar_size * 2 + 24))).interactive()


def _chart_timeseries_dispatch(
    df: pd.DataFrame,
    x: str,
    num_cols: list,
    cat_cols: list,
) -> Optional[alt.Chart]:
    """Route to the right time series sub-chart."""
    n_pts = len(df)
    try:
        s = pd.to_datetime(df[x], errors="coerce").dropna().sort_values()
        med_days = float(s.diff().dropna().dt.days.median()) if len(s) >= 2 else 1
        is_monthly = med_days >= 27
        is_weekly  = 6 <= med_days < 27
        is_coarse  = is_monthly or is_weekly or n_pts <= 12
    except Exception:
        is_coarse = n_pts <= 12

    any_rate = any(_is_rate(c) for c in num_cols)

    try:
        n_unique_time = int(df[x].nunique(dropna=True))
    except Exception:
        n_unique_time = n_pts

    # Multi-numeric, no category
    if len(num_cols) >= 2 and not cat_cols:
        if len(num_cols) == 2:
            return _chart_dual_axis(df, x, num_cols[0], num_cols[1])
        return _chart_multiline(df, x, num_cols)

    # Single numeric with category: one time bucket → snapshot bars, not a degenerate line chart
    if cat_cols:
        if n_unique_time <= 1:
            y0 = num_cols[0]
            d1 = df[[cat_cols[0], y0]].dropna().copy()
            if d1.empty:
                return None
            return _chart_hbar(
                d1.sort_values(y0, ascending=False).head(20),
                cat_cols[0],
                y0,
            )
        return _chart_category_lines(df, x, num_cols[0], cat_cols[0])

    # Single numeric
    y = num_cols[0]
    if n_pts <= 2:
        return _chart_two_period_bars(df, x, y)
    if is_coarse and not any_rate:
        return _chart_bar_counts(df, x, y)
    # Daily or rate → annotated line
    return _chart_timeseries_annotated(df, x, y)


def _chart_dual_axis(df, x, c0, c1) -> Optional[alt.Chart]:
    x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
    x_type    = "T" if x_is_temp else "O"
    x_fmt     = "%b %d" if len(df) > 14 else "%b '%y"
    base_enc  = alt.Chart(df).encode(
        x=alt.X(f"{x}:{x_type}", title="",
                axis=alt.Axis(format=x_fmt if x_is_temp else None,
                              labelAngle=-30, grid=False)),
    )
    def _series(col, color, dash=None):
        kw = {"strokeWidth": 2.5, "color": color}
        if dash:
            kw["strokeDash"] = dash
        line = base_enc.mark_line(**kw).encode(
            y=alt.Y(f"{col}:Q", title=col.replace("_", " ").title(),
                    axis=alt.Axis(format=_fmt(col), titleColor=color, labelColor=color)),
            tooltip=[
                alt.Tooltip(f"{x}:{x_type}", format=x_fmt if x_is_temp else None),
                alt.Tooltip(f"{col}:Q", format=_fmt(col)),
            ],
        )
        dot = base_enc.mark_circle(size=55, color=color, opacity=0.9).encode(y=f"{col}:Q")
        return line + dot

    chart = alt.layer(_series(c0, _PALETTE_DUAL[0]), _series(c1, _PALETTE_DUAL[1], [6, 3])
                      ).resolve_scale(y="independent")
    w = _bounded_chart_width(n_along_x=max(3, len(df)))
    return _theme(chart, height=340, width=w).interactive()


def _chart_multiline(df, x, num_cols) -> Optional[alt.Chart]:
    melted = df[[x] + num_cols].melt(id_vars=[x], var_name="metric", value_name="value")
    x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
    x_type    = "T" if x_is_temp else "O"
    any_rate  = any(_is_rate(c) for c in num_cols)
    chart = (
        alt.Chart(melted)
        .mark_line(strokeWidth=2.5, point=alt.OverlayMarkDef(size=55, filled=True))
        .encode(
            x=alt.X(f"{x}:{x_type}", title="",
                    axis=alt.Axis(labelAngle=-30, grid=False)),
            y=alt.Y("value:Q", title="", axis=alt.Axis(format=".1f" if any_rate else ",.0f")),
            color=alt.Color("metric:N",
                            scale=alt.Scale(range=_PALETTE[:len(num_cols)]),
                            legend=alt.Legend(orient="top-right", title=None)),
            tooltip=[
                alt.Tooltip(f"{x}:{x_type}"),
                alt.Tooltip("metric:N"),
                alt.Tooltip("value:Q", format=".1f" if any_rate else ",.0f"),
            ],
        )
    )
    w = _bounded_chart_width(n_along_x=max(3, int(df[x].nunique(dropna=True))))
    return _theme(chart, height=340, width=w).interactive()


def _chart_category_lines(df, x, y, cat) -> Optional[alt.Chart]:
    x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
    x_type    = "T" if x_is_temp else "O"
    n_cats    = int(df[cat].nunique(dropna=True))
    n_x       = int(df[x].nunique(dropna=True))
    palette_n = max(n_cats, 2)
    chart = (
        alt.Chart(df)
        .mark_line(
            strokeWidth=3,
            interpolate="monotone",
            point=alt.OverlayMarkDef(size=60, filled=True, stroke="white", strokeWidth=1.5),
        )
        .encode(
            x=alt.X(
                f"{x}:{x_type}",
                title="",
                axis=alt.Axis(labelAngle=0 if n_x <= 6 else -35, grid=False),
            ),
            y=alt.Y(
                f"{y}:Q",
                title=y.replace("_", " ").title(),
                axis=alt.Axis(format=_fmt(y)),
            ),
            color=alt.Color(
                f"{cat}:N",
                scale=alt.Scale(range=_PALETTE[:palette_n]),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=[
                alt.Tooltip(f"{x}:{x_type}"),
                alt.Tooltip(f"{cat}:N"),
                alt.Tooltip(f"{y}:Q", format=_fmt(y)),
            ],
        )
    )
    w = _bounded_chart_width(n_along_x=max(n_x, 3))
    return _theme(chart, height=min(420, 140 + n_cats * 8), width=w).interactive()


def _chart_two_period_bars(df, x, y) -> Optional[alt.Chart]:
    df = df.copy()
    x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
    if x_is_temp:
        try:
            df[x] = pd.to_datetime(df[x]).dt.strftime("%b %d")
        except Exception:
            pass
    order  = df[x].tolist()
    y_title = y.replace("_", " ").title()
    bars = (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
        .encode(
            x=alt.X(f"{x}:O", sort=order, title="",
                    scale=alt.Scale(paddingInner=0.3, paddingOuter=0.15)),
            y=alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=_fmt(y))),
            color=alt.Color(f"{x}:O", legend=None, scale=alt.Scale(range=_PALETTE[:2])),
            tooltip=[
                alt.Tooltip(f"{x}:O", title="Period"),
                alt.Tooltip(f"{y}:Q", title=y_title, format=_fmt(y)),
            ],
        )
    )
    labels = (
        alt.Chart(df)
        .mark_text(dy=-9, fontSize=13, fontWeight=700, color="#111827", font=_FONT)
        .encode(x=alt.X(f"{x}:O", sort=order), y=alt.Y(f"{y}:Q"),
                text=alt.Text(f"{y}:Q", format=_fmt(y)))
    )
    w = _bounded_chart_width(n_along_x=len(order))
    return _theme(bars + labels, height=340, width=w).interactive()


def _chart_bar_counts(df, x, y) -> Optional[alt.Chart]:
    """
    Vertical bars for coarse time buckets (week / month / few daily points).
    Uses week *starting* dates (Monday) on the x-axis instead of W04-style labels.
    """
    df = df.copy()
    n_pts = len(df)
    bar_band = min(48, max(26, int(540 // max(n_pts, 1))))
    x_title = ""

    x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
    if not x_is_temp:
        t_try = pd.to_datetime(df[x], errors="coerce")
        if float(t_try.notna().mean()) >= 0.7:
            df[x] = t_try
            x_is_temp = True

    if x_is_temp:
        t = pd.to_datetime(df[x], errors="coerce")
        df = df.assign(_t=t).dropna(subset=["_t"]).sort_values("_t")
        if df.empty:
            return None
        med = _median_spacing_days(df["_t"])
        name_l = str(x).lower()
        multi_year = int(df["_t"].dt.year.nunique()) > 1

        if "week" in name_l or (med is not None and 6 <= med < 27):
            starts = df["_t"].dt.to_period("W-MON").dt.start_time
            df["_x_lbl"] = (
                starts.dt.strftime("%b %d, %Y") if multi_year else starts.dt.strftime("%b %d")
            )
            x_title = "Week starting (Mon)"
        elif med is not None and med >= 27:
            starts = df["_t"].dt.to_period("M").dt.start_time
            df["_x_lbl"] = starts.dt.strftime("%b %Y") if multi_year else starts.dt.strftime("%b '%y")
        else:
            df["_x_lbl"] = (
                df["_t"].dt.strftime("%b %d, %Y")
                if multi_year or n_pts > 18
                else df["_t"].dt.strftime("%b %d")
            )

        order = df["_x_lbl"].tolist()
        x_field = "_x_lbl"
    else:
        order = df[x].astype(str).tolist()
        df["_x_lbl"] = df[x].astype(str)
        x_field = "_x_lbl"

    y_title = y.replace("_", " ").title()
    bars = (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color=_PALETTE[0])
        .encode(
            x=alt.X(
                f"{x_field}:O",
                sort=order,
                title=x_title,
                axis=alt.Axis(
                    labelAngle=-35 if n_pts > 6 else 0,
                    labelLimit=220,
                    titlePadding=12,
                ),
                scale=alt.Scale(paddingInner=0.18, paddingOuter=0.06),
            ),
            y=alt.Y(
                f"{y}:Q",
                title=y_title,
                axis=alt.Axis(format=_fmt(y), grid=True, gridColor="#e2e8f0", tickCount=6),
            ),
            tooltip=[
                alt.Tooltip(f"{x_field}:O", title="Period"),
                alt.Tooltip(f"{y}:Q", title=y_title, format=_fmt(y)),
            ],
        )
    )
    labels = (
        alt.Chart(df)
        .mark_text(dy=-8, fontSize=11, fontWeight=700, color="#1e293b", font=_FONT)
        .encode(
            x=alt.X(f"{x_field}:O", sort=order),
            y=alt.Y(f"{y}:Q"),
            text=alt.Text(f"{y}:Q", format=_fmt(y)),
        )
    )
    w = _bounded_chart_width(n_along_x=n_pts)
    chart = (bars + labels).configure_bar(discreteBandSize=bar_band)
    return _theme(chart, height=360, width=w).interactive()


def _chart_hbar(df, y_col, x_col) -> Optional[alt.Chart]:
    n       = min(len(df), 15)
    df_plot = df.head(n).copy()
    if not _is_rate(x_col):
        total = float(pd.to_numeric(df_plot[x_col], errors="coerce").fillna(0).sum())
        if total > 0:
            df_plot["_users"] = pd.to_numeric(df_plot[x_col], errors="coerce").fillna(0)
            df_plot["_pct"]   = (df_plot["_users"] * 100.0 / total).round(1)
            if n <= 8:
                return _donut(df_plot.sort_values("_pct", ascending=False), y_col, "_pct", n)
            order    = df_plot.sort_values("_users", ascending=True)[y_col].tolist()
            bar_size = max(24, min(40, 280 // max(n, 1)))
            bars = (
                alt.Chart(df_plot)
                .mark_bar(size=bar_size, cornerRadiusTopRight=6, cornerRadiusBottomRight=6)
                .encode(
                    y=alt.Y(f"{y_col}:N", sort=order, title="",
                            axis=alt.Axis(labelLimit=170, labelFontSize=12)),
                    x=alt.X("_users:Q", title="Users", axis=alt.Axis(format=",.0f")),
                    color=alt.Color(f"{y_col}:N",
                                    scale=alt.Scale(range=_PALETTE[:n]), legend=None),
                    tooltip=[
                        alt.Tooltip(f"{y_col}:N"),
                        alt.Tooltip("_users:Q", title="users", format=",.0f"),
                        alt.Tooltip("_pct:Q",   title="% share", format=".1f"),
                    ],
                )
            )
            labels = (
                alt.Chart(df_plot)
                .transform_calculate(lbl="format(datum._users, ',') + '  (' + toString(datum._pct) + '%)'")
                .mark_text(align="left", dx=8, fontSize=11, fontWeight=600,
                           color="#1f2937", font=_FONT)
                .encode(y=alt.Y(f"{y_col}:N", sort=order), x="_users:Q", text="lbl:N")
            )
            return _theme(bars + labels, height=n * (bar_size + 20))
    return _hbar_with_labels(df_plot, y_col, x_col, n)
