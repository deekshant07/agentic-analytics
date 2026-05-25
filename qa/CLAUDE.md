# qa/ — Testing Reference

## Regression test layers (`regression.py`)

| Layer | What | Needs DB? |
|---|---|---|
| L1 | QO → SQL string assertions | No |
| L2 | QO fixup chain state assertions | No |
| L3 (`@pytest.mark.db`) | SQL → DuckDB execution | Yes (`jupiter.duckdb`) |

Every bug fix gets a test before it's considered done. Add to the matching `Test*` class or create a new one. L1/L2 use minimal synthetic `QueryObject` + inline catalog fragments — the test documents the bug class, not the fintech schema.

## Eval structure

```
qa/
├── regression.py                    # pytest L1/L2/L3
├── smoke_test.py                    # quick health check
│
├── evals/
│   ├── shared/
│   │   ├── benchmark.py             # shared eval utilities (load_catalog, scoring, etc.)
│   │   ├── production.py            # production-parity fixup helpers
│   │   └── env.py                   # .env loading
│   ├── orchestrator/
│   │   ├── run.py                   # QU gold eval — tests intent routing
│   │   └── gold.yaml                # 66 human-labeled gold queries
│   ├── sql/
│   │   └── run.py                   # compiler eval — QO → SQL assertions (no LLM)
│   ├── business/
│   │   └── run.py                   # result semantics eval (needs DB)
│   ├── charts.py / datasets.py / drift.py / narratives.py / reporter.py / semantic_layer.py
│
└── results/                         # auto-saved eval outputs
    ├── orchestrator/
    │   ├── openai/                  # one JSON per run
    │   ├── groq/
    │   └── gemini/
    ├── sql/
    └── business/
```

Old paths (`qa/eval_qu_gold.py`, `qa/eval_benchmark.py`, etc.) are shims that re-export from the new locations — existing scripts still work.

## Running evals

```bash
# Orchestrator (intent routing) — switch provider with --provider
python -m qa.evals.orchestrator.run --save-outputs /tmp/run.json
python -m qa.evals.orchestrator.run --save-outputs /tmp/run.json --provider groq
python -m qa.evals.orchestrator.run --save-outputs /tmp/run.json --provider gemini
python -m qa.evals.orchestrator.run --rerun-failures qa/results/orchestrator/openai/YYYYMMDD.json

# Compiler (text-to-SQL) — no LLM needed
python -m qa.evals.sql.run

# Business correctness — needs jupiter.duckdb
python -m qa.evals.business.run
```

Results are saved to `qa/results/{component}/{provider}/YYYYMMDD_HHMMSS.json`.

## Shared utilities (`evals/shared/`)

`build_metrics_like_chat()` maps `sql_hint` → `"sql"` key and appends custom events as `ce_*` entries — same logic as `chat.py`.

## When a test fails

Fix the **general mechanism** (compiler path, fixup, orchestrator rule, catalog-driven remap). If the bug is bad catalog data for the dev tenant, fix `catalog.json` or the semantic-layer generator — not a hardcoded workaround in `core/`.
