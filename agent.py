"""
agent.py — CLI entry point for the analytics agent.

Thin wrapper around the core/ pipeline:
  orchestrator → hypothesis_agent → analyst → story_architect

Usage:
    python agent.py
    python agent.py "What is DAU for last 30 days?"
    python agent.py "Why did transactions drop last month?"
    python agent.py "Forecast DAU for the next 2 weeks"

Interactive mode maintains a session across turns so the context resolver
can detect follow-up questions and rewrite them before routing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from core.infra.logger import setup_logging
setup_logging(log_file="agent.log")

CATALOG_PATH  = Path(__file__).parent / "catalog.json"
DB_PATH       = Path(__file__).parent / "jupiter.duckdb"
_SESSION_FILE = Path(__file__).parent / ".cli_session"


def _load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def _get_sampled_values(catalog: dict) -> dict:
    """
    Fetch distinct values for low-cardinality VARCHAR columns from DuckDB.
    Returns {table: {col: [vals]}} — same shape expected by orchestrator.
    """
    import duckdb
    sampled: dict = {}
    if not DB_PATH.exists():
        return sampled
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).df()["table_name"].tolist()
        skip = {"user_id", "session_id", "event_id", "transaction_id", "device_id"}
        for tname in tables:
            sampled[tname] = {}
            try:
                cols_df = conn.execute(f"DESCRIBE {tname}").df()
                for _, row in cols_df.iterrows():
                    cname, ctype = row["column_name"], row["column_type"]
                    if ctype != "VARCHAR" or cname in skip:
                        continue
                    try:
                        vals = conn.execute(
                            f"SELECT DISTINCT {cname} FROM {tname} "
                            f"WHERE {cname} IS NOT NULL LIMIT 20"
                        ).df()[cname].tolist()
                        sampled[tname][cname] = [str(v) for v in vals if v]
                    except Exception:
                        pass
            except Exception:
                pass
        conn.close()
    except Exception:
        pass
    return sampled


def _get_or_create_session() -> str:
    """Load an existing CLI session id or create a fresh one."""
    from core.memory.chat_history import new_session
    if _SESSION_FILE.exists():
        sid = _SESSION_FILE.read_text().strip()
        if sid:
            return sid
    sid = new_session()
    _SESSION_FILE.write_text(sid)
    return sid


def _reset_session() -> str:
    """Discard the current session and start a new one."""
    from core.memory.chat_history import new_session
    sid = new_session()
    _SESSION_FILE.write_text(sid)
    return sid


def ask(question: str, session_id: str = "", verbose: bool = False) -> dict:
    """
    Run the full core pipeline for a question.

    Delegates to AnalyticsAPI so CLI and Streamlit share the same logic.
    When session_id is provided the context resolver auto-loads memory and
    rewrites contextual follow-up questions before routing.

    Returns:
        {
            "question":          str,
            "analysis_type":     str,
            "matched_metric":    str | None,
            "narrative":         str,
            "executive_summary": str,
            "next_steps":        list[str],
            "confidence":        str,
            "hypothesis_verdict": str,
        }
    """
    from core.api import AnalyticsAPI

    api = AnalyticsAPI(db_path=str(DB_PATH), catalog_path=str(CATALOG_PATH))
    result = api.ask(question, session_id=session_id)

    if verbose and result.analysis_type not in ("out_of_scope", "clarify"):
        print(f"[intent] analysis_type={result.analysis_type} "
              f"metric={result.matched_metric} "
              f"latency={result.latency_ms:.0f}ms")

    return result.to_dict()


def _print_result(result: dict) -> None:
    if result.get("was_rewritten"):
        print(f"  [rewritten from: \"{result['original_question']}\"]")
    print(f"\nAgent: {result['narrative']}")
    if result.get("next_steps"):
        print("\nRecommended actions:")
        for i, step in enumerate(result["next_steps"], 1):
            print(f"  {i}. {step}")
    print()


def main():
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        session_id = _get_or_create_session()
        result = ask(question, session_id=session_id, verbose=True)
        print(f"\n{'='*60}")
        if result.get("was_rewritten"):
            print(f"Original      : {result['original_question']}")
            print(f"Rewritten to  : {result['question']}")
        else:
            print(f"Question      : {result['question']}")
        print(f"Analysis type : {result['analysis_type']}")
        if result.get("matched_metric"):
            print(f"Metric/Event  : {result['matched_metric']}")
        if result.get("confidence"):
            print(f"Confidence    : {result['confidence']}")
        print(f"\nNarrative:\n{result['narrative']}")
        if result.get("executive_summary") and result["executive_summary"] != result["narrative"]:
            print(f"\nExecutive summary:\n{result['executive_summary']}")
        if result.get("next_steps"):
            print(f"\nRecommended actions:")
            for i, step in enumerate(result["next_steps"], 1):
                print(f"  {i}. {step}")
        return

    # Interactive mode — persist session across turns
    session_id = _get_or_create_session()
    print(f"Analytics Agent  (type 'quit' to exit, 'new session' to reset)\n"
          f"Session: {session_id[:8]}…\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if question.lower() in {"quit", "exit", "q"}:
            break
        if question.lower() in {"new session", "reset session"}:
            session_id = _reset_session()
            print(f"  [new session started: {session_id[:8]}…]\n")
            continue
        if not question:
            continue

        result = ask(question, session_id=session_id)
        _print_result(result)


if __name__ == "__main__":
    main()
