"""
run.py — single entry point for the Catalog Agent

Usage:
  python run.py data.duckdb
  python run.py data.duckdb "Jupiter Money fintech neobank"
  python run.py data.duckdb "Healthcare SaaS for patient management"

Requires .env file with one of:
  OPENAI_API_KEY=sk-...
  GEMINI_API_KEY=...
"""

import sys
import json
import time
from pathlib import Path

from scanner        import scan
from catalog_agent  import run_catalog_agent
from semantic_writer import generate_semantic_files


def main():
    db_path      = sys.argv[1] if len(sys.argv) > 1 else "data.duckdb"
    company_hint = sys.argv[2] if len(sys.argv) > 2 else ""

    # Output to the project root (parent of semantic-layer/), not cwd
    output_dir = Path(db_path).resolve().parent

    if not Path(db_path).exists():
        print(f"\nError: '{db_path}' not found.")
        print(f"Usage: python run.py your_database.duckdb")
        raise SystemExit(1)

    print("\n" + "=" * 55)
    print("  MetricMind — Catalog Agent")
    print("=" * 55)
    print(f"  Database : {db_path}")
    print(f"  Company  : {company_hint or '(will infer from data)'}")
    print("=" * 55)

    t_start = time.time()

    # ── Step 1: Scan ──────────────────────────────────────────
    print("\nStep 1/3 — Scanning warehouse (zero LLM)...")
    t0 = time.time()
    raw_schema = scan(db_path)
    (output_dir / "raw_schema.json").write_text(
        json.dumps(raw_schema, indent=2, default=str)
    )
    print(f"  Completed in {time.time()-t0:.1f}s  →  raw_schema.json")

    # ── Step 2: Catalog Agent (LLM) ───────────────────────────
    print("\nStep 2/3 — Catalog Agent (LLM call per table)...")
    t1 = time.time()
    catalog = run_catalog_agent(raw_schema, company_hint)
    (output_dir / "catalog.json").write_text(json.dumps(catalog, indent=2))
    print(f"  Completed in {time.time()-t1:.1f}s  →  catalog.json")

    # ── Step 3: Write semantic files ──────────────────────────
    print("\nStep 3/3 — Writing semantic files...")
    t2 = time.time()
    generate_semantic_files(catalog, output_dir=str(output_dir))
    print(f"  Completed in {time.time()-t2:.1f}s")

    # ── Summary ───────────────────────────────────────────────
    total = time.time() - t_start
    print("\n" + "=" * 55)
    print(f"  Done in {total:.1f}s")
    print("=" * 55)
    print("\n  Files written:")
    print("    raw_schema.json  — raw scanner output")
    print("    catalog.json     — structured catalog (programmatic use)")
    print("    catalog.md       — semantic context → inject into agent prompt")
    print("    tables/          — per-table markdown files (one folder per table)")

    biz = catalog.get("__business_context__", {})
    if biz:
        print(f"\n  Industry detected : {biz.get('industry', 'unknown')}")
        print(f"  Company          : {biz.get('company', 'unknown')}")
        print(f"  Custom events    : {len(biz.get('custom_events', []))}")
        print(f"  Glossary terms   : {len(biz.get('exclusions', {}).get('glossary', []))}")

    print("\n  Catalog summary:")
    for table_name, entry in catalog.items():
        if table_name.startswith("__"):
            continue
        if "error" in entry:
            print(f"    ✗ {table_name}: {entry['error'][:50]}")
            continue
        events    = entry.get("events", [])
        high_cols = [c for c in entry.get("columns", []) if c.get("analysis_priority") == "high"]
        pii_cols  = [c for c in entry.get("columns", []) if c.get("is_pii")]
        print(f"\n    ✓ {table_name}")
        print(f"      {entry.get('table_description','')[:65]}")
        print(f"      {len(events)} events  |  "
              f"{len(high_cols)} ★ dimensions  |  "
              f"{len(pii_cols)} PII columns")

    print("\n  Next step:")
    print("    1. Open catalog.md and review what was generated")
    print("    2. Fix anything that looks wrong")
    print("    3. Use catalog.md as your agent's system prompt context")
    print()


if __name__ == "__main__":
    main()
