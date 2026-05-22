# Analytics Agent — Codebase Reference

## Project Context

- Text-to-SQL analytics agent: orchestrator → compiler → chart layers
- Key modules: `core/pipeline/orchestrator.py`, `core/sql/compilers.py`, `chat.py`, `core/analysis/diagnose.py`
- Avoid Jupiter-specific examples in prompts; keep generic
- Chart routing: donut for ≤N categories, bar otherwise — confirm threshold before changing

## Work Style

- Prefer small, incremental changes with verification over large refactors in one shot
- When asked for a plan or HLD, deliver the plan/diagram FIRST before deep code exploration
- For multi-file refactors, write out the full step list upfront so work can resume across sessions

## Testing & Verification

- Always add/run regression tests after fixing pipeline bugs (orchestrator, compiler, chart layers)
- After SQL/compiler fixes, verify with a smoke test — past fixes had follow-on bugs (ambiguous JOINs, invalid aliases starting with digits, missing time columns)
- Watch for false positives in column/keyword detection (e.g. suffix matches like `ifsc_code`, `merchant_category_code`)

## What this is

A **multi-industry** natural-language analytics agent (fintech, SaaS, healthtech, ecommerce, edtech, etc.). Users type questions in plain English; the system orchestrates SQL against DuckDB and returns charts + narrative. Built with Streamlit.

**One codebase, many domains.** Swapping `catalog.json` + the `.duckdb` file changes the domain entirely. Python in `core/` and `ui/` must stay domain-neutral.

## Industry-agnostic rules (mandatory)

**Domain knowledge belongs in `catalog.json`, not in Python.** Before fixing a failure, ask: *If we swap `catalog.json` for SaaS or healthcare, does this change still make sense without editing Python?* If no — move it to the catalog.

Anti-patterns to avoid:
- Hardcoding column/event names or literal filter values in `core/`, `ui/qo_fixups.py`, or `compilers.py`
- Baking `sampled_values` into compiler SQL (samples are hints for the LLM, not config)
- Injecting `always_filter` SQL in code — use `catalog["__business_context__"]["exclusions"]["always_filter"]` + `set_global_sql_guards()`

Preferred fix patterns → see `core/CLAUDE.md` for full detail.

## Running

```bash
uv run streamlit run chat.py            # main UI
uv run streamlit run catalog_editor.py  # catalog / semantic layer editor
pytest qa/regression.py -q             # fast regression suite (no DB needed for L1/L2)
pytest qa/regression.py -m db          # L3 DB execution tests (needs the .duckdb file)
pytest qa/regression.py -m "not db" -q # L1+L2 only
```

## Project layout

```
chat.py          Streamlit main loop
catalog.json     Single source of truth: schemas, metrics, custom events, business context
*.duckdb         DuckDB analytics database (jupiter.duckdb by default)
core/            Pipeline engine — orchestrator, compilers, charts, analysis (see core/CLAUDE.md)
ui/              Streamlit wrappers — pipeline, fixups, chart display (see ui/CLAUDE.md)
qa/              Regression + eval suite (see qa/CLAUDE.md)
semantic-layer/  Catalog generation tools (see semantic-layer/CLAUDE.md)
playbooks/       Domain-specific investigation templates (YAML)
```

## Catalog (`catalog.json`)

```
catalog["<table_name>"]["suggested_metrics"][]
    id, name, description, sql_hint, builder_definition, status

catalog["__business_context__"]["custom_events"][]
    name, label, sql, builder_definition (groups → event + filters rules)

catalog["__business_context__"]["exclusions"]["always_filter"][]
    Raw SQL fragments injected into every query (e.g. test user exclusions)
```

Metrics loaded by `build_metrics_like_chat()` in `qa/eval_production.py` and `chat.py`: maps `sql_hint` → `"sql"` key, appends custom events as `ce_*` pseudo-metric entries.
