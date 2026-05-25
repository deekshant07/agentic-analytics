"""
qa/trace_to_regression.py — Convert a production trace JSON into a regression test.

Usage:
    # From a file
    python qa/trace_to_regression.py trace.json

    # From stdin (paste the JSON blob from the UI debug panel)
    pbpaste | python qa/trace_to_regression.py -

    # Auto-append to qa/regression.py
    pbpaste | python qa/trace_to_regression.py - --append

Outputs Python code for an EvalCase (eval_benchmark.py) and a regression test
(regression.py) that you can paste or auto-append. Every production bug you
find becomes a test that prevents it from regressing silently.
"""
from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGRESSION_PATH = ROOT / "qa" / "regression.py"


# ── Extraction helpers ────────────────────────────────────────────────────────

def _sig(trace: dict) -> dict:
    return trace.get("signature") or {}


def _extract_qo(trace: dict) -> dict:
    sig = _sig(trace)
    return {
        "analysis_type":    sig.get("analysis_type"),
        "metric_id":        sig.get("metric_id"),
        "event":            sig.get("event"),
        "event_b":          sig.get("event_b"),
        "breakdown":        sig.get("breakdown"),
        "filters":          sig.get("filters") or {},
        "time_range_days":  sig.get("time_range_days"),
        "time_granularity": sig.get("time_granularity"),
        "metric_variant":   sig.get("metric_variant"),
        "metric_status_col":sig.get("metric_status_col"),
    }


def _extract_sql(trace: dict) -> str:
    # Try top-level "sql" key first (chat UI format)
    sql = trace.get("sql") or ""
    if not sql:
        # Try nested under outputs
        sql = (_sig(trace).get("outputs") or {}).get("sql_attached") or ""
    return str(sql).strip()


def _extract_outputs(trace: dict) -> dict:
    return (_sig(trace).get("outputs") or {})


def _infer_gold_sql(qo: dict, sql: str, filters: dict) -> dict | None:
    """Build gold_sql assertions from what SHOULD be in the SQL."""
    required: list[str] = []
    forbidden: list[str] = []
    forbidden_in_cte: dict[str, list[str]] = {}

    # For pct_of_users / activation metrics: must use CTE
    if "WITH denom_cohort" in sql:
        required.append("WITH denom_cohort")
        required.append("LEFT JOIN numer_cohort")
        forbidden.append("COUNT(DISTINCT CASE WHEN")

    # Any filter in qo must appear in SQL
    for col, val in (filters or {}).items():
        safe_col = re.sub(r"[^a-z0-9_]", "", str(col).lower())
        safe_val = str(val).replace("'", "\\'")
        pat = f"{safe_col} = '{safe_val}'"
        if pat in sql:
            required.append(pat)
        # Dimension filters must NOT be in the denominator CTE
        if "WITH denom_cohort" in sql and "denom_cohort" not in col:
            forbidden_in_cte.setdefault("denom_cohort", []).append(safe_col)

    # Guard: app_opened must not appear for specific analysis types
    if qo.get("analysis_type") in ("stickiness", "user_lifecycle", "journey", "retention"):
        if "app_opened" not in sql:
            forbidden.extend(["event_name = 'app_opened'", "event_name = 'app_open'"])

    gold: dict[str, Any] = {}
    if required:
        gold["required"] = required
    if forbidden:
        gold["forbidden"] = forbidden
    if forbidden_in_cte:
        gold["forbidden_in_cte"] = forbidden_in_cte
    return gold or None


def _infer_gold_result(qo: dict, outputs: dict) -> dict | None:
    """Build gold_result from output columns and known semantic constraints."""
    cols = outputs.get("table_columns") or []
    first_row = outputs.get("table_first_row") or []
    row_count = outputs.get("table_row_count") or 0

    gold: dict[str, Any] = {}

    # Rate/pct columns must be 0-100
    rate_cols = [c for c in cols if any(k in str(c).lower() for k in ("rate", "pct", "ratio", "percent"))]
    if rate_cols:
        gold["col_bounds"] = {c: [0, 100] for c in rate_cols}

    # For scalar metrics (single row), assert row_count
    if row_count == 1 and qo.get("time_granularity") == "day":
        gold["row_count_min"] = 1
        gold["row_count_max"] = 1

    # For trend metrics, at least 1 row
    if row_count > 1:
        gold["row_count_min"] = 1

    # Assert key columns present
    if cols:
        # Only assert the most important non-id columns
        key_cols = [c for c in cols if not any(k in c.lower() for k in ("user_id", "session", "id"))][:4]
        if key_cols:
            gold["col_present"] = key_cols

    # Assert no nulls for the rate column
    if rate_cols:
        gold["col_not_null"] = rate_cols[:2]

    return gold or None


def _infer_gold_qo(qo: dict) -> dict | None:
    """Build gold_qo from current QO — only include non-trivial assertions."""
    gold: dict[str, Any] = {}
    if qo.get("analysis_type"):
        gold["analysis_type"] = qo["analysis_type"]
    if qo.get("metric_id"):
        gold["metric_id"] = qo["metric_id"]
    if qo.get("event"):
        gold["event"] = qo["event"]
    if qo.get("breakdown"):
        gold["breakdown"] = qo["breakdown"]
    if qo.get("metric_variant"):
        gold["metric_variant"] = qo["metric_variant"]
    if qo.get("filters"):
        gold["filters_contain"] = dict(qo["filters"])
    return gold or None


# ── Formatters ────────────────────────────────────────────────────────────────

def _py_repr(obj: Any, indent: int = 0) -> str:
    """Compact Python repr for dicts/lists without trailing commas."""
    pad = "    " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = ["{"]
        for k, v in obj.items():
            lines.append(f'{pad}    "{k}": {_py_repr(v, indent + 1)},')
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        if all(isinstance(x, str) for x in obj) and len(obj) <= 4:
            items = ", ".join(f'"{x}"' for x in obj)
            return f"[{items}]"
        lines = ["["]
        for x in obj:
            lines.append(f"{pad}    {_py_repr(x, indent + 1)},")
        lines.append(f"{pad}]")
        return "\n".join(lines)
    if isinstance(obj, str):
        return f'"{obj}"'
    if obj is None:
        return "None"
    return repr(obj)


def generate_eval_case(trace: dict) -> str:
    """Return Python source for an EvalCase that can be added to build_100_question_suite."""
    sig = _sig(trace)
    question = (sig.get("user_prompt") or "").strip().rstrip("\n")
    qo = _extract_qo(trace)
    sql = _extract_sql(trace)
    outputs = _extract_outputs(trace)
    filters = qo.get("filters") or {}

    gold_qo = _infer_gold_qo(qo)
    gold_sql = _infer_gold_sql(qo, sql, filters)
    gold_result = _infer_gold_result(qo, outputs)

    at = qo.get("analysis_type") or ""
    tags = ["regression"]
    if at:
        tags.append(at)
    if filters:
        tags.append("filter")
    if sql and "WITH denom_cohort" in sql:
        tags.append("pct_users_cte")

    lines = [f'EvalCase(']
    lines.append(f'    question={json.dumps(question)},')
    if at:
        lines.append(f'    expected_analysis_type="{at}",')
    lines.append(f'    tags={_py_repr(tags)},')
    if gold_qo:
        lines.append(f'    gold_qo={_py_repr(gold_qo, 1)},')
    if gold_sql:
        lines.append(f'    gold_sql={_py_repr(gold_sql, 1)},')
    if gold_result:
        lines.append(f'    gold_result={_py_repr(gold_result, 1)},')
    lines.append('),')
    return "\n".join(lines)


def generate_regression_test(trace: dict) -> str:
    """Return Python source for a pytest test method that can be added to regression.py."""
    sig = _sig(trace)
    question = (sig.get("user_prompt") or "").strip().rstrip("\n")
    qo = _extract_qo(trace)
    sql = _extract_sql(trace)
    outputs = _extract_outputs(trace)
    filters = qo.get("filters") or {}
    at = qo.get("analysis_type") or "metric"
    mid = qo.get("metric_id") or ""

    # Slugify question for test name
    slug = re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")[:50]

    gold_sql = _infer_gold_sql(qo, sql, filters)
    gold_result = _infer_gold_result(qo, outputs)

    lines = [f"    def test_{slug}(self, metrics, db_conn):"]
    lines.append(f'        """Regression: {question}"""')
    lines.append(f"        qo = QueryObject(")
    lines.append(f'            analysis_type="{at}",')
    if mid:
        lines.append(f'            metric_id="{mid}",')
    if filters:
        lines.append(f"            filters={_py_repr(filters, 3)},")
    tg = qo.get("time_granularity") or "day"
    tr = qo.get("time_range_days") or 30
    lines.append(f'            time_granularity="{tg}",')
    lines.append(f"            time_range_days={tr},")
    lines.append("        )")
    lines.append("        sql, _ = compile_query(qo, metrics)")

    if gold_sql:
        for pat in (gold_sql.get("required") or []):
            lines.append(f'        assert {json.dumps(pat)} in sql, "required pattern missing"')
        for pat in (gold_sql.get("forbidden") or []):
            lines.append(f'        assert {json.dumps(pat)} not in sql, "forbidden pattern found"')
        for cte, pats in (gold_sql.get("forbidden_in_cte") or {}).items():
            for pat in pats:
                lines.append(f'        assert {json.dumps(pat)} not in _cte_body("{cte}", sql), "forbidden in {cte}"')

    if gold_result:
        lines.append("        df = db_conn.execute(sql).df()")
        for col, (lo, hi) in (gold_result.get("col_bounds") or {}).items():
            lines.append(f"        for v in df[{json.dumps(col)}].dropna():")
            lines.append(f"            assert {lo} <= float(v) <= {hi}, f\"{col} = {{v}} out of [{lo}, {hi}]\"")
        rmin = gold_result.get("row_count_min")
        if rmin is not None:
            lines.append(f"        assert len(df) >= {rmin}, f\"Expected >={rmin} rows, got {{len(df)}}\"")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def _load_trace(source: str) -> dict:
    if source == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(source).read_text()
    return json.loads(raw)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert a production trace JSON → regression test code."
    )
    parser.add_argument(
        "source",
        help="Path to trace JSON file, or '-' to read from stdin",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Auto-append the generated pytest test to qa/regression.py",
    )
    parser.add_argument(
        "--format",
        choices=["both", "eval_case", "pytest"],
        default="both",
        help="Output format (default: both)",
    )
    args = parser.parse_args()

    try:
        trace = _load_trace(args.source)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Error reading trace: {e}", file=sys.stderr)
        sys.exit(1)

    sig = _sig(trace)
    question = (sig.get("user_prompt") or "").strip().rstrip("\n")

    print("=" * 72)
    print(f"Question: {question}")
    print(f"Analysis: {sig.get('analysis_type')}  Metric: {sig.get('metric_id')}")
    print(f"Filters:  {sig.get('filters') or '{}'}")
    print("=" * 72)

    if args.format in ("both", "eval_case"):
        print("\n── eval_benchmark.py EvalCase ──────────────────────────────────────")
        print(generate_eval_case(trace))

    if args.format in ("both", "pytest"):
        test_code = generate_regression_test(trace)
        print("\n── qa/regression.py pytest test ────────────────────────────────────")
        print(test_code)

        if args.append:
            existing = REGRESSION_PATH.read_text()
            # Append to the TestActivationRateDB class or as a new standalone function
            marker = "\n# ── AUTO-GENERATED REGRESSION TESTS ──"
            if marker not in existing:
                existing += f"\n\n{marker}\n# Each test below was generated from a production trace.\n"
                existing += "# Add these to the appropriate test class or leave here.\n\n"
                existing += "@pytest.mark.db\nclass TestProductionRegressions:\n"

            indented = textwrap.indent(test_code, "    ")
            existing += "\n" + indented + "\n"
            REGRESSION_PATH.write_text(existing)
            print(f"\n✓ Appended to {REGRESSION_PATH}")


if __name__ == "__main__":
    main()
