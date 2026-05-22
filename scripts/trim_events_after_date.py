#!/usr/bin/env python3
"""Remove events with timestamp after a cutoff date from jupiter.duckdb."""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "jupiter.duckdb"


def trim(db_path: Path, cutoff: str) -> None:
    con = duckdb.connect(str(db_path))
    before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    to_del = con.execute(
        "SELECT COUNT(*) FROM events WHERE DATE(timestamp) > DATE ?",
        [cutoff],
    ).fetchone()[0]
    con.execute("DELETE FROM events WHERE DATE(timestamp) > DATE ?", [cutoff])
    after = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    mx = con.execute("SELECT MAX(timestamp) FROM events").fetchone()[0]
    con.execute("CHECKPOINT")
    con.close()
    print(f"DB: {db_path}")
    print(f"Cutoff: {cutoff} (deleted dates strictly after)")
    print(f"Before: {before:,}  Deleted: {to_del:,}  After: {after:,}  Max timestamp: {mx}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--after", default="2026-05-22", help="Delete rows with date > this (YYYY-MM-DD)")
    args = p.parse_args()
    trim(args.db, args.after)
