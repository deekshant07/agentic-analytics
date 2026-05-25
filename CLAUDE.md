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

## Before touching `qo_fixups.py` — ask these questions first

Fixups are the highest-risk layer. Each one interacts with all others. Before adding or changing one:

1. **Is this filling an empty slot, or overwriting something the orchestrator already set?**
   - Filling = OK. Use `_fill(qo, field, value)` — never `qo.field = value` directly.
   - Overwriting = almost always wrong. If a fixup clobbers orchestrator intent, that's the bug.

2. **Is this compensating for bad LLM output, or doing legitimate post-processing?**
   - Legitimate: catalog hydration, filter validation against sampled values, session time inheritance.
   - Spray and paint: correcting wrong `analysis_type`, inheriting slots the LLM dropped, guessing what a short follow-up meant.
   - If it's spray and paint, the right fix is the orchestrator prompt or structured LLM output — not a new fixup.

3. **Which `analysis_type` values should this fixup never touch?**
   - Every fixup must have an explicit early return for types it doesn't own.
   - `retention`, `funnel`, `behavioral_cohort` have their own compiler paths — fixups must not reclassify them.

4. **What's the invariant this could break?**
   - Run `pytest qa/regression.py -q` before and after. If a new test class is needed, write it first.

## The fixup ownership contract

These rules apply to every fixup, always:

- `analysis_type` — owned by the orchestrator. Fixups may fill if null; never change a set value.
- `metric_id` — fill only when `event` is also empty. A query with its own event doesn't need an inherited metric_id.
- `event` — fill only when empty. Never overwrite a specific event the LLM or user named.
- `breakdown` — only cleared (never replaced) by fixups, except sanitize_invalid_breakdown.

Violations produce a `WARNING: QO_INVARIANT` log line at runtime — check logs after testing a fixup change.

## Testing & Verification

- Before fixing any pipeline bug: (1) identify the bug *class* (not just the instance), (2) write a failing regression test that names the class, (3) fix the general mechanism. Never fix the symptom — ask why it fired at all.
- When a fixup change is the fix, ask if 5 similar bugs exist elsewhere in the same fixup. If yes, fix the pattern, not the one line.
- Always add/run regression tests after fixing pipeline bugs (orchestrator, compiler, chart layers)
- After SQL/compiler fixes, verify with a smoke test — past fixes had follow-on bugs (ambiguous JOINs, invalid aliases starting with digits, missing time columns)
- Watch for false positives in column/keyword detection (e.g. suffix matches like `ifsc_code`, `merchant_category_code`)

## What this is

A **multi-industry** natural-language analytics agent (fintech, SaaS, healthtech, ecommerce, edtech, etc.). Users type questions in plain English; the system orchestrates SQL against DuckDB and returns charts + narrative. Built with Streamlit.

**One codebase, many domains.** Swapping `catalog.json` + the `.duckdb` file changes the domain entirely. Python in `core/` and `ui/` must stay domain-neutral.

## Industry-agnostic rules (mandatory)

**Domain knowledge belongs in `catalog.json`, not in Python.** Before fixing a failure, ask: *If we swap `catalog.json` for SaaS or healthcare, does this change still make sense without editing Python?* If no — move it to the catalog.

**Orchestrator worked examples and rules must also be domain-neutral.** Use generic placeholders (`<signup event>`, `<purchase event>`, `<core action event>`) — never hardcode fintech event names (e.g. `onboarding_completed`, `transaction_reconciled`, `UPI`) in orchestrator prompt text, `_ANALYSIS_TYPES` rules, or worked examples. The same rule applies to column names and filter values. If a fix requires embedding a specific catalog value in the prompt, it belongs in the catalog, not the orchestrator.

Anti-patterns to avoid:
- Resist patching `qo_fixups.py` for new edge cases. Each fixup raises interaction surface and usually masks a root cause that belongs in the compiler, orchestrator, or catalog. If you're writing a third fixup for the same concept, that's a signal to fix the upstream layer instead.
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
