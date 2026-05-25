# Analytics Agent — Eval System Reference

## Architecture Overview

```
qa/
├── regression.py             L1/L2/L3 pytest regression tests (no LLM)
├── eval_benchmark.py         Full eval harness — orchestrates, compiles, executes, scores
├── eval_production.py        Production-parity fixup application + metric builder
├── eval_env.py               .env loader
├── eval_dashboard.py         Streamlit dashboard for eval results
├── eval_results/             JSON output files from every run
└── evals/
    ├── charts.py             Chart type & quality scoring
    ├── semantic_layer.py     Metric resolution, synonym, catalog coverage eval
    ├── narratives.py         Faithfulness, hallucination, actionability scoring
    ├── drift.py              Regression drift detection across eval runs
    ├── datasets.py           Synthetic & adversarial case generation, gold dataset I/O
    ├── reporter.py           Markdown report + run comparison generation
    └── datasets/             JSON gold dataset files (persisted between sessions)
```

---

## What Each Layer Evaluates

### Layer 0 — Regression Tests (`regression.py`)
**No LLM required. Run before every merge.**

| Class | What it checks |
|-------|---------------|
| L1 — SQL string tests | QO → SQL structural assertions (regex on SQL text) |
| L2 — Fixup state tests | QO before/after fixup chain; slot invariants |
| L3 (`@pytest.mark.db`) | SQL → DuckDB execution; real result values |

```bash
pytest qa/regression.py -q                 # L1 + L2 (fast, CI-safe)
pytest qa/regression.py -m db -q           # L3 (needs jupiter.duckdb)
```

---

### Layer 1 — Full Benchmark (`eval_benchmark.py`)
**Requires LLM. Produces JSON trace files.**

Scoring formula per case (answer_relevance = AR):
```
AR = 0.30 × query_correctness
   + 0.10 × query_routing
   + 0.15 × llm_judge_relevance
   + 0.10 × llm_judge_completeness
   + 0.10 × gold_qo_score
   + 0.10 × gold_sql_score
   + 0.10 × gold_result_score
   + 0.05 × data_presentation
```

**Pass threshold**: AR ≥ 0.75 AND no hard failures (gold_sql or gold_result assertion failures).

**Stage attribution**: Every failure is attributed to one of:
`orchestrator → fixup → compiler → execution → execution_semantic → answer_quality`

```bash
uv run python -m qa.eval_benchmark                          # full run
uv run python -m qa.eval_benchmark --hard --hard-n 20      # worst 20 cases only
```

---

### Layer 2 — Chart Evaluation (`evals/charts.py`)

Scores chart type correctness from `trace["chart_json"]` (Plotly figure JSON).

| analysis_type | Expected | Accepted |
|---------------|----------|---------|
| metric | scatter (line) | bar, scatter |
| segment | bar | bar |
| retention | heatmap | heatmap, scatter |
| funnel | bar | bar, funnel |
| journey | sankey | sankey |
| user_lifecycle | pie | pie, bar |
| behavioral_cohort | bar | bar |

**Score rubric**:
- 1.0 — preferred chart type rendered
- 0.8 — acceptable alternative type
- 0.5 — wrong chart type
- 0.0 — no chart generated

```python
from qa.evals.charts import batch_score_charts
import json
traces = json.loads(Path("qa/eval_results/eval_latest.json").read_text())["cases"]
report = batch_score_charts(traces)
print(report["avg_chart_score"], report["wrong_type_cases"])
```

---

### Layer 3 — Semantic Layer Evaluation (`evals/semantic_layer.py`)

Evaluates metric resolution accuracy against the catalog.

**Components**:
1. **Metric resolution accuracy** — for traces with `gold_qo.metric_id`, does the QO match?
2. **Synonym resolution** — do known aliases (e.g., "D7 retention" → `d7_retention`) resolve?
3. **Deprecated metric detection** — are deprecated metrics being served?
4. **Catalog coverage** — what % of approved catalog metrics appear in eval traces?

```python
from qa.evals.semantic_layer import batch_eval_semantic_layer
import json
data = json.loads(Path("qa/eval_results/eval_latest.json").read_text())
report = batch_eval_semantic_layer(catalog, data["cases"])
print(report["catalog_coverage"]["coverage_pct"])
print(report["metric_resolution"]["failures"])
```

**Adding synonyms**: Edit `KNOWN_SYNONYMS` in `semantic_layer.py`. Synonyms should come
from observed production queries, not guesses.

---

### Layer 4 — Narrative Evaluation (`evals/narratives.py`)

Extends the base LLM judge with narrative-specific dimensions.

| Dimension | Method | What it checks |
|-----------|--------|----------------|
| faithfulness | Rule + LLM | Numeric claims in narrative match data values |
| hallucination | LLM | Claims not derivable from SQL result |
| actionability | Rule + LLM | Narrative recommends a next action |
| clarity | LLM | Non-technical, concise language |
| causal_claim | Rule | "because / due to" supported by breakdown data |

**Rule-based faithfulness** (no LLM cost):
- Extracts numbers from narrative text
- Checks each against actual data values (±10% tolerance)
- Causal claims require ≥2 rows and ≥2 columns in the data

```python
from qa.evals.narratives import score_narratives_from_traces
report = score_narratives_from_traces(traces, skip_llm=True)  # rule-based only
print(report["avg_faithfulness"], report["avg_actionability"])
```

---

### Layer 5 — Drift Detection (`evals/drift.py`)

Monitors score degradation across consecutive eval runs.

**Alert thresholds** (edit `DRIFT_THRESHOLDS` to adjust):

| Metric | Default threshold |
|--------|-----------------|
| pass_rate drop | 5pp |
| avg_answer_relevance drop | 0.03 |
| avg_intent_accuracy drop | 5pp |
| latency p95 spike | 2000 ms |
| hard_fail_count spike | +3 cases |
| per-tag pass_rate drop | 10pp |

```python
from qa.evals.drift import run_full_drift_report
from pathlib import Path

report = run_full_drift_report(Path("qa/eval_results"), n_baseline_runs=3)
print(f"Alerts: {report['n_alerts']}")
for a in report["alerts"]:
    print(f"  [{a['severity']}] {a['metric']}: {a['delta']:+.3f}")
```

**Trend scoring**: `score_trend(history, "avg_answer_relevance")` computes a linear slope
and labels direction as `improving / stable / degrading`.

---

### Layer 6 — Dataset Management (`evals/datasets.py`)

**Synthetic generation** from catalog metrics:
```python
from qa.evals.datasets import generate_synthetic_from_catalog
cases = generate_synthetic_from_catalog(catalog, max_per_metric=2)
# Each case is a SyntheticEvalCase compatible with eval_benchmark.EvalCase
```

**Adversarial cases** — known failure patterns:
```python
from qa.evals.datasets import generate_adversarial_cases
cases = generate_adversarial_cases(catalog)  # typos, ambiguity, bad windows, multi-intent
```

**Gold dataset persistence**:
```python
from qa.evals.datasets import save_gold_dataset, cases_from_failed_traces

# Convert worst failures into gold cases for future regression tests
hard_cases = cases_from_failed_traces(traces, min_ar=0.4)
save_gold_dataset("hard_cases_may2026", hard_cases)

# Load them back
from qa.evals.datasets import load_gold_dataset
cases = load_gold_dataset("hard_cases_may2026")
```

---

### Layer 7 — Reporting (`evals/reporter.py`)

**Single-run markdown report**:
```python
from qa.evals.reporter import generate_markdown_report
from pathlib import Path
report = generate_markdown_report(Path("qa/eval_results/eval_latest.json"))
print(report)
```

**Two-run comparison** (good for PR descriptions):
```python
from qa.evals.reporter import compare_runs
diff = compare_runs(
    Path("qa/eval_results/eval_20260524.json"),
    Path("qa/eval_results/eval_20260525.json"),
)
print(diff)
```

**Trend table** (last N runs):
```python
from qa.evals.reporter import generate_trend_table
print(generate_trend_table(Path("qa/eval_results"), n_runs=10))
```

---

## Running the Full Suite

```bash
# 1. Fast regression (no LLM, CI-safe)
pytest qa/regression.py -q

# 2. Full benchmark (LLM required)
uv run python -m qa.eval_benchmark

# 3. Chart + semantic + narrative analysis on latest run
python3 - <<'EOF'
import json
from pathlib import Path
from qa.evals.charts import batch_score_charts
from qa.evals.semantic_layer import batch_eval_semantic_layer
from qa.evals.narratives import score_narratives_from_traces
from qa.evals.drift import run_full_drift_report
from qa.evals.reporter import generate_markdown_report

data = json.loads(Path("qa/eval_results/eval_latest.json").read_text())
catalog = json.loads(Path("catalog.json").read_text())
traces = data["cases"]

print("=== Charts ===")
print(batch_score_charts(traces))

print("\n=== Semantic Layer ===")
print(batch_eval_semantic_layer(catalog, traces))

print("\n=== Narratives (rule-based) ===")
print(score_narratives_from_traces(traces, skip_llm=True))

print("\n=== Drift ===")
drift = run_full_drift_report(Path("qa/eval_results"))
for a in drift["alerts"]:
    print(f"  [{a['severity']}] {a['metric']}: {a['delta']:+.3f}")
EOF

# 4. Generate markdown report
python3 -c "
from qa.evals.reporter import generate_markdown_report
from pathlib import Path
print(generate_markdown_report(Path('qa/eval_results/eval_latest.json')))
"
```

---

## How to Add New Test Cases

### Add a regression test (deterministic, preferred)
```python
# In qa/regression.py — add to the matching TestXxx class
class TestActivationCompiler(unittest.TestCase):
    def test_upi_filter_in_numerator_only(self):
        qo = QueryObject(metric_id="activation_rate", filters={"transaction_channel": "UPI"})
        sql, _ = compile_query(qo, metrics)
        self.assertIn("transaction_channel = 'UPI'", sql)
        self.assertNotIn("denom_cohort.*transaction_channel", sql)
```

### Add a benchmark case (LLM-evaluated)
```python
# In qa/eval_benchmark.py — add to build_eval_suite()
EvalCase(
    question="show activation rate for UPI users by platform",
    expected_analysis_type="segment",
    tags=["segment", "activation", "filter_rendering"],
    gold_qo={"metric_id": "activation_rate", "filters_contain": {"transaction_channel": "UPI"}},
    gold_sql={"required": ["transaction_channel = 'UPI'"]},
    gold_result={"col_bounds": {"activation_rate_30d": [0, 100]}},
),
```

### Add a gold dataset case (for hard failure tracking)
```python
from qa.evals.datasets import save_gold_dataset
save_gold_dataset("hard_cases", [{
    "question": "...",
    "expected_analysis_type": "metric",
    "tags": ["regression"],
    "gold_qo": {"metric_id": "activation_rate"},
}])
```

---

## Score Interpretation Guide

| Score range | Meaning |
|-------------|---------|
| AR ≥ 0.90 | Excellent — answer directly addresses the question |
| AR 0.75–0.90 | Pass — answer is relevant with minor gaps |
| AR 0.50–0.75 | Partial — intent understood, result has quality issues |
| AR < 0.50 | Fail — wrong intent, SQL failure, or irrelevant data |

| Intent accuracy | Meaning |
|-----------------|---------|
| 1.0 | Correct `analysis_type` matched |
| 0.5 | No `expected_analysis_type` defined (neutral) |
| 0.0 | Wrong `analysis_type` — orchestrator failure |

| Gold QO score | Meaning |
|---------------|---------|
| 1.0 | All slot assertions pass |
| 0.5 | No gold_qo defined (neutral) |
| 0.0 | One or more slot mismatches |

---

## Failure Root Cause Playbook

| Stage | Likely cause | Where to look |
|-------|-------------|--------------|
| `orchestrator` | LLM picked wrong `analysis_type` or wrong `metric_id` | Orchestrator prompt, `orchestrate()` |
| `fixup` | Orchestrator was right but fixup chain corrupted a slot | `ui/qo_fixups.py`, check `_fixup_deltas` in trace |
| `compiler` | SQL not generated or wrong structure | `core/sql/compilers.py`, gold_sql_detail in trace |
| `execution` | SQL syntax or schema error | `execution.error` in trace, run SQL in DuckDB directly |
| `execution_semantic` | SQL ran but values are wrong (rate > 100%, empty) | `gold_result_detail`, check SQL aggregation |
| `answer_quality` | LLM judge found data irrelevant | Check if data shape matches question intent |

---

## Key Design Decisions

1. **Hard assertions over soft scoring** — `gold_sql` and `gold_result` failures cause immediate `pass=False`, regardless of LLM judge score. A compiler bug that produces syntactically valid but wrong SQL cannot pass just because the judge was confused.

2. **Stage attribution before LLM** — `_determine_failure_stage()` uses the QO snapshot diff (`orchestrator_raw` vs `qo_after_fixups`) to distinguish orchestrator vs. fixup failures. This eliminates ambiguous "the model was wrong" blame.

3. **Fixup deltas are first-class** — every fixup pass that changes a slot is logged to `_fixup_deltas`. The eval harness surfaces this so you can see exactly which fixup fired and what it changed.

4. **Catalog-driven, not hardcoded** — eval cases assert on schema-agnostic invariants (e.g., "rate is in [0, 100]") rather than hardcoded column names where possible.

5. **Multi-turn as first-class** — `MultiTurnEvalCase` accumulates history across turns exactly as production does. Filter inheritance and breakdown isolation bugs only surface here, not in single-turn evals.
