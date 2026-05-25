"""
Run a lightweight regression sweep for common fintech analytics questions.

This is a non-UI harness that exercises:
orchestrator -> resolver -> arbiter -> compiler selection

Usage:
  python qa/run_fintech_regression.py
  python qa/eval_benchmark.py
  streamlit run qa/eval_dashboard.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pipeline.orchestrator import orchestrate
from core.semantic.resolver_policy import resolve_query_policy
from core.agents.policy_arbiter import arbitrate
from core.sql.compilers import (
    compile_query,
    compile_custom_event_segment,
    compile_custom_event_single,
    compile_custom_event_split,
    compile_custom_event_retention,
)
from qa.eval_production import (
    apply_post_orchestrator_fixups,
    build_metrics_like_chat,
    configure_sql_guards_from_catalog,
)


CATALOG_PATH = ROOT / "catalog.json"
DB_PATH = ROOT / "jupiter.duckdb"


@dataclass
class CaseResult:
    question: str
    analysis_type: str
    route: str
    confidence: float
    ok: bool
    reason: str
    sql_preview: str = ""


def load_catalog_and_sampled():
    catalog = json.loads(CATALOG_PATH.read_text())
    sampled: dict[str, dict[str, list]] = {}
    if not DB_PATH.exists():
        return catalog, sampled
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).df()["table_name"].tolist()
        skip = {"user_id", "session_id", "event_id", "transaction_id", "device_id"}
        for tname in tables:
            sampled[tname] = {}
            cols_df = conn.execute(f"DESCRIBE {tname}").df()
            for _, row in cols_df.iterrows():
                cname, ctype = row["column_name"], row["column_type"]
                if ctype != "VARCHAR" or cname in skip:
                    continue
                try:
                    vals = conn.execute(
                        f"SELECT DISTINCT {cname} FROM {tname} WHERE {cname} IS NOT NULL LIMIT 20"
                    ).df()[cname].tolist()
                    sampled[tname][cname] = [str(v) for v in vals if v]
                except Exception:
                    pass
    finally:
        conn.close()
    return catalog, sampled


def run_case(question: str, history: list[dict], catalog: dict, sampled: dict, metrics: list[dict]) -> CaseResult:
    qo = orchestrate(
        question=question,
        catalog=catalog,
        sampled_values=sampled,
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        history=history,
        hypothesis_doc=None,
    )
    if qo.analysis_type not in ("clarify", "out_of_scope"):
        apply_post_orchestrator_fixups(
            qo,
            question=question,
            catalog=catalog,
            sampled=sampled,
            metrics=metrics,
            history=history,
        )
    decision = resolve_query_policy(question, qo, catalog, sampled)
    arb = arbitrate(decision, qo, question)
    if not arb.allow_execute:
        return CaseResult(question, qo.analysis_type, decision.route, decision.confidence, False, "arbiter_blocked")

    # Route-specific compilers (aligned with ui.pipeline.get_sql)
    sql = ""
    mce = list(getattr(decision, "matched_custom_events", None) or [])
    if decision.route == "custom_split" and mce:
        ce_b = mce[1] if len(mce) > 1 else None
        sql = compile_custom_event_split(mce[0], ce_b, qo)
    elif decision.route == "custom_single" and mce:
        if qo.analysis_type == "retention":
            sql = compile_custom_event_retention(mce[0], qo)
        else:
            sql = compile_custom_event_single(mce[0], qo)
    elif decision.route == "custom_segment" and mce:
        sql = compile_custom_event_segment(mce[0], qo)
    elif decision.route == "custom_retention" and mce:
        sql = compile_custom_event_retention(mce[0], qo)
    else:
        sql, _ = compile_query(qo, metrics)

    if str(getattr(qo, "analysis_type", "") or "").strip().lower() == "diagnose":
        if sql and str(sql).strip() not in ("", "__analyst__"):
            sql = "__diagnose__"

    if not sql:
        return CaseResult(question, qo.analysis_type, decision.route, decision.confidence, False, "empty_sql")
    preview = " ".join(sql.split())[:160]
    return CaseResult(question, qo.analysis_type, decision.route, decision.confidence, True, "ok", preview)


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    catalog, sampled = load_catalog_and_sampled()
    configure_sql_guards_from_catalog(catalog)
    metrics = build_metrics_like_chat(catalog)

    question_sets = [
        "Total successful transactions in January",
        "UPI success rate for Jan",
        "Transactions per user for Jan",
        "Spends per user for Jan",
        "Spends per user and transactions per user for Jan",
        "NEFT vs UPI numbers for Jan",
        "D7 retention for transacting users",
        "UPI success transactions per user for Feb vs Jan",
    ]

    history: list[dict] = []
    results: list[CaseResult] = []
    for q in question_sets:
        r = run_case(q, history, catalog, sampled, metrics)
        results.append(r)
        # Keep minimal history context for follow-up behavior.
        history.append({"question": q, "qo": {"analysis_type": r.analysis_type}})

    ok = sum(1 for r in results if r.ok)
    print(f"Pass: {ok}/{len(results)}")
    for r in results:
        badge = "OK " if r.ok else "BAD"
        print(f"[{badge}] {r.question}")
        print(f"      type={r.analysis_type} route={r.route} conf={r.confidence:.2f} reason={r.reason}")
        if r.sql_preview:
            print(f"      sql: {r.sql_preview}")


if __name__ == "__main__":
    main()

