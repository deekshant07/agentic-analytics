"""ui/legacy_charts.py — Legacy Altair chart builder (heuristic auto-chart)."""
import pandas as pd
import altair as alt

# ── Chart palette & themes ────────────────────────────────────────────────────

_PALETTE     = ["#3b82f6", "#f97316", "#10b981", "#8b5cf6", "#ef4444", "#6366f1",
                "#06b6d4", "#f59e0b", "#84cc16", "#ec4899"]
_PALETTE_DUAL = ["#3b82f6", "#f97316"]   # blue + orange for dual-metric charts
_FONT        = "Inter, -apple-system, BlinkMacSystemFont, sans-serif"
_BG_DARK     = "#0f1117"
_GRID_DARK   = "#2a2d3a"
_LABEL_DARK  = "#94a3b8"
_AXIS_DARK   = alt.AxisConfig(
    gridColor=_GRID_DARK, domainColor=_GRID_DARK, tickColor=_GRID_DARK,
    labelColor=_LABEL_DARK, titleColor=_LABEL_DARK,
    labelFont=_FONT, titleFont=_FONT, labelFontSize=12, titleFontSize=12,
)
# keep old names so existing callers don't break
_BG        = _BG_DARK
_GRID_COL  = _GRID_DARK
_LABEL_COL = _LABEL_DARK
_AXIS_CFG  = _AXIS_DARK


def _dark(chart: alt.Chart) -> alt.Chart:
    return chart.configure(
        background=_BG_DARK,
        view=alt.ViewConfig(stroke="transparent"),
        axis=_AXIS_DARK, axisX=_AXIS_DARK, axisY=_AXIS_DARK,
        legend=alt.LegendConfig(labelColor=_LABEL_DARK, titleColor=_LABEL_DARK,
                                labelFont=_FONT, titleFont=_FONT),
        title=alt.TitleConfig(color="#e2e8f0", font=_FONT, fontSize=14),
    )


_AXIS_LIGHT = alt.AxisConfig(
    gridColor="#f1f5f9", domainColor="#e2e8f0", tickColor="#e2e8f0",
    labelColor="#374151", titleColor="#6b7280",
    labelFont=_FONT, titleFont=_FONT, labelFontSize=12, titleFontSize=12,
    gridOpacity=0.8,
)


def _light(chart: alt.Chart) -> alt.Chart:
    """Clean white theme — used for all charts."""
    return chart.configure(
        background="#ffffff",
        view=alt.ViewConfig(stroke="#e2e8f0", strokeWidth=1),
        axis=_AXIS_LIGHT, axisX=_AXIS_LIGHT, axisY=_AXIS_LIGHT,
        legend=alt.LegendConfig(
            labelColor="#374151", titleColor="#6b7280",
            labelFont=_FONT, titleFont=_FONT,
            labelFontSize=12, titleFontSize=12,
            orient="top-right",
            fillColor="white", strokeColor="#e2e8f0",
            padding=6,
        ),
        title=alt.TitleConfig(color="#111827", font=_FONT, fontSize=14,
                              fontWeight=600, anchor="start"),
    )


def _base(df) -> alt.Chart:
    return _light(alt.Chart(df)).properties(height=360)


def _is_temporal_col(df: pd.DataFrame, col: str) -> bool:
    """
    Robust temporal-column detector.
    Avoids misclassifying categorical fields like acquisition_cohort as dates.
    """
    if pd.api.types.is_datetime64_any_dtype(df[col]):
        return True
    s = df[col]
    if pd.api.types.is_numeric_dtype(s):
        return False
    # Name hint alone is not enough; require parseable values for date-like labels.
    name_has_time_hint = any(k in col.lower() for k in ("date", "week", "month", "day", "period", "year", "quarter"))
    if not name_has_time_hint:
        return False
    try:
        parsed = pd.to_datetime(s, errors="coerce")
        ratio = float(parsed.notna().mean()) if len(parsed) else 0.0
        return ratio >= 0.7
    except Exception:
        return False


def _should_show_chart(
    df: pd.DataFrame,
    inv_name: str = "",
    compact: bool = False,
) -> tuple[bool, str]:
    """
    Deterministic gate for whether a chart should be rendered.
    Prevents noisy or misleading visuals for table-like outputs.
    """
    if df is None or df.empty:
        return False, "no_data"

    cols = list(df.columns)
    if len(cols) < 2:
        return False, "too_few_columns"

    date_cols = [c for c in cols if _is_temporal_col(df, c)]
    num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    cat_cols = [c for c in cols if c not in date_cols and c not in num_cols]

    # Single-point outputs are not chartable in a useful way.
    # Use metric cards/table so users don't see one-dot charts.
    if len(df) <= 1:
        if len(num_cols) == 1:
            return False, "scalar_card"
        return False, "single_point"

    # Wide frames are usually evidence tables, not chart-friendly.
    if len(cols) > 8:
        return False, "wide_table"

    # In compact evidence cards, very long result sets are hard to read as charts.
    if compact and len(df) > 30 and not date_cols:
        return False, "compact_many_rows"

    # Unknown-table style outputs: many rows with only numeric fields.
    if not date_cols and not cat_cols and len(num_cols) >= 2 and len(df) > 16:
        return False, "numeric_matrix"

    # For categorical splits, avoid charts when cardinality is too high
    # unless we have explicit contribution/share columns.
    if cat_cols:
        cat = cat_cols[0]
        if (
            df[cat].nunique(dropna=True) > 20
            and "pct_share" not in cols
            and "pct_change" not in cols
            and "pct_contrib" not in cols
        ):
            return False, "high_cardinality_category"

    # Contribution tables are chartable only when the contribution columns exist.
    if inv_name.startswith("driver_contribution_"):
        needed = {"abs_contrib", "pct_contrib"}
        if not needed.issubset(set(cols)):
            return False, "invalid_contribution_shape"

    return True, "chart_ok"


def auto_chart(df: pd.DataFrame):  # noqa: C901
    """
    Chart type decision tree:
      1. Funnel           → vertical bar
      2. Single scalar    → None (metric card)
      3. pct_change + cat → diverging horizontal bar (green/red)
      4. pct_share + cat  → horizontal bar with "N (X%)" labels
      5. Time series      → line/area (continuous) or bar (coarse/few points)
           • multi-metric (≥2 numeric, no cat) → multi-line
           • single metric + category          → multi-line per category
           • single metric, rate/ratio         → always line
           • single metric, coarse/few points  → vertical bar
      6. Period comparison (current/previous cols) → grouped horizontal bar
      7. cat + num fallback                   → horizontal bar with labels

    All charts use the bright _light() theme.
    """
    if df.empty or len(df.columns) < 2:
        return None
    try:
        # ── Column classification ────────────────────────────────────────────
        _DATE_KW = ("date", "week", "month", "day", "period", "quarter", "year")
        _RATE_KW = ("rate", "pct", "ratio", "avg", "mean", "per_user",
                    "conversion", "retention", "churn", "score", "share")

        date_cols = [c for c in df.columns if _is_temporal_col(df, c)]
        num_cols  = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        cat_cols  = [c for c in df.columns if c not in date_cols and c not in num_cols]
        # Prefer semantic rate columns over raw count helpers when plotting.
        if "status_rate_pct" in num_cols:
            num_cols = ["status_rate_pct"] + [c for c in num_cols if c != "status_rate_pct"]

        for dc in date_cols:
            try:
                df[dc] = pd.to_datetime(df[dc])
            except Exception:
                pass

        def _is_rate_col(c: str) -> bool:
            return any(k in c.lower() for k in _RATE_KW)

        def _single_period(date_col: str) -> bool:
            try:
                return df[date_col].nunique(dropna=True) <= 1
            except Exception:
                return len(df) <= 1

        def _hbar_with_labels(df_plot, y_col, x_col, n):
            """Reusable bright horizontal bar + inline label."""
            order    = df_plot.sort_values(x_col, ascending=True)[y_col].tolist()
            bar_size = max(24, min(44, 300 // max(n, 1)))
            is_pct   = _is_rate_col(x_col)
            fmt      = ".1f" if is_pct else ",.0f"
            bars = (
                alt.Chart(df_plot)
                .mark_bar(size=bar_size, cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
                .encode(
                    y=alt.Y(f"{y_col}:N", sort=order, title="",
                            axis=alt.Axis(labelLimit=180, labelFontSize=12)),
                    x=alt.X(f"{x_col}:Q", title=x_col,
                            axis=alt.Axis(format=fmt)),
                    color=alt.Color(f"{y_col}:N",
                                    scale=alt.Scale(range=_PALETTE[:n]), legend=None),
                    tooltip=[alt.Tooltip(f"{y_col}:N"),
                             alt.Tooltip(f"{x_col}:Q", format=fmt)],
                )
            )
            labels = (
                alt.Chart(df_plot)
                .mark_text(align="left", dx=5, fontSize=11, fontWeight=600,
                           color="#1f2937", font=_FONT)
                .encode(
                    y=alt.Y(f"{y_col}:N", sort=order),
                    x=alt.X(f"{x_col}:Q"),
                    text=alt.Text(f"{x_col}:Q", format=fmt),
                )
            )
            return _light(bars + labels).properties(height=n * (bar_size + 20))

        def _donut_share_chart(df_plot, cat_col, val_col, n):
            """Donut chart for single-period share splits."""
            inner_r = 55
            outer_r = 115
            pie = (
                alt.Chart(df_plot)
                .mark_arc(innerRadius=inner_r, outerRadius=outer_r)
                .encode(
                    theta=alt.Theta(f"{val_col}:Q"),
                    color=alt.Color(
                        f"{cat_col}:N",
                        scale=alt.Scale(range=_PALETTE[:n]),
                        legend=alt.Legend(orient="right", title=cat_col),
                    ),
                    tooltip=[
                        alt.Tooltip(f"{cat_col}:N"),
                        alt.Tooltip("users:Q", title="users", format=",")
                        if "users" in df_plot.columns
                        else alt.Tooltip(f"{val_col}:Q", format=".1f"),
                        alt.Tooltip(f"{val_col}:Q", title="% share", format=".1f"),
                    ],
                )
            )
            center = (
                alt.Chart(pd.DataFrame({"t": ["Share"]}))
                .mark_text(font=_FONT, fontSize=15, fontWeight=700, color="#111827")
                .encode(text="t:N")
            )
            return _light((pie + center).properties(width=420, height=320)).interactive()

        # ── 1. Funnel ────────────────────────────────────────────────────────
        if "step_name" in df.columns and "users" in df.columns:
            order = (df.sort_values("step_num")["step_name"].tolist()
                     if "step_num" in df.columns else df["step_name"].tolist())
            n = len(df)
            bar_size = max(32, min(52, 360 // max(n, 1)))
            bars = (
                alt.Chart(df)
                .mark_bar(size=bar_size, cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
                .encode(
                    y=alt.Y("step_name:N", sort=order, title="",
                            axis=alt.Axis(labelLimit=160, labelFontSize=12)),
                    x=alt.X("users:Q", title="Users", axis=alt.Axis(format=",.0f")),
                    color=alt.Color("step_name:N",
                                    scale=alt.Scale(range=_PALETTE[:n]), legend=None),
                    tooltip=[
                        alt.Tooltip("step_name:N"),
                        alt.Tooltip("users:Q", format=",.0f"),
                        *(  [alt.Tooltip("pct_of_top:Q", title="% of top", format=".1f")]
                            if "pct_of_top" in df.columns else [] ),
                    ],
                )
            )
            labels = (
                alt.Chart(df)
                .mark_text(align="left", dx=5, fontSize=11, fontWeight=600,
                           color="#1f2937", font=_FONT)
                .encode(
                    y=alt.Y("step_name:N", sort=order),
                    x=alt.X("users:Q"),
                    text=alt.Text("users:Q", format=","),
                )
            )
            return _light(bars + labels).properties(height=n * (bar_size + 20))

        # ── 2. Single scalar → handled as metric card ────────────────────────
        if len(df) == 1 and num_cols and not date_cols:
            return None

        # ── 3. pct_change diverging bar ──────────────────────────────────────
        if "pct_change" in df.columns and cat_cols:
            cat     = cat_cols[0]
            df_plot = df.dropna(subset=["pct_change"]).sort_values("pct_change")
            if not df_plot.empty:
                tooltip_extra = []
                if "curr_n" in df_plot.columns:
                    tooltip_extra = [
                        alt.Tooltip("curr_n:Q", title="current",  format=","),
                        alt.Tooltip("prev_n:Q", title="previous", format=","),
                    ]
                n = len(df_plot)
                bar_size = max(24, min(40, 280 // max(n, 1)))
                _bar = (
                    alt.Chart(df_plot)
                    .mark_bar(size=bar_size,
                              cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
                    .encode(
                        y=alt.Y(f"{cat}:N", sort=df_plot[cat].tolist(), title="",
                                axis=alt.Axis(labelLimit=130, labelFontSize=11)),
                        x=alt.X("pct_change:Q", title="% change",
                                axis=alt.Axis(format="+.1f")),
                        color=alt.condition(
                            alt.datum.pct_change >= 0,
                            alt.value("#10b981"),  # green = growth
                            alt.value("#ef4444"),  # red   = decline
                        ),
                        tooltip=[
                            alt.Tooltip(f"{cat}:N"),
                            alt.Tooltip("pct_change:Q", title="% change", format="+.1f"),
                            *tooltip_extra,
                        ],
                    )
                )
                return _light(_bar).properties(height=n * (bar_size + 20)).interactive()

        # ── 4. Distribution: cat + users + pct_share ────────────────────────
        if "pct_share" in df.columns and "users" in df.columns and cat_cols:
            cat      = cat_cols[0]
            n        = min(len(df), 10)
            df_plot  = df.head(n).sort_values("users", ascending=True).copy()
            # Single-period category split looks cleaner as donut (few buckets).
            # For many categories, keep bar chart for readability.
            pct_sum = float(df_plot["pct_share"].fillna(0).sum()) if "pct_share" in df_plot.columns else 0.0
            looks_like_share = 90 <= pct_sum <= 110
            if n <= 8 and not date_cols and looks_like_share:
                return _donut_share_chart(df_plot.sort_values("pct_share", ascending=False), cat, "pct_share", n)
            order    = df_plot[cat].tolist()
            bar_size = max(28, min(44, 300 // max(n, 1)))
            bars = (
                alt.Chart(df_plot)
                .mark_bar(size=bar_size,
                          cornerRadiusTopRight=6, cornerRadiusBottomRight=6)
                .encode(
                    y=alt.Y(f"{cat}:N", sort=order, title="",
                            axis=alt.Axis(labelLimit=180, labelFontSize=12)),
                    x=alt.X("users:Q", title="users", axis=alt.Axis(format=",")),
                    color=alt.Color(f"{cat}:N",
                                    scale=alt.Scale(range=_PALETTE[:n]), legend=None),
                    tooltip=[
                        alt.Tooltip(f"{cat}:N"),
                        alt.Tooltip("users:Q",     title="users",   format=",.0f"),
                        alt.Tooltip("pct_share:Q", title="% share", format=".1f"),
                    ],
                )
            )
            labels = (
                alt.Chart(df_plot)
                .transform_calculate(
                    lbl="format(datum.users, ',') + '  (' + toString(datum.pct_share) + '%)'",
                )
                .mark_text(align="left", dx=8, fontSize=11, fontWeight=600,
                           color="#1f2937", font=_FONT)
                .encode(
                    y=alt.Y(f"{cat}:N", sort=order),
                    x=alt.X("users:Q"),
                    text=alt.Text("lbl:N"),
                )
            )
            return _light(bars + labels).properties(height=n * (bar_size + 20))

        # ── 5. Time series ───────────────────────────────────────────────────
        if date_cols and num_cols:
            x       = date_cols[0]
            n_pts   = len(df)
            def _infer_temporal_grain(col: str) -> str:
                # Infer grain from actual spacing to avoid relying only on column names.
                # Example: DATE_TRUNC('month', ...) AS date should still be treated as monthly.
                try:
                    s = pd.to_datetime(df[col], errors="coerce").dropna().sort_values()
                    if len(s) < 2:
                        return "day"
                    deltas = s.diff().dropna().dt.days
                    if deltas.empty:
                        return "day"
                    med = float(deltas.median())
                    if med >= 27:
                        return "month"
                    if med >= 6:
                        return "week"
                    return "day"
                except Exception:
                    return "day"

            inferred_grain = _infer_temporal_grain(x)
            is_daily   = inferred_grain == "day"
            is_coarse  = inferred_grain in {"month", "week"} \
                         or any(k in x.lower() for k in ("month", "week", "quarter", "year")) \
                         or (n_pts <= 16 and not is_daily)
            any_rate   = any(_is_rate_col(c) for c in num_cols)

            # ── 5a. Multiple numeric cols (no category) → dual-axis or multi-line ──
            if len(num_cols) >= 2 and not cat_cols:
                n_lines   = len(num_cols)
                x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
                x_type    = "T" if x_is_temp else "O"
                x_fmt     = ("%b %d"  if is_daily else
                             "%b '%y" if "week" in x.lower() else
                             "%b '%y")
                x_ax      = alt.Axis(
                    labelAngle=-30 if (is_daily or n_pts > 12) else 0,
                    format=x_fmt if x_is_temp else None,
                    grid=False,
                )
                # Dual-axis for 2 metrics with very different scales (e.g. revenue + count)
                if n_lines == 2:
                    c0, c1 = num_cols[0], num_cols[1]
                    fmt0 = ".1f" if _is_rate_col(c0) else ",.0f"
                    fmt1 = ".1f" if _is_rate_col(c1) else ",.0f"
                    col0_label = c0.replace("_", " ").title()
                    col1_label = c1.replace("_", " ").title()
                    base_enc = alt.Chart(df).encode(
                        x=alt.X(f"{x}:{x_type}", axis=x_ax, title=""),
                    )
                    line0 = base_enc.mark_line(
                        strokeWidth=2.5, color=_PALETTE_DUAL[0],
                    ).encode(
                        y=alt.Y(f"{c0}:Q", title=col0_label,
                                axis=alt.Axis(format=fmt0, titleColor=_PALETTE_DUAL[0],
                                              labelColor=_PALETTE_DUAL[0])),
                        tooltip=[
                            alt.Tooltip(f"{x}:{x_type}", format=x_fmt if x_is_temp else None),
                            alt.Tooltip(f"{c0}:Q", title=col0_label, format=fmt0),
                        ],
                    )
                    dot0 = base_enc.mark_circle(
                        size=55, color=_PALETTE_DUAL[0], opacity=0.9,
                    ).encode(
                        y=alt.Y(f"{c0}:Q"),
                    )
                    line1 = base_enc.mark_line(
                        strokeWidth=2.5, color=_PALETTE_DUAL[1], strokeDash=[6, 3],
                    ).encode(
                        y=alt.Y(f"{c1}:Q", title=col1_label,
                                axis=alt.Axis(format=fmt1, titleColor=_PALETTE_DUAL[1],
                                              labelColor=_PALETTE_DUAL[1])),
                        tooltip=[
                            alt.Tooltip(f"{x}:{x_type}", format=x_fmt if x_is_temp else None),
                            alt.Tooltip(f"{c1}:Q", title=col1_label, format=fmt1),
                        ],
                    )
                    dot1 = base_enc.mark_circle(
                        size=55, color=_PALETTE_DUAL[1], opacity=0.9,
                    ).encode(
                        y=alt.Y(f"{c1}:Q"),
                    )
                    chart = alt.layer(line0 + dot0, line1 + dot1).resolve_scale(y="independent")
                    return _light(chart).properties(height=360).interactive()

                # 3+ metrics → melted multi-line
                melted = df[[x] + num_cols].copy()
                try:
                    melted = melted.melt(id_vars=[x], var_name="metric", value_name="value")
                except Exception:
                    melted = None

                if melted is not None and not melted.empty:
                    v_fmt = ".1f" if any_rate else ",.0f"
                    _ml = (
                        alt.Chart(melted)
                        .mark_line(strokeWidth=2.5,
                                   point=alt.OverlayMarkDef(size=55, filled=True))
                        .encode(
                            x=alt.X(f"{x}:{x_type}", axis=x_ax, title=""),
                            y=alt.Y("value:Q", title="",
                                    axis=alt.Axis(format=v_fmt)),
                            color=alt.Color("metric:N",
                                            scale=alt.Scale(range=_PALETTE[:n_lines]),
                                            legend=alt.Legend(orient="top-right",
                                                              title=None)),
                            tooltip=[
                                alt.Tooltip(f"{x}:{x_type}",
                                            format=x_fmt if x_is_temp else None),
                                alt.Tooltip("metric:N"),
                                alt.Tooltip("value:Q", format=v_fmt),
                            ],
                        )
                    )
                    return _light(_ml).properties(height=360).interactive()

            # ── 5b. Single numeric + category → multi-line per category ──
            if cat_cols and len(num_cols) >= 1:
                c, y    = cat_cols[0], num_cols[0]
                # If there is only one time bucket (e.g. "Feb 2026"), a line chart
                # is visually misleading. Show a categorical bar split instead.
                if _single_period(x):
                    dfg = (
                        df.groupby(c, as_index=False)[y]
                        .sum(numeric_only=True)
                        .sort_values(y, ascending=True)
                    )
                    n = min(len(dfg), 15)
                    if n > 0:
                        return _hbar_with_labels(dfg.tail(n), c, y, n)
                n_cats  = df[c].nunique()
                x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
                x_type  = "T" if x_is_temp else "O"
                x_fmt   = ("%b %d"  if is_daily else
                           "%b '%y" if "week" in x.lower() else
                           "%b '%y")
                v_fmt   = ".1f" if _is_rate_col(y) else ",.0f"
                _mc = (
                    alt.Chart(df)
                    .mark_line(strokeWidth=2.5,
                               point=alt.OverlayMarkDef(size=50, filled=True))
                    .encode(
                        x=alt.X(f"{x}:{x_type}", title="",
                                axis=alt.Axis(
                                    labelAngle=-30 if is_daily else 0,
                                    format=x_fmt if x_is_temp else None,
                                )),
                        y=alt.Y(f"{y}:Q", title=y,
                                axis=alt.Axis(format=v_fmt)),
                        color=alt.Color(f"{c}:N",
                                        scale=alt.Scale(range=_PALETTE[:n_cats]),
                                        legend=alt.Legend(orient="bottom")),
                        tooltip=[
                            alt.Tooltip(f"{x}:{x_type}",
                                        format=x_fmt if x_is_temp else None),
                            alt.Tooltip(f"{c}:N"),
                            alt.Tooltip(f"{y}:Q", format=v_fmt),
                        ],
                    )
                )
                return _light(_mc).properties(height=320).interactive()

            # ── 5c. Single numeric, no category ─────────────────────────
            y     = num_cols[0]
            v_fmt = ".1f" if _is_rate_col(y) else ",.0f"

            if n_pts <= 2 and not any_rate:
                # Very short trends — wide bars, clear comparison.
                dfg = df.copy()
                x_is_temp = pd.api.types.is_datetime64_any_dtype(dfg[x])
                x_fmt = "%b %d" if is_daily else "%b '%y"
                if x_is_temp:
                    try:
                        dfg[x] = pd.to_datetime(dfg[x]).dt.strftime(x_fmt)
                    except Exception:
                        pass
                x_order = dfg[x].tolist()
                y_title = y.replace("_", " ").title()
                bars = (
                    alt.Chart(dfg)
                    .mark_bar(
                        cornerRadiusTopLeft=6,
                        cornerRadiusTopRight=6,
                        opacity=1.0,
                    )
                    .encode(
                        x=alt.X(f"{x}:O", sort=x_order, title="",
                                scale=alt.Scale(paddingInner=0.3, paddingOuter=0.15)),
                        y=alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=v_fmt)),
                        color=alt.Color(f"{x}:O", legend=None,
                                        scale=alt.Scale(range=_PALETTE[:2])),
                        tooltip=[
                            alt.Tooltip(f"{x}:O", title="Period"),
                            alt.Tooltip(f"{y}:Q", title=y_title, format=v_fmt),
                        ],
                    )
                )
                labels = (
                    alt.Chart(dfg)
                    .mark_text(dy=-9, fontSize=13, fontWeight=700, color="#111827", font=_FONT)
                    .encode(x=alt.X(f"{x}:O", sort=x_order), y=alt.Y(f"{y}:Q"),
                            text=alt.Text(f"{y}:Q", format=v_fmt))
                )
                return _light(bars + labels).properties(height=360).interactive()

            if is_coarse and not any_rate:
                # Vertical bar for count-type monthly/weekly data
                if "week" in x.lower():
                    x_fmt = "W%W '%y"
                elif n_pts <= 7:
                    x_fmt = "%b %d"
                else:
                    x_fmt = "%b '%y"
                df = df.copy()
                try:
                    df[x] = pd.to_datetime(df[x]).dt.strftime(x_fmt)
                except Exception:
                    pass
                x_order = df[x].tolist()
                y_title = y.replace("_", " ").title()
                # Use band-based sizing (paddingInner controls gap between bars)
                # so bars fill the chart width regardless of container size.
                bars = (
                    alt.Chart(df)
                    .mark_bar(
                        cornerRadiusTopLeft=4,
                        cornerRadiusTopRight=4,
                        opacity=1.0,
                        color=_PALETTE[0],
                    )
                    .encode(
                        x=alt.X(f"{x}:O", sort=x_order, title="",
                                axis=alt.Axis(
                                    labelAngle=-30 if n_pts > 8 else 0,
                                    labelFontSize=12,
                                    labelFontWeight=500,
                                    ticks=False,
                                    domainColor="#e2e8f0",
                                ),
                                scale=alt.Scale(paddingInner=0.25, paddingOuter=0.08)),
                        y=alt.Y(f"{y}:Q", title=y_title,
                                axis=alt.Axis(format=v_fmt, grid=True,
                                              gridColor="#f1f5f9", tickCount=5)),
                        tooltip=[
                            alt.Tooltip(f"{x}:O", title="Period"),
                            alt.Tooltip(f"{y}:Q", title=y_title, format=v_fmt),
                        ],
                    )
                )
                labels = (
                    alt.Chart(df)
                    .mark_text(dy=-7, fontSize=11, fontWeight=600,
                               color="#374151", font=_FONT)
                    .encode(
                        x=alt.X(f"{x}:O", sort=x_order),
                        y=alt.Y(f"{y}:Q"),
                        text=alt.Text(f"{y}:Q", format=v_fmt),
                    )
                )
                return _light(bars + labels).properties(height=360).interactive()

            else:
                # Area + line for daily / rate data
                x_is_temp = pd.api.types.is_datetime64_any_dtype(df[x])
                x_type    = "T" if x_is_temp else "O"
                x_fmt_ax  = "%b %d" if is_daily else "%b '%y"
                y_title   = y.replace("_", " ").title()
                base = alt.Chart(df).encode(
                    x=alt.X(f"{x}:{x_type}", title="",
                            axis=alt.Axis(
                                labelAngle=-30 if (is_daily or n_pts > 16) else 0,
                                format=x_fmt_ax if x_is_temp else None,
                                grid=False,
                            )),
                    tooltip=[
                        alt.Tooltip(f"{x}:{x_type}",
                                    format=x_fmt_ax if x_is_temp else None),
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
                    opacity=0.15, line=False,
                ).encode(y=alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=v_fmt)))
                line = base.mark_line(
                    color=_PALETTE[0], strokeWidth=2.5,
                ).encode(y=alt.Y(f"{y}:Q"))
                dots = base.mark_circle(
                    color=_PALETTE[0], size=55, opacity=0.95,
                ).encode(y=alt.Y(f"{y}:Q"))
                return _light(area + line + dots).properties(height=360).interactive()

        # ── 6. Period comparison: curr + prev columns → grouped horizontal bar
        _CURR_KW = ("current", "curr", "this_period", "period_2")
        _PREV_KW = ("previous", "prev", "last_period", "prior", "period_1")
        curr_cols = [c for c in num_cols if any(k in c.lower() for k in _CURR_KW)]
        prev_cols = [c for c in num_cols if any(k in c.lower() for k in _PREV_KW)]
        if curr_cols and prev_cols and cat_cols:
            cat   = cat_cols[0]
            curr  = curr_cols[0]
            prev  = prev_cols[0]
            melted = df[[cat, curr, prev]].melt(
                id_vars=cat, var_name="period", value_name="value"
            )
            n        = len(df)
            is_rate  = _is_rate_col(curr)
            v_fmt    = ".1f" if is_rate else ",.0f"
            bar_size = max(20, min(36, 260 // max(n, 1)))
            _gc = (
                alt.Chart(melted)
                .mark_bar(size=bar_size,
                          cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
                .encode(
                    y=alt.Y(f"{cat}:N", title="",
                            axis=alt.Axis(labelLimit=150, labelFontSize=11)),
                    x=alt.X("value:Q", title="",
                            axis=alt.Axis(format=v_fmt)),
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
            return _light(_gc).properties(height=max(200, n * (bar_size * 2 + 24))).interactive()

        # ── 7. cat + num fallback → horizontal bar with labels ───────────────
        if cat_cols and num_cols:
            x_col   = num_cols[0]
            y_col   = cat_cols[0]
            n       = min(len(df), 15)
            df_plot = df.head(n).copy()
            # Amplitude-like split view: compute percentage share for categorical counts.
            if not _is_rate_col(x_col):
                total = float(pd.to_numeric(df_plot[x_col], errors="coerce").fillna(0).sum())
                if total > 0:
                    df_plot["users"] = pd.to_numeric(df_plot[x_col], errors="coerce").fillna(0)
                    df_plot["pct_share"] = (df_plot["users"] * 100.0 / total).round(1)
                    # Small category sets read better as donut only when top-N
                    # captures most of the distribution (avoid misleading pies).
                    topn_cov = float(df_plot["pct_share"].sum())
                    if n <= 8 and not date_cols and topn_cov >= 90:
                        return _donut_share_chart(
                            df_plot.sort_values("pct_share", ascending=False),
                            y_col,
                            "pct_share",
                            n,
                        )
                    order = df_plot.sort_values("users", ascending=True)[y_col].tolist()
                    bar_size = max(24, min(40, 280 // max(n, 1)))
                    bars = (
                        alt.Chart(df_plot)
                        .mark_bar(size=bar_size, cornerRadiusTopRight=6, cornerRadiusBottomRight=6)
                        .encode(
                            y=alt.Y(f"{y_col}:N", sort=order, title="",
                                    axis=alt.Axis(labelLimit=170, labelFontSize=12)),
                            x=alt.X("users:Q", title="Users", axis=alt.Axis(format=",.0f")),
                            color=alt.Color(f"{y_col}:N", scale=alt.Scale(range=_PALETTE[:n]), legend=None),
                            tooltip=[
                                alt.Tooltip(f"{y_col}:N"),
                                alt.Tooltip("users:Q", title="users", format=",.0f"),
                                alt.Tooltip("pct_share:Q", title="% share", format=".1f"),
                            ],
                        )
                    )
                    labels = (
                        alt.Chart(df_plot)
                        .transform_calculate(
                            lbl="format(datum.users, ',') + '  (' + toString(datum.pct_share) + '%)'",
                        )
                        .mark_text(align="left", dx=8, fontSize=11, fontWeight=600,
                                   color="#1f2937", font=_FONT)
                        .encode(
                            y=alt.Y(f"{y_col}:N", sort=order),
                            x=alt.X("users:Q"),
                            text=alt.Text("lbl:N"),
                        )
                    )
                    return _light(bars + labels).properties(height=n * (bar_size + 20))
            return _hbar_with_labels(df_plot, y_col, x_col, n)

    except Exception:
        pass
    return None

