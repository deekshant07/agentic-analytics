# ui/ — Frontend & Chart Reference

## Chart routing (`core/viz/charts_plotly.py`)

Entry: `evidence_chart_plotly(inv_name, df, chart_title=...)`

Specialized routes first (lifecycle, survival curves, sankey, power user, funnel drilldown), then `_plotly_auto(df)`:

| DataFrame shape | Chart |
|---|---|
| `date + cat + num` (coarse / monthly) | 100% stacked bar (`barnorm="percent"`) |
| `date + num` | Time series line |
| `cat + num` (non-rate, n > 1) | Donut — top `_DONUT_MAX_SLICES` (=8) + "Others" |
| `cat + num` (rate metric or n=1) | Horizontal bar |

Altair (`charts.py`) is the fallback for heatmaps, waterfall, and other specialized visuals.

## LLM tiers (`core/infra/llm.py`)

Exports `LLM_STRONG`, `LLM_MEDIUM`, `LLM_FAST`. Set `LLM_PROVIDER` in `.env`.

Supported providers: `groq`, `cerebras`, `sambanova`, `grok`, `gemini`, `openai`, `ollama` (all OpenAI-compatible API).

Per-component overrides: `LLM_STRONG_MODEL`, `LLM_MEDIUM_MODEL`, `LLM_FAST_MODEL`.

## Key files

- `pipeline.py` — `get_sql()` main pipeline: orchestrate → fixups → compile → run
- `qo_fixups.py` — all post-orchestration `QueryObject` mutation functions
- `chart_display.py` — Streamlit wrapper: Plotly first, Altair fallback
- `cohort_labels.py` — human-readable labels; update `_SENTINEL_BY_COL` for new domains
- `debug_panel.py` — debug sidebar
