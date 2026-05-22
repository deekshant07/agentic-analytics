"""
chat_history.py — SQLite-backed conversation persistence.

Schema: one `turns` row per Q&A exchange.
  - question     : the user's raw question
  - analysis_type, event, breakdown, filters, metric_id : QO slots for orchestrator context
  - answer       : the narrative summary shown to the user
  - sql, metric_name, table_data, steps, error : display artefacts

Session identity is carried in the URL query param ?session=<id>,
so the session survives page refresh as long as the URL is unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent / "chat_history.db"


class _Encoder(json.JSONEncoder):
    """Handles types that standard json can't serialize."""
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if hasattr(o, "item"):          # numpy scalars (int64, float64, …)
            return o.item()
        if hasattr(o, "isoformat"):     # pandas Timestamp, date-like
            return o.isoformat()
        return super().default(o)


def _dumps(obj) -> str:
    return json.dumps(obj, cls=_Encoder)


# ── Connection ─────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH))
    c.row_factory = sqlite3.Row
    return c


# ── Schema init ────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            title       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS turns (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       TEXT    NOT NULL,
            question         TEXT    NOT NULL,
            analysis_type    TEXT,
            event            TEXT,
            breakdown        TEXT,
            filters          TEXT,          -- JSON object
            metric_id        TEXT,
            date_from        TEXT,          -- YYYY-MM-DD absolute start (or null)
            date_to          TEXT,          -- YYYY-MM-DD absolute end (or null)
            time_range_days  INTEGER,       -- rolling window fallback
            time_granularity TEXT,          -- "day" | "week" | "month"
            qo_json          TEXT,          -- full QueryObject.to_dict() as JSON
            memory_summary   TEXT,          -- concise carry-forward memory for orchestrator
            hypothesis_context TEXT,        -- hypothesis frame used for this turn
            hypothesis_verdict TEXT,        -- verdict from story arc (or diagnose)
            answer           TEXT,
            sql              TEXT,
            metric_name      TEXT,
            table_data       TEXT,          -- JSON {columns, data}
            steps            TEXT,          -- JSON list of step strings
            error            TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS user_memory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT    NOT NULL,
            key         TEXT    NOT NULL,
            value       TEXT    NOT NULL,
            context     TEXT,              -- optional: why this was saved
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, key)
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT    NOT NULL,
            turn_id       INTEGER,          -- references turns.id (may be null)
            question      TEXT    NOT NULL,
            event         TEXT,             -- primary event from QO (for targeted retrieval)
            metric_id     TEXT,             -- metric id from QO  (for targeted retrieval)
            analysis_type TEXT,
            sentiment     TEXT    NOT NULL, -- "positive" | "negative"
            note          TEXT,             -- user's free-text correction / note
            created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """)
        # Migration: silently add new columns to existing databases
        for col, defn in [
            ("date_from",        "TEXT"),
            ("date_to",          "TEXT"),
            ("time_range_days",  "INTEGER"),
            ("time_granularity", "TEXT"),
            ("time_source",      "TEXT"),
            ("memory_summary",   "TEXT"),
            ("hypothesis_context", "TEXT"),
            ("hypothesis_verdict", "TEXT"),
            ("custom_event_name", "TEXT"),
            ("qo_json",          "TEXT"),   # full QueryObject snapshot for complete follow-up context
        ]:
            try:
                c.execute(f"ALTER TABLE turns ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists


# ── Session CRUD ───────────────────────────────────────────────────────────────

def new_session() -> str:
    sid = uuid.uuid4().hex[:10]
    with _conn() as c:
        c.execute("INSERT INTO sessions (session_id) VALUES (?)", (sid,))
    return sid


def get_session(session_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def list_sessions(limit: int = 15) -> list[dict]:
    """Return recent sessions ordered by last activity."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def _touch_session(c: sqlite3.Connection, session_id: str, title: Optional[str] = None) -> None:
    if title:
        c.execute(
            "UPDATE sessions SET updated_at = datetime('now'), title = COALESCE(title, ?) WHERE session_id = ?",
            (title[:80], session_id),
        )
    else:
        c.execute(
            "UPDATE sessions SET updated_at = datetime('now') WHERE session_id = ?",
            (session_id,),
        )


# ── Turn CRUD ──────────────────────────────────────────────────────────────────

def save_turn(session_id: str, turn: dict) -> int:
    """
    Persist one Q&A turn. Returns the new row id.

    Expected keys in `turn`:
        question, analysis_type, event, breakdown, filters (dict),
        metric_id, answer, sql, metric_name,
        table ({"columns": [...], "data": [...]}),
        steps (list of str), error (str)
    """
    # Serialize full QueryObject when available — used by get_qo_history for
    # complete follow-up context (includes metric_status_col, threshold, event_b, etc.)
    qo_obj = turn.get("qo_obj")
    qo_json_str = None
    if qo_obj is not None and hasattr(qo_obj, "to_dict"):
        qo_json_str = _dumps(qo_obj.to_dict())

    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO turns
              (session_id, question, analysis_type, event, breakdown, filters,
               metric_id, date_from, date_to, time_range_days, time_granularity,
               time_source, qo_json,
               memory_summary, hypothesis_context, hypothesis_verdict,
               answer, sql, metric_name, custom_event_name, table_data, steps, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn.get("question", ""),
                turn.get("analysis_type"),
                turn.get("event"),
                turn.get("breakdown"),
                _dumps(turn["filters"]) if turn.get("filters") else None,
                turn.get("metric_id"),
                turn.get("date_from"),
                turn.get("date_to"),
                turn.get("time_range_days"),
                turn.get("time_granularity"),
                turn.get("time_source"),
                qo_json_str,
                turn.get("memory_summary"),
                turn.get("hypothesis_context"),
                turn.get("hypothesis_verdict"),
                turn.get("answer"),
                turn.get("sql"),
                turn.get("metric_name"),
                turn.get("custom_event_name"),
                _dumps(turn["table"]) if turn.get("table") else None,
                _dumps(turn["steps"]) if turn.get("steps") else None,
                turn.get("error"),
            ),
        )
        _touch_session(c, session_id, title=turn.get("question"))
        return cur.lastrowid


def load_turns(session_id: str) -> list[dict]:
    """Return all turns for a session, oldest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()

    result = []
    for row in rows:
        t = dict(row)
        if t.get("table_data"):
            t["table"] = json.loads(t.pop("table_data"))
        else:
            t.pop("table_data", None)
        if t.get("steps"):
            t["steps"] = json.loads(t["steps"])
        if t.get("filters"):
            t["filters"] = json.loads(t["filters"])
        result.append(t)
    return result


# ── Orchestrator context ───────────────────────────────────────────────────────

def get_qo_history(session_id: str, limit: int = 5) -> list[dict]:
    """
    Return last `limit` turns as orchestrator-context dicts:
      [{"question": str, "metric_name": str|None, "custom_event_name": str|None, "qo": {...}}, ...]

    Used by the orchestrator for follow-up resolution ("same but filter to iOS").
    Ordered oldest-first so the LLM sees them in chronological order.
    """
    with _conn() as c:
        rows = c.execute(
            """
            SELECT question, analysis_type, event, breakdown, filters, metric_id,
                   date_from, date_to, time_range_days, time_granularity, time_source,
                   qo_json, memory_summary, hypothesis_verdict, answer, metric_name, custom_event_name
            FROM turns
            WHERE session_id = ? AND analysis_type IS NOT NULL
            ORDER BY id DESC LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()

    history = []
    for row in reversed(rows):   # flip back to chronological order
        t = dict(row)
        # Prefer full qo_json snapshot (captures metric_status_col, threshold, event_b, etc.).
        # Fall back to per-field reading for older rows that predate the qo_json column.
        if t.get("qo_json"):
            qo_dict = json.loads(t["qo_json"])
        else:
            qo_dict = {
                "analysis_type":    t["analysis_type"],
                "event":            t["event"],
                "breakdown":        t["breakdown"],
                "filters":          json.loads(t["filters"]) if t["filters"] else {},
                "metric_id":        t["metric_id"],
                "date_from":        t.get("date_from"),
                "date_to":          t.get("date_to"),
                "time_range_days":  t.get("time_range_days"),
                "time_granularity": t.get("time_granularity"),
                "time_source":      t.get("time_source") or "default",
            }
        history.append({
            "question":          t["question"],
            "metric_name":       t.get("metric_name"),
            "custom_event_name": t.get("custom_event_name"),
            "qo":                qo_dict,
            "memory": {
                "summary":            t.get("memory_summary") or (t.get("answer") or "")[:220],
                "hypothesis_verdict": t.get("hypothesis_verdict") or "",
            },
        })
    return history


# ── Session narrative thread (for story_architect continuity) ─────────────────

def get_narrative_thread(session_id: str, limit: int = 3) -> str:
    """
    Return a formatted narrative thread from recent turns for story_architect injection.
    Each entry: question + hypothesis_verdict + memory_summary + cohort context (newest-first).
    Used to maintain story continuity across turns in the same session.
    """
    with _conn() as c:
        rows = c.execute(
            """
            SELECT question, hypothesis_verdict, memory_summary, answer,
                   metric_name, custom_event_name, metric_id, event,
                   date_from, date_to, analysis_type, time_granularity
            FROM turns
            WHERE session_id = ? AND analysis_type IS NOT NULL
            ORDER BY id DESC LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()

    if not rows:
        return ""

    lines = ["Session narrative thread (most recent turns first):"]
    for row in rows:
        t       = dict(row)
        q       = t.get("question", "")
        verdict = t.get("hypothesis_verdict") or ""
        summary = t.get("memory_summary") or (t.get("answer") or "")[:180]
        entry   = f"  • Q: \"{q}\""
        if verdict:
            entry += f" → verdict: {verdict}"
        if summary:
            entry += f"\n    Finding: {summary}"

        ctx_bits = []
        if t.get("metric_name"):
            ctx_bits.append(f"display_metric={t['metric_name']}")
        if t.get("custom_event_name"):
            ctx_bits.append(f"custom_event={t['custom_event_name']}")
        if t.get("metric_id"):
            ctx_bits.append(f"metric_id={t['metric_id']}")
        if t.get("event"):
            ctx_bits.append(f"primary_event={t['event']}")
        if t.get("analysis_type"):
            ctx_bits.append(f"analysis_type={t['analysis_type']}")
        tw = []
        if t.get("date_from"):
            tw.append(str(t["date_from"]))
        if t.get("date_to"):
            tw.append(str(t["date_to"]))
        if tw:
            ctx_bits.append(f"window={'–'.join(tw)}")
        if t.get("time_granularity"):
            ctx_bits.append(f"grain={t['time_granularity']}")
        if ctx_bits:
            entry += "\n    Cohort / metric context: " + ", ".join(ctx_bits)

        lines.append(entry)

    return "\n".join(lines)


# ── Message list reconstruction (for Streamlit UI) ────────────────────────────

def turns_to_messages(turns: list[dict]) -> list[dict]:
    """
    Convert DB turns into the flat user/assistant message list that
    Streamlit renders. Each turn becomes two messages.
    """
    messages = []
    for t in turns:
        messages.append({"role": "user", "content": t["question"]})
        msg: dict = {"role": "assistant", "content": t.get("answer") or ""}
        for key in ("sql", "metric_name", "steps", "table", "error"):
            if t.get(key):
                msg[key] = t[key]
        # Carry metadata needed by the feedback bar
        msg["turn_id"]       = t.get("id")
        msg["_question"]     = t.get("question", "")
        msg["_event"]        = t.get("event")
        msg["_metric_id"]    = t.get("metric_id")
        msg["_analysis_type"]= t.get("analysis_type")
        messages.append(msg)
    return messages


# ── Cross-session user memory ──────────────────────────────────────────────────

def save_user_memory(user_id: str, key: str, value: str, context: str = "") -> None:
    """
    Upsert a user-level memory fact.  Memories persist across sessions and are
    injected into the orchestrator system prompt to avoid re-explaining context.

    Examples:
        save_user_memory("u1", "preferred_metric", "transacting_users")
        save_user_memory("u1", "usual_time_range", "last 30 days")
        save_user_memory("u1", "domain", "fintech / lending")
    """
    with _conn() as c:
        c.execute(
            """
            INSERT INTO user_memory (user_id, key, value, context, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, key) DO UPDATE SET
                value      = excluded.value,
                context    = excluded.context,
                updated_at = datetime('now')
            """,
            (user_id, key[:80], value[:500], context[:200] if context else None),
        )


def load_user_memories(user_id: str, limit: int = 8) -> list[dict]:
    """
    Return the most-recently-updated memories for a user, newest first.
    Returns a list of {"key": str, "value": str, "context": str | None}.
    """
    with _conn() as c:
        rows = c.execute(
            """
            SELECT key, value, context, updated_at
            FROM user_memory
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_user_memory(user_id: str, key: str) -> None:
    """Remove a single memory fact for a user."""
    with _conn() as c:
        c.execute(
            "DELETE FROM user_memory WHERE user_id = ? AND key = ?",
            (user_id, key),
        )


# ── Feedback & correction memory ──────────────────────────────────────────────

def save_feedback(
    session_id: str,
    question: str,
    sentiment: str,
    *,
    event: Optional[str] = None,
    metric_id: Optional[str] = None,
    analysis_type: Optional[str] = None,
    note: Optional[str] = None,
    turn_id: Optional[int] = None,
) -> None:
    """
    Persist one piece of user feedback for a turn.

    sentiment : "positive" | "negative"
    note      : free-text correction or comment (may be None)
    """
    with _conn() as c:
        c.execute(
            """
            INSERT INTO feedback
              (session_id, turn_id, question, event, metric_id,
               analysis_type, sentiment, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, turn_id, question[:400],
             event, metric_id, analysis_type,
             sentiment, (note or "").strip() or None),
        )


def get_relevant_feedback(
    event: Optional[str] = None,
    metric_id: Optional[str] = None,
    limit: int = 6,
) -> list[dict]:
    """
    Return recent negative feedback entries that have a correction note.
    Filters by event / metric_id when provided; falls back to global recents.
    Used by the orchestrator to inject correction context into the prompt.
    """
    with _conn() as c:
        if event or metric_id:
            params: list = []
            clauses: list[str] = ["sentiment = 'negative'", "note IS NOT NULL", "note != ''"]
            if event:
                clauses.append("event = ?")
                params.append(event)
            if metric_id:
                clauses.append("metric_id = ?")
                params.append(metric_id)
            params.append(limit)
            rows = c.execute(
                f"SELECT question, event, metric_id, analysis_type, note, created_at "
                f"FROM feedback WHERE {' AND '.join(clauses)} "
                f"ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            # If nothing targeted, fall back to recent global
            if not rows:
                rows = c.execute(
                    "SELECT question, event, metric_id, analysis_type, note, created_at "
                    "FROM feedback WHERE sentiment = 'negative' AND note IS NOT NULL AND note != '' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        else:
            rows = c.execute(
                "SELECT question, event, metric_id, analysis_type, note, created_at "
                "FROM feedback WHERE sentiment = 'negative' AND note IS NOT NULL AND note != '' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    return [dict(r) for r in rows]


def format_user_memory_block(user_id: str) -> str:
    """
    Return a formatted string suitable for injection into the orchestrator system prompt.
    Returns empty string if no memories exist.

    Example output:
        User context (remembered preferences):
          • preferred_metric: transacting_users
          • usual_time_range: last 30 days
    """
    memories = load_user_memories(user_id)
    if not memories:
        return ""
    lines = ["User context (remembered preferences):"]
    for m in memories:
        line = f"  • {m['key']}: {m['value']}"
        if m.get("context"):
            line += f"  ({m['context']})"
        lines.append(line)
    return "\n".join(lines)
