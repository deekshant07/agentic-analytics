# semantic-layer/ — Catalog Generation Reference

## What runs here

Catalog generation tools — **run at setup time, not at query time.**

- `scanner.py` — profiles the DuckDB schema into `raw_schema.json`
- `metrics_matcher.py` — matches metric templates from `metrics_library/*.json` against the scanned schema; returns only computable metrics (zero LLM)
- `semantic_writer.py` — calls LLM to write descriptions for pre-matched metrics, writes `catalog.json`
- `event_semantics.py` — enriches event metadata (journey, outcome, terminal stage)
- `metrics_matcher.py` is **catalog-generation only** — it writes `catalog.json`; it never runs at query time

## metrics_matcher conventions

- `_find_col()` token-splits on `_` — prone to suffix false positives (e.g. `merchant_category_code` matching `code` signals)
- `_find_exact_col()` requires full-name match — use for high-specificity signals like `user_id`
- `_match_activation_event()` uses event_semantics scores (journey/outcome/terminal/stage) — prefer over raw token matching for onboarding metrics
- Malformed SQL (empty string literals `= ''`) is caught and skipped in `_fill_template()`

## metrics_library/

Industry metric packs: `universal.json` always loaded; `{industry}.json` loaded when industry is specified. Each template has `requires` (column/event/date constraints) and `sql_template` with `{table}`, `{user_col}`, `{time_col}`, `{event_col}`, `{matched_event}`, `{start_event}`, `{end_event}`, `{numeric_col}` placeholders.

Add new metrics here, not in Python code.
