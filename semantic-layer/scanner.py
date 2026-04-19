"""
scanner.py — Step 1
Pure Python. Zero LLM. Zero cost.

Reads your DuckDB and produces raw_schema.json with:
- Table names, row counts
- Column names + types + cardinality
- Actual distinct values for low-cardinality columns (Mitzu pattern)
- Detects which tables are event tables
- 5 sample rows per table (key for LLM to understand the data)
"""

import duckdb
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict


@dataclass
class ColumnProfile:
    name: str
    type: str
    cardinality: int = 0
    null_pct: float = 0.0
    sample_values: list = field(default_factory=list)


@dataclass
class TableProfile:
    name: str
    row_count: int
    columns: list
    sample_rows: list
    is_event_table: bool
    event_name_col: str
    event_names: list
    event_properties: dict   # event_name -> list of non-null column names
    event_groups: dict       # event_name -> {group_col: value, ...}
    event_counts: dict       # event_name -> row count
    date_range: dict         # {min: str, max: str, days: int}
    unique_users: int        # COUNT(DISTINCT user_id)
    product_signals: dict    # pre-computed: feature_adoption, funnels, retention


# ── Product signal computation ─────────────────────────────────────────────────

FUNNEL_START_WORDS = {"initiated", "started", "began", "opened", "attempted",
                       "requested", "triggered", "launched", "created", "submitted"}
FUNNEL_END_WORDS   = {"completed", "succeeded", "success", "finished", "done",
                       "confirmed", "approved", "verified", "failed", "error", "rejected"}


def _word_tokens(event_name: str) -> set:
    """Split snake_case/camelCase event name into word tokens."""
    import re
    tokens = re.split(r"[_\-\s]+", event_name.lower())
    return set(tokens)


def compute_product_signals(conn, table_name: str, event_name_col: str,
                             event_counts: dict, user_col: str,
                             time_col: str, date_range: dict) -> dict:
    """
    Pure SQL. Zero LLM. Zero cost.
    Returns real computed product metrics to ground the LLM's suggestions in facts.
    """
    signals = {}
    total_users = max(
        conn.execute(f'SELECT COUNT(DISTINCT "{user_col}") FROM "{table_name}"').fetchone()[0], 1
    )

    # ── 1. Feature adoption ───────────────────────────────────────────────────
    # % of all users who ever fired each event — tells LLM which features matter
    adoption = {}
    for evt in event_counts:
        try:
            u = conn.execute(f"""
                SELECT COUNT(DISTINCT "{user_col}") FROM "{table_name}"
                WHERE "{event_name_col}" = ?
            """, [evt]).fetchone()[0]
            adoption[evt] = {"users": u, "adoption_pct": round(u / total_users * 100, 1)}
        except Exception:
            pass
    signals["feature_adoption"] = adoption

    # ── 2. Auto-detect funnels by name patterns ───────────────────────────────
    # Find pairs where one event name contains a "start" word and the other
    # contains an "end" word AND they share a common base (e.g. kyc_initiated → kyc_completed)
    funnels = []
    events = list(event_counts.keys())
    for evt_a in events:
        tokens_a = _word_tokens(evt_a)
        start_word = next((w for w in tokens_a if w in FUNNEL_START_WORDS), None)
        if not start_word:
            continue
        base_a = tokens_a - {start_word}
        for evt_b in events:
            if evt_a == evt_b:
                continue
            tokens_b = _word_tokens(evt_b)
            end_word = next((w for w in tokens_b if w in FUNNEL_END_WORDS), None)
            if not end_word:
                continue
            base_b = tokens_b - {end_word}
            # Must share at least one meaningful base word
            if not (base_a & base_b):
                continue
            try:
                users_a = adoption.get(evt_a, {}).get("users", 0)
                users_ab = conn.execute(f"""
                    SELECT COUNT(DISTINCT "{user_col}") FROM "{table_name}"
                    WHERE "{user_col}" IN (
                        SELECT DISTINCT "{user_col}" FROM "{table_name}"
                        WHERE "{event_name_col}" = ?
                    ) AND "{event_name_col}" = ?
                """, [evt_a, evt_b]).fetchone()[0]
                if users_a > 0:
                    funnels.append({
                        "from":             evt_a,
                        "to":               evt_b,
                        "outcome":          end_word,
                        "users_entered":    users_a,
                        "users_converted":  users_ab,
                        "conversion_pct":   round(users_ab / users_a * 100, 1),
                    })
            except Exception:
                pass
    signals["auto_funnels"] = sorted(funnels, key=lambda f: f["users_entered"], reverse=True)

    # ── 3. Retention (D1 / D7) ────────────────────────────────────────────────
    # Only compute if we have enough date range to make it meaningful
    days = date_range.get("days", 0) or 0
    if time_col and days >= 8:
        try:
            # Anchor: first day each user appears
            retention = {}
            for d in ([1, 7, 30] if days >= 31 else [1, 7] if days >= 8 else [1]):
                r = conn.execute(f"""
                    WITH first_seen AS (
                        SELECT "{user_col}", MIN(DATE("{time_col}")) AS first_date
                        FROM "{table_name}"
                        GROUP BY 1
                    ),
                    returned AS (
                        SELECT f."{user_col}"
                        FROM first_seen f
                        JOIN "{table_name}" e ON e."{user_col}" = f."{user_col}"
                        WHERE DATE(e."{time_col}") = f.first_date + INTERVAL '{d} days'
                    )
                    SELECT
                        COUNT(DISTINCT first_seen."{user_col}") AS cohort_size,
                        COUNT(DISTINCT returned."{user_col}")   AS retained
                    FROM first_seen
                    LEFT JOIN returned ON returned."{user_col}" = first_seen."{user_col}"
                """).fetchone()
                if r and r[0] > 0:
                    retention[f"d{d}"] = round(r[1] / r[0] * 100, 1)
            signals["retention"] = retention
        except Exception:
            pass

    return signals


def scan(db_path: str) -> dict:
    conn = duckdb.connect(db_path, read_only=True)

    tables = conn.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'main'
        ORDER BY table_name
    """).fetchall()

    schema = {}

    for (table_name,) in tables:
        print(f"  Scanning {table_name}...")

        row_count = conn.execute(
            f'SELECT COUNT(*) FROM "{table_name}"'
        ).fetchone()[0]

        raw_cols = conn.execute(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = '{table_name}'
            ORDER BY ordinal_position
        """).fetchall()

        # 5 real rows — most important signal for LLM
        try:
            sample_df = conn.execute(
                f'SELECT * FROM "{table_name}" LIMIT 5'
            ).df()
            sample_rows = sample_df.to_dict(orient="records")
            for row in sample_rows:
                for k, v in row.items():
                    if hasattr(v, 'isoformat'):
                        row[k] = str(v)
                    elif v != v:  # NaN
                        row[k] = None
        except Exception:
            sample_rows = []

        columns = []
        for col_name, col_type in raw_cols:
            col = ColumnProfile(name=col_name, type=col_type)

            try:
                col.cardinality = conn.execute(
                    f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}"'
                ).fetchone()[0]

                null_count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table_name}" WHERE "{col_name}" IS NULL'
                ).fetchone()[0]
                col.null_pct = round(null_count / max(row_count, 1) * 100, 1)

                # Index distinct values for low-cardinality columns
                # This prevents the LLM from hallucinating filter values
                if col.cardinality <= 100 and col_type.upper() in (
                    "VARCHAR", "TEXT", "BOOLEAN", "INTEGER",
                    "BIGINT", "SMALLINT", "TINYINT"
                ):
                    vals = conn.execute(f"""
                        SELECT DISTINCT "{col_name}"
                        FROM "{table_name}"
                        WHERE "{col_name}" IS NOT NULL
                        ORDER BY "{col_name}"
                        LIMIT 100
                    """).fetchall()
                    col.sample_values = [str(v[0]) for v in vals]

            except Exception:
                pass

            columns.append(col)

        # Detect event table
        col_names_lower = {c.name.lower() for c in columns}

        EVENT_NAME_SIGNALS = {"event_name", "event_type", "event", "action",
                               "activity", "event_key", "tracking_event"}
        event_name_col = next(
            (c.name for c in columns if c.name.lower() in EVENT_NAME_SIGNALS),
            None
        )

        TIME_SIGNALS = {"event_time", "timestamp", "created_at", "occurred_at",
                        "event_ts", "ts", "time"}
        has_time_col = any(c.lower() in TIME_SIGNALS for c in col_names_lower)

        USER_SIGNALS = {"user_id", "userid", "account_id", "customer_id",
                        "member_id", "uid", "visitor_id"}
        has_user_col = any(c.lower() in USER_SIGNALS for c in col_names_lower)

        is_event_table = has_time_col and has_user_col

        event_names = []
        if event_name_col:
            evt_col = next((c for c in columns if c.name == event_name_col), None)
            if evt_col and evt_col.sample_values:
                event_names = evt_col.sample_values

        # Per-event property profiling: for each event, which columns are non-null?
        # Sparse columns (null_pct > 5%) are candidates for event-specific properties.
        # We query each event to find which of these sparse columns it actually populates.
        # Auto-detect event grouping columns.
        # Strategy: name-based signal (words that indicate categorisation)
        # + low cardinality + mostly non-null. Works across any product schema.
        # Examples: event_category, feature_area, product_section, flow_name, screen_type.
        GROUP_SIGNALS = {
            "category", "type", "group", "area", "section",
            "flow", "module", "domain", "feature", "subcategory",
        }
        SKIP_COLS = {
            event_name_col, "event_id", "user_id", "session_id",
            "timestamp", "date", "created_at", "occurred_at",
        }
        SKIP_SUFFIXES = ("_id", "_ts", "_time", "_at", "_key")
        group_cols = [
            c for c in columns
            if c.type.upper() in ("VARCHAR", "TEXT")
            and c.null_pct < 20
            and 2 <= c.cardinality <= 20
            and c.name not in SKIP_COLS
            and not c.name.lower().endswith(SKIP_SUFFIXES)
            and any(sig in c.name.lower() for sig in GROUP_SIGNALS)
        ]

        # Per-event property profiling: for each event, which columns are non-null?
        # Sparse columns (null_pct > 5%) are candidates for event-specific properties.
        event_properties = {}
        event_groups = {}
        if is_event_table and event_name_col and event_names:
            sparse_cols = [
                c for c in columns
                if c.null_pct > 5 and c.name not in (event_name_col, "event_id")
            ]
            print(f"    Profiling {len(event_names)} events × {len(sparse_cols)} sparse columns...")
            for evt in event_names:
                non_null = []
                for col in sparse_cols:
                    try:
                        count = conn.execute(f"""
                            SELECT COUNT(*) FROM "{table_name}"
                            WHERE "{event_name_col}" = ? AND "{col.name}" IS NOT NULL
                        """, [evt]).fetchone()[0]
                        if count > 0:
                            non_null.append(col.name)
                    except Exception:
                        pass
                event_properties[evt] = non_null

                # Capture group column values for this event
                groups = {}
                for gc in group_cols:
                    try:
                        val = conn.execute(f"""
                            SELECT "{gc.name}" FROM "{table_name}"
                            WHERE "{event_name_col}" = ? AND "{gc.name}" IS NOT NULL
                            LIMIT 1
                        """, [evt]).fetchone()
                        if val:
                            groups[gc.name] = val[0]
                    except Exception:
                        pass
                event_groups[evt] = groups

        # ── Event counts, date range, unique users (zero-cost enrichment) ────────
        event_counts = {}
        date_range   = {}
        unique_users = 0

        if is_event_table and event_name_col:
            # How many times each event fires — tells LLM which events matter
            try:
                rows = conn.execute(f"""
                    SELECT "{event_name_col}", COUNT(*) AS cnt
                    FROM "{table_name}"
                    GROUP BY 1
                    ORDER BY 2 DESC
                """).fetchall()
                event_counts = {r[0]: r[1] for r in rows}
            except Exception:
                pass

            # Date range — needed to suggest retention/cohort metrics appropriately
            time_col = next(
                (c.name for c in columns if c.name.lower() in TIME_SIGNALS), None
            )
            if time_col:
                try:
                    r = conn.execute(f"""
                        SELECT MIN(DATE("{time_col}")), MAX(DATE("{time_col}"))
                        FROM "{table_name}"
                    """).fetchone()
                    if r and r[0] and r[1]:
                        days = (r[1] - r[0]).days if hasattr(r[1], 'days') else None
                        date_range = {
                            "min": str(r[0]),
                            "max": str(r[1]),
                            "days": days,
                        }
                except Exception:
                    pass

            # Unique user count — DAU/MAU/conversion denominators
            user_col = next(
                (c.name for c in columns if c.name.lower() in USER_SIGNALS), None
            )
            if user_col:
                try:
                    unique_users = conn.execute(f"""
                        SELECT COUNT(DISTINCT "{user_col}") FROM "{table_name}"
                    """).fetchone()[0]
                except Exception:
                    pass

        # ── Product signals (adoption, funnels, retention) ────────────────────
        product_signals = {}
        if is_event_table and event_name_col and event_counts:
            user_col_name = next(
                (c.name for c in columns if c.name.lower() in USER_SIGNALS), None
            )
            time_col_name = next(
                (c.name for c in columns if c.name.lower() in TIME_SIGNALS), None
            )
            if user_col_name:
                print(f"    Computing product signals (adoption, funnels, retention)...")
                product_signals = compute_product_signals(
                    conn, table_name, event_name_col,
                    event_counts, user_col_name, time_col_name, date_range
                )
                n_funnels = len(product_signals.get("auto_funnels", []))
                retention = product_signals.get("retention", {})
                print(f"    → {n_funnels} funnels detected  |  retention: {retention}")

        profile = TableProfile(
            name=table_name,
            row_count=row_count,
            columns=columns,
            sample_rows=sample_rows,
            is_event_table=is_event_table,
            event_name_col=event_name_col or "",
            event_names=event_names,
            event_properties=event_properties,
            event_groups=event_groups,
            event_counts=event_counts,
            date_range=date_range,
            unique_users=unique_users,
            product_signals=product_signals,
        )

        schema[table_name] = asdict(profile)

    conn.close()
    return schema


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "data.duckdb"

    print(f"\nScanning: {db}")
    schema = scan(db)

    Path("raw_schema.json").write_text(
        json.dumps(schema, indent=2, default=str)
    )

    print(f"\nDone. {len(schema)} tables found:")
    for name, info in schema.items():
        print(f"  {name}: {info['row_count']:,} rows  |  "
              f"event_table={info['is_event_table']}  |  "
              f"events={info['event_names'][:5]}")
    print("\n→ raw_schema.json written")
