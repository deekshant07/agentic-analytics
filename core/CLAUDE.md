# core/ — Pipeline Deep Reference

## Preferred fix patterns (scalable)

1. **Orchestrator / prompt** — `build_vocab()` (`pipeline/catalog_vocab.py`) merges catalog `value_meanings` + DB samples into DIMENSION VALUE HINTS. Thin rescue in `orchestrate()` if LLM still returns clarify/out_of_scope.
2. **Catalog custom events** — define the business concept in `builder_definition` (groups, filters, IS NOT NULL / IS NULL). Compiler and fixups already read this.
3. **Sentinels** — `__IS_NOT_NULL__` / `__IS_NULL__` in `qo.filters` when the user phrase matches a CE concept, not a sampled literal. Post-orchestration: `_remap_invalid_filter_values_via_custom_events()` in `ui/qo_fixups.py`.
4. **Metric definitions** — `suggested_metrics[].builder_definition`, `sql_hint`, `status`; hydrate via `_inherit_metric_status_and_filters`, `_apply_lineage_rollforward_filters`.
5. **Naming conventions** — prefer column/event patterns (`*_status`, `*_channel`) in `semantic/resolver_policy.py` and `analysis/diagnose.py`.
6. **Global guards** — test-user exclusion → `always_filter` in catalog, not a one-off `WHERE` in a compiler.

## Pipeline Architecture

### Layer contracts

| Layer | Input | Output | LLM? |
|---|---|---|---|
| `orchestrator.py · orchestrate()` | prompt + `build_vocab()` vocab | `QueryObject` (slots only, no SQL) | Yes (once) |
| `ui/qo_fixups.py` (8 passes) | raw `QueryObject` | normalized `QueryObject` | No |
| `compilers.py · compile_query()` | validated `QueryObject` + metrics | DuckDB SQL string | No |
| `analyst.py · investigate()` | `QueryObject` + db_path | `AnalystReport` (multi-SQL + narrative) | Yes |
| `viz/charts_plotly.py` | DataFrame | Plotly Figure | No |

**Special compiler return values:** `"__diagnose__"` → diagnose pipeline; `"__analyst__"` → `analyst.investigate()`; `""` → clarify message.

### Known bug classes (fixed; don't regress)

- **Ambiguous JOIN column refs** — `_guards_clause(alias)` qualifies guard fragments; always pass `alias=` inside JOINs.
- **Invalid SQL aliases starting with digits** — `_safe_col()` strips non-`[a-z0-9_]` but doesn't prepend `_`; metric IDs must not start with a digit.
- **Missing time column** — scalar-metric compilers that skip `DATE_TRUNC` return frames with no temporal column; chart layer then fails.
- **Sentinel not remapped** — `_remap_invalid_filter_values_via_custom_events()` catches LLM literal values not in sampled data; fully catalog-driven.

### Conventions

- `set_global_sql_guards()` must be called before any `compile_query()`; guards from `catalog["__business_context__"]["exclusions"]["always_filter"]`.
- All filter rendering through `_render_filter_part()` — never concatenate raw values into SQL strings.
- `_safe_col()` must wrap every externally-sourced column name before SQL interpolation.
- Compiler dispatch order: `% of users` builder_type → `metric.get("sql")` → analysis_type. Do not reorder.

## QueryObject (`sql/query_object.py`)

```python
analysis_type      # "metric"|"segment"|"funnel"|"retention"|"behavioral_cohort"|
                   # "diagnose"|"same_month_anchor"|"user_lifecycle"|"stickiness"|
                   # "journey"|"xyz_matrix"|"forecast"|"clarify"|"out_of_scope"|...
event              # primary event name
event_b            # return/anchor event (retention, lifecycle)
metric_id          # pre-built metric id — bypasses event-based compiler path
filters            # {col: value} — equality OR sentinel strings
filter_excludes    # {col: value|[values]} — NOT IN / !=
breakdown          # GROUP BY column
time_range_days    # lookback days; ignored when date_from/date_to are set
date_from/date_to  # absolute YYYY-MM-DD; takes priority over time_range_days
time_granularity   # "day" | "week" | "month"
funnel_steps       # ordered event list
```

## IS NOT NULL / IS NULL sentinels

| Sentinel | SQL rendered |
|---|---|
| `"__IS_NOT_NULL__"` | `col IS NOT NULL` |
| `"__IS_NULL__"` | `col IS NULL` |

Set by: (1) orchestrator LLM via `CUSTOM EVENT COLUMN CONDITIONS` block in prompt, (2) `_remap_invalid_filter_values_via_custom_events()` post-orchestration. Display text via `_translate_sentinel()` in `ui/cohort_labels.py`; update `_SENTINEL_BY_COL` for new domains.

## Pipeline flow (`ui/pipeline.py · get_sql()`)

```
orchestrate(prompt, catalog, sampled, history)
    build_vocab() → vocab; LLM → QueryObject
    _resolve_qualified_prebuilt_metric  # clarify rescue via catalog

_maybe_resolve_clarify_as_followup(qo, history)
    # short modifier phrase → inherit prior QO context, change analysis_type

normalize_query_object(qo, ...)   # calls full fixup chain:
    _apply_time_intent_overrides, _hydrate_retention_event_from_metric,
    _hydrate_primary_event_for_action_types, _apply_metric_variant_overrides,
    _sanitize_invalid_breakdown, _remap_invalid_filter_values_via_custom_events,
    _apply_followup_context_repair, _inherit_metric_status_and_filters,
    _apply_same_month_anchor_composite_metric, _apply_lineage_rollforward_filters,
    _materialize_metric_status_into_filters_for_equality_cohorts,
    _strip_redundant_calendar_day_filter

resolve_query_policy() → "custom_split"|"custom_segment"|"custom_single"|"custom_retention"|"orchestrator"
compile_query(qo, metrics) → (sql, metric_name)
run_sql(sql) → DataFrame
```

## Compiler routing (`sql/compilers.py`)

Dispatch order in `compile_query()`:
1. `% of users` / `pct of users` in `builder_type` → `_compile_pct_users_metric_*` (two CTEs: `denom_cohort` + `numer_cohort`, same time window)
2. `metric.get("sql")` set → pre-built SQL path
3. `analysis_type` dispatch → one `_compile_*` per type

Custom event routes (from resolver, before `compile_query`):
`compile_custom_event_single/split/segment/retention`

## Resolved query semantics

`core/pipeline/normalize_query.py` → `attach_query_semantics()` → `qo._query_semantics`

| Consumer | Reads |
|---|---|
| `_compile_retention()` | `RetentionTemplate` → mom_nday / weekly_nday / period_matrix |
| `core/semantic/presentation.py` | `primary_metric_column`, `value_kind` → chart column + `%` |
| `ui/debug_panel.py` | `query_semantics` in metric contract |

Tests: `pytest qa/test_query_semantics.py -v`
