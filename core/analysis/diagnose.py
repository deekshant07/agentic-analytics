"""
diagnose.py — "Why did X drop/spike?" agent.

A real analyst doesn't jump straight to dimension slicing. They first ask:
  1. SUPPLY PROBLEM?    Did the upstream event (the funnel input) also drop?
  2. CONVERSION PROBLEM? Did the rate from upstream → this event worsen?
  3. FUNNEL BREAKDOWN?  At which *upstream* step do we lose the most people?
  4. SEGMENT PROBLEM?   Which dimension slice explains the remaining drop?

Only after answering 1-3 does it make sense to ask "which platform/city".

Time comparison:
  • Explicit month span on narrative_qo (time_granularity=month, date_from/date_to
    covering ≥2 months) → first vs last calendar month in that window (e.g. Jan vs Mar).
  • Else if period_end is the last day of a calendar month → that month vs prior month.
  • Else → rolling N/2-day halves.
"""

import json
import math
import os
from calendar import monthrange
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Optional

import duckdb
import pandas as pd
from core.infra.llm import make_llm_client, LLM_STRONG, LLM_FAST
from core.infra.tracer import track
from core.semantic.event_context import QualityContext, detect_quality_context
from core.sql.query_object import QueryObject
from core.agents.story_architect import STORY_ARC_FAILURE_SUMMARY, build_story_arc


# ── Statistical significance (pure math, no scipy dependency) ─────────────────

def _z_test_proportion_change(
    curr_n: int, curr_total: int,
    prev_n: int, prev_total: int,
) -> tuple[float, bool]:
    """
    Two-proportion z-test: is the change in this slice's share statistically
    significant at p < 0.05 (two-tailed)?

    Returns (z_score, is_significant).
    Uses pooled proportion estimate; returns (0.0, False) on degenerate inputs.
    """
    if curr_total <= 0 or prev_total <= 0 or (curr_n + prev_n) == 0:
        return 0.0, False
    p1 = curr_n / curr_total
    p2 = prev_n / prev_total
    p_pool = (curr_n + prev_n) / (curr_total + prev_total)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / curr_total + 1 / prev_total))
    if se == 0:
        return 0.0, False
    z = abs(p1 - p2) / se
    return round(z, 2), z >= 1.96   # 1.96 ≈ p < 0.05 two-tailed


def _annotate_significance(
    ddf: pd.DataFrame,
    curr_total: int,
    prev_total: int,
) -> pd.DataFrame:
    """
    Add 'z_score' and 'significant' columns to a driver contribution DataFrame.
    Significant = True means the change in this slice's share is unlikely due to noise.
    """
    if ddf.empty or curr_total <= 0 or prev_total <= 0:
        return ddf
    z_scores, flags = [], []
    for _, row in ddf.iterrows():
        z, sig = _z_test_proportion_change(
            int(row.get("curr_n", 0) or 0), curr_total,
            int(row.get("prev_n", 0) or 0), prev_total,
        )
        z_scores.append(z)
        flags.append(sig)
    ddf = ddf.copy()
    ddf["z_score"]    = z_scores
    ddf["significant"] = flags
    return ddf

# ── Catalog-driven event graph inference ──────────────────────────────────────
#
# Instead of hardcoding upstream relationships and funnel steps for a specific
# product, these are derived at runtime from the catalog's event_semantics.
# Each event in the catalog carries a `journey` and `stage` — this lets us
# reconstruct within-journey funnels without any domain-specific knowledge.

_STAGE_RANK: dict[str, int] = {
    "started":      0,
    "intermediate": 1,
    "completed":    2,
    "failed":       2,
    "observed":     3,
}

# Columns that are identifiers or timestamps — never useful as slice dimensions
_SKIP_COLS: frozenset[str] = frozenset({
    "user_id", "session_id", "event_id",
    "timestamp", "event_name",
    "created_at", "updated_at",
})

_MAX_DRIVER_DIMS_DIAG = 5


def _infer_upstream(event: str, catalog: dict) -> Optional[str]:
    """
    Derive the upstream event for the supply check using catalog event semantics.
    Returns the closest predecessor in the same journey (highest stage rank
    that is still below the current event's rank). Returns None if the event
    is at the start of its journey or has no catalog entry.
    """
    event_semantics = catalog.get("events", {}).get("event_semantics", {})
    meta = event_semantics.get(event)
    if not meta:
        return None

    journey   = meta.get("journey", "")
    stage     = meta.get("stage", "")
    curr_rank = _STAGE_RANK.get(stage, 99)

    if not journey or curr_rank == 0:
        return None  # already at the start, no upstream

    candidates: list[tuple[int, str]] = []
    for evt, emeta in event_semantics.items():
        if evt == event or emeta.get("journey") != journey:
            continue
        rank = _STAGE_RANK.get(emeta.get("stage", ""), 99)
        if rank < curr_rank:
            candidates.append((rank, evt))

    if not candidates:
        return None

    best_rank = max(r for r, _ in candidates)
    # Deterministic: alphabetical when multiple events share the same rank
    return sorted(evt for r, evt in candidates if r == best_rank)[0]


def _infer_funnel_steps(event: str, catalog: dict) -> Optional[list[str]]:
    """
    Build an ordered funnel for the given event using catalog event semantics.
    Returns all events in the same journey whose stage rank is ≤ this event's
    rank, sorted stage-first then alphabetically. Returns None if fewer than
    2 steps are found (no funnel to show).
    """
    event_semantics = catalog.get("events", {}).get("event_semantics", {})
    meta = event_semantics.get(event)
    if not meta:
        return None

    journey   = meta.get("journey", "")
    stage     = meta.get("stage", "")
    curr_rank = _STAGE_RANK.get(stage, 99)

    if not journey:
        return None

    steps: list[tuple[int, str]] = []
    for evt, emeta in event_semantics.items():
        if emeta.get("journey") != journey:
            continue
        rank = _STAGE_RANK.get(emeta.get("stage", ""), 99)
        if rank <= curr_rank:
            steps.append((rank, evt))

    if len(steps) < 2:
        return None

    steps.sort(key=lambda x: (x[0], x[1]))
    return [s[1] for s in steps]


def _catalog_slice_dims(
    catalog: dict,
    event_sampled_values: dict[str, list],
    exclude_set: set,
) -> list[str]:
    """
    Derive segmentation dimensions from catalog metadata + sampled schema.
    Selects columns that:
      - Exist in the sampled data (have values)
      - Have cardinality between 2 and 20 (useful for GROUP BY)
      - Are not identity/timestamp columns
      - Are not marked PII in the catalog
      - Are not in the caller's exclude set
    """
    col_meta_map: dict[str, dict] = {
        c["raw_name"]: c
        for c in catalog.get("events", {}).get("columns", [])
        if "raw_name" in c
    }

    dims = []
    for col, vals in event_sampled_values.items():
        if col in _SKIP_COLS or col in exclude_set:
            continue
        if not vals or not (2 <= len(vals) <= 20):
            continue
        if col_meta_map.get(col, {}).get("is_pii", False):
            continue
        dims.append(col)

    return sorted(dims)


# ── DB helper ─────────────────────────────────────────────────────────────────

def _run(conn: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    return conn.execute(sql).df()


# ── Calendar helpers ──────────────────────────────────────────────────────────

def _is_month_end(d: date) -> bool:
    """True when d is the last day of its calendar month."""
    return d.day == monthrange(d.year, d.month)[1]


def _prev_month_bounds(d: date) -> tuple[date, date]:
    """(first_day_of_prev_month, last_day_of_prev_month) for the month before d."""
    last_of_prev  = d.replace(day=1) - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev, last_of_prev


def _parse_iso_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    raw = str(s).strip()[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _months_in_half_open_range(start: date, end_exclusive: date) -> list[tuple[date, date]]:
    """
    Full calendar months (first_day, last_day) that overlap [start, end_exclusive).
    """
    if end_exclusive <= start:
        return []
    out: list[tuple[date, date]] = []
    y, m = start.year, start.month
    for _ in range(48):
        first = date(y, m, 1)
        if first >= end_exclusive:
            break
        last = date(y, m, monthrange(y, m)[1])
        if last >= start:
            out.append((first, last))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _try_literal_month_span_comparison(
    narrative_qo: Optional[QueryObject],
) -> Optional[dict]:
    """
    When the orchestrator set an explicit multi-month window with time_granularity=month,
    compare the **first** and **last** calendar months in [date_from, date_to) — e.g.
    Jan–Mar span → January (baseline) vs March (current), instead of March vs February
    from diagnose_period_end alone.
    """
    if narrative_qo is None:
        return None
    gran = (getattr(narrative_qo, "time_granularity", None) or "").strip().lower()
    if gran != "month":
        return None
    ds = _parse_iso_date(getattr(narrative_qo, "date_from", None))
    de = _parse_iso_date(getattr(narrative_qo, "date_to", None))
    if not ds or not de or de <= ds:
        return None

    end_exc = de
    months = _months_in_half_open_range(ds, end_exc)
    # Inclusive month-end sometimes passed as date_to instead of exclusive next day.
    if (
        len(months) < 2
        and (de - ds).days >= 27
        and de.day == monthrange(de.year, de.month)[1]
    ):
        end_exc = de + timedelta(days=1)
        months = _months_in_half_open_range(ds, end_exc)
    if len(months) < 2:
        return None

    prev_start, prev_end = months[0]
    curr_start, curr_end = months[-1]
    if prev_start == curr_start:
        return None

    ps, pe = prev_start.isoformat(), prev_end.isoformat()
    cs, ce = curr_start.isoformat(), curr_end.isoformat()
    cc = f"timestamp::DATE >= DATE '{cs}' AND timestamp::DATE <= DATE '{ce}'"
    pc = f"timestamp::DATE >= DATE '{ps}' AND timestamp::DATE <= DATE '{pe}'"
    wf = (
        f"((timestamp::DATE >= DATE '{ps}' AND timestamp::DATE <= DATE '{pe}') "
        f"OR (timestamp::DATE >= DATE '{cs}' AND timestamp::DATE <= DATE '{ce}'))"
    )
    period_label = f"{curr_start.strftime('%b %Y')} vs {prev_start.strftime('%b %Y')}"
    return {
        "cc": cc,
        "pc": pc,
        "wf": wf,
        "period_label": period_label,
        "curr_start_disp": cs,
        "prev_start_disp": ps,
        "prev_end_disp": pe,
        "curr_end_disp": ce,
    }


def _diagnose_evidence_narrative_fallback(
    period_label: str,
    overall_line: str,
    driver_block: str,
    supply_line: str,
    conv_line: str,
    funnel_line: str,
    quality_section: str,
    dim_lines: str,
) -> str:
    """When LLM calls fail (auth, quota, network), still show the computed diagnosis text."""
    chunks = [
        f"**Period:** {period_label}",
        f"**Overall:** {overall_line}",
    ]
    if supply_line:
        chunks.append(f"**Supply:** {supply_line}")
    if conv_line:
        chunks.append(f"**Conversion:** {conv_line}")
    if funnel_line and "(no upstream" not in funnel_line.lower():
        chunks.append(f"**Funnel:** {funnel_line}")
    qs = (quality_section or "").strip()
    if qs:
        chunks.append(f"**Quality / failures:** {qs.replace(chr(10), ' ')[:900]}")
    if dim_lines and "no single dimension" not in dim_lines.lower():
        chunks.append("**Segment mix:**\n" + dim_lines[:2000])
    elif driver_block and "(no slice" not in driver_block.lower():
        chunks.append("**Drivers:**\n" + driver_block[:2000])
    chunks.append(
        "\n*Narrative synthesis was unavailable (e.g. invalid API key, quota, or network). "
        "The metrics and charts above are still valid — fix credentials and retry for a full prose summary.*"
    )
    return "\n\n".join(chunks)


# ── SQL filter helpers ────────────────────────────────────────────────────────

def _filter_clause(filters: dict) -> str:
    if not filters:
        return ""
    parts = [f"AND {k} = '{str(v).replace(chr(39), chr(39)*2)}'" for k, v in filters.items()]
    return "\n  ".join(parts)


# ── SQL builders (all accept explicit cc/pc/wf condition strings) ─────────────
#
# cc  = current-period WHERE condition  (e.g. "timestamp::DATE >= '2026-03-01' …")
# pc  = previous-period WHERE condition
# wf  = full window filter (both periods combined, for single-scan queries)

def _overall_sql(event: str, cc: str, pc: str,
                 filter_sql: str = "") -> tuple[str, str]:
    fp   = f"\n  {filter_sql}" if filter_sql else ""
    base = f"FROM events\nWHERE event_name = '{event}'\n  AND "
    curr = f"SELECT COUNT(DISTINCT user_id) AS n\n{base}{cc}{fp}"
    prev = f"SELECT COUNT(DISTINCT user_id) AS n\n{base}{pc}{fp}"
    return curr.strip(), prev.strip()


def _volume_check_sql(upstream: str, cc: str, pc: str, wf: str,
                      filter_sql: str = "") -> str:
    """Did the upstream event also change? Answers: supply problem?"""
    fp = f"\n  {filter_sql}" if filter_sql else ""
    return f"""
SELECT
  COUNT(DISTINCT CASE WHEN {cc} THEN user_id END) AS curr_n,
  COUNT(DISTINCT CASE WHEN {pc} THEN user_id END) AS prev_n,
  ROUND(
    (COUNT(DISTINCT CASE WHEN {cc} THEN user_id END) * 100.0
     / NULLIF(COUNT(DISTINCT CASE WHEN {pc} THEN user_id END), 0)) - 100, 1
  ) AS pct_change
FROM events
WHERE event_name = '{upstream}'
  AND {wf}{fp}
""".strip()


def _conversion_check_sql(upstream: str, downstream: str,
                           cc: str, pc: str, wf: str,
                           filter_sql: str = "") -> str:
    """Did the funnel conversion rate change?"""
    fp = f"\n  {filter_sql}" if filter_sql else ""
    return f"""
WITH up AS (
  SELECT
    COUNT(DISTINCT CASE WHEN {cc} THEN user_id END) AS curr_n,
    COUNT(DISTINCT CASE WHEN {pc} THEN user_id END) AS prev_n
  FROM events WHERE event_name = '{upstream}' AND {wf}{fp}
),
dn AS (
  SELECT
    COUNT(DISTINCT CASE WHEN {cc} THEN user_id END) AS curr_n,
    COUNT(DISTINCT CASE WHEN {pc} THEN user_id END) AS prev_n
  FROM events WHERE event_name = '{downstream}' AND {wf}{fp}
)
SELECT
  up.curr_n  AS upstream_curr,
  up.prev_n  AS upstream_prev,
  dn.curr_n  AS downstream_curr,
  dn.prev_n  AS downstream_prev,
  ROUND(dn.curr_n * 100.0 / NULLIF(up.curr_n, 0), 1) AS curr_conversion_pct,
  ROUND(dn.prev_n * 100.0 / NULLIF(up.prev_n, 0), 1) AS prev_conversion_pct,
  ROUND(
    (dn.curr_n * 100.0 / NULLIF(up.curr_n, 0))
    - (dn.prev_n * 100.0 / NULLIF(up.prev_n, 0)), 1
  ) AS conversion_delta_pp
FROM up, dn
""".strip()


def _funnel_comparison_sql(steps: list[str], cc: str, pc: str, wf: str,
                            filter_sql: str = "") -> str:
    """Compare each funnel step in current vs previous period."""
    fp = f"\n  {filter_sql}" if filter_sql else ""
    steps_in   = ", ".join(f"'{s}'" for s in steps)
    step_order = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(steps))
    return f"""
SELECT
  event_name                                                                  AS step,
  COUNT(DISTINCT CASE WHEN {cc} THEN user_id END)                            AS curr_n,
  COUNT(DISTINCT CASE WHEN {pc} THEN user_id END)                            AS prev_n,
  COUNT(DISTINCT CASE WHEN {cc} THEN user_id END)
  - COUNT(DISTINCT CASE WHEN {pc} THEN user_id END)                          AS delta,
  ROUND(
    (COUNT(DISTINCT CASE WHEN {cc} THEN user_id END) * 100.0
     / NULLIF(COUNT(DISTINCT CASE WHEN {pc} THEN user_id END), 0)) - 100, 1
  )                                                                           AS pct_change
FROM events
WHERE event_name IN ({steps_in})
  AND {wf}{fp}
GROUP BY 1
ORDER BY CASE event_name {step_order} END
""".strip()


def _delta_contribution_sql(event: str, dim: str, cc: str, pc: str, wf: str,
                            filter_sql: str = "") -> str:
    """
    Per-dimension contribution to the overall user-count delta (curr vs prev),
    same semantics as analyst driver blueprint (% of total delta).
    """
    fp = f"\n  {filter_sql}" if filter_sql else ""
    ev = event.replace("'", "''")
    return f"""
WITH by_value AS (
  SELECT
    COALESCE(CAST({dim} AS VARCHAR), 'unknown') AS {dim},
    COUNT(DISTINCT CASE WHEN {cc} THEN user_id END) AS curr_n,
    COUNT(DISTINCT CASE WHEN {pc} THEN user_id END) AS prev_n
  FROM events
  WHERE event_name = '{ev}'
    AND {wf}{fp}
    AND {dim} IS NOT NULL
  GROUP BY 1
),
delta AS (
  SELECT
    {dim},
    curr_n,
    prev_n,
    (curr_n - prev_n) AS abs_contrib
  FROM by_value
),
total AS (
  SELECT SUM(abs_contrib) AS total_delta FROM delta
)
SELECT
  d.{dim},
  d.curr_n,
  d.prev_n,
  d.abs_contrib,
  ROUND(100.0 * d.abs_contrib / NULLIF(t.total_delta, 0), 1) AS pct_contrib
FROM delta d
CROSS JOIN total t
WHERE d.curr_n > 0 OR d.prev_n > 0
ORDER BY d.abs_contrib ASC
LIMIT 12
""".strip()


def _slice_sql(event: str, dim: str, cc: str, pc: str, wf: str,
               filter_sql: str = "") -> str:
    """
    Per-dimension % share + mix_shift_pp.
    Shows how the *distribution* of users changed, not just absolute counts.
    """
    fp = f"\n  {filter_sql}" if filter_sql else ""
    return f"""
WITH base AS (
  SELECT
    {dim}                                                                    AS slice,
    COUNT(DISTINCT CASE WHEN {cc} THEN user_id END)                          AS curr_n,
    COUNT(DISTINCT CASE WHEN {pc} THEN user_id END)                          AS prev_n
  FROM events
  WHERE event_name = '{event}'
    AND {wf}{fp}
    AND {dim} IS NOT NULL
  GROUP BY 1
),
totals AS (
  SELECT SUM(curr_n) AS total_curr, SUM(prev_n) AS total_prev FROM base
)
SELECT
  b.slice,
  b.curr_n,
  b.prev_n,
  ROUND(100.0 * b.curr_n / NULLIF(t.total_curr, 0), 1)                     AS curr_pct,
  ROUND(100.0 * b.prev_n / NULLIF(t.total_prev, 0), 1)                     AS prev_pct,
  ROUND(
    100.0 * b.curr_n / NULLIF(t.total_curr, 0)
    - 100.0 * b.prev_n / NULLIF(t.total_prev, 0), 1
  )                                                                          AS mix_shift_pp
FROM base b, totals t
ORDER BY b.prev_n DESC
LIMIT 15
""".strip()


# ── Quality SQL builders ─────────────────────────────────────────────────────

def _attempt_rate_sql(event: str, status_col: str, success_val: str,
                      cc: str, pc: str, wf: str,
                      count_unit: str = "user",
                      filter_sql: str = "") -> str:
    """
    Compare total *attempts* (all statuses) vs *successful completions* per period.

    Answers: DEMAND problem (fewer attempts) or QUALITY problem (lower success rate)?

    count_unit="event"  → COUNT(*) per row — correct for transactions
                          (a user can have many transactions; user_id gives wrong rate)
    count_unit="user"   → COUNT(DISTINCT user_id) — correct for user-journey events
    """
    fp = f"\n  {filter_sql}" if filter_sql else ""
    sv = success_val.replace("'", "''")

    if count_unit == "event":
        curr_att  = f"COUNT(CASE WHEN {cc} THEN 1 END)"
        prev_att  = f"COUNT(CASE WHEN {pc} THEN 1 END)"
        curr_suc  = f"COUNT(CASE WHEN {cc} AND {status_col} = '{sv}' THEN 1 END)"
        prev_suc  = f"COUNT(CASE WHEN {pc} AND {status_col} = '{sv}' THEN 1 END)"
    else:
        curr_att  = f"COUNT(DISTINCT CASE WHEN {cc} THEN user_id END)"
        prev_att  = f"COUNT(DISTINCT CASE WHEN {pc} THEN user_id END)"
        curr_suc  = f"COUNT(DISTINCT CASE WHEN {cc} AND {status_col} = '{sv}' THEN user_id END)"
        prev_suc  = f"COUNT(DISTINCT CASE WHEN {pc} AND {status_col} = '{sv}' THEN user_id END)"

    return f"""
SELECT
  {curr_att}                                              AS curr_attempts,
  {prev_att}                                              AS prev_attempts,
  {curr_suc}                                              AS curr_success,
  {prev_suc}                                              AS prev_success,
  ROUND({curr_suc} * 100.0 / NULLIF({curr_att}, 0), 1)  AS curr_success_rate_pct,
  ROUND({prev_suc} * 100.0 / NULLIF({prev_att}, 0), 1)  AS prev_success_rate_pct
FROM events
WHERE event_name = '{event}'
  AND {wf}{fp}
""".strip()


def _failure_mode_sql(event: str, fail_dim: str, status_col: str, fail_val: str,
                      cc: str, pc: str, wf: str,
                      filter_sql: str = "") -> str:
    """
    Which failure mode (failure_reason / error_code / rejection_reason)
    grew the most between the two periods?

    Returns: failure_mode, curr_n, prev_n, delta, pct_change
    Only looks at rows where status = fail_val, so counts are pure failure counts.
    """
    fp = f"\n  {filter_sql}" if filter_sql else ""
    fv = fail_val.replace("'", "''")
    return f"""
SELECT
  {fail_dim}                                                                         AS failure_mode,
  COUNT(DISTINCT CASE WHEN {cc} THEN user_id END)                                   AS curr_n,
  COUNT(DISTINCT CASE WHEN {pc} THEN user_id END)                                   AS prev_n,
  COUNT(DISTINCT CASE WHEN {cc} THEN user_id END)
  - COUNT(DISTINCT CASE WHEN {pc} THEN user_id END)                                 AS delta,
  ROUND(
    (COUNT(DISTINCT CASE WHEN {cc} THEN user_id END) * 100.0
     / NULLIF(COUNT(DISTINCT CASE WHEN {pc} THEN user_id END), 0)) - 100, 1
  )                                                                                  AS pct_change
FROM events
WHERE event_name = '{event}'
  AND {status_col} = '{fv}'
  AND {fail_dim} IS NOT NULL
  AND {wf}{fp}
GROUP BY 1
ORDER BY curr_n DESC
LIMIT 10
""".strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct(a: float, b: float) -> float:
    return round((a - b) / b * 100, 1) if b else 0.0


def _hypothesize(event: str, direction: str, curr_n: int, prev_n: int,
                 available_dims: list[str], context: str,
                 client) -> list[dict]:
    """Cheap LLM call: prioritise which dimensions to check first."""
    dims_str = ", ".join(available_dims)
    ctx_line = f"\nContext: {context}" if context else ""
    prompt = (
        f"Event '{event}' {direction}: {prev_n:,} → {curr_n:,}.{ctx_line}\n"
        f"Available dimensions: {dims_str}\n"
        f"Which 2 dimensions are most likely to explain the change? "
        f"Respond JSON only: "
        f'[{{"hypothesis":"one sentence","dim":"dim_name"}}, ...]'
    )
    try:
        from core.infra.llm import call_llm
        resp = call_llm(
            client,
            call_site="diagnose._hypothesize",
            model=LLM_FAST, temperature=0, max_tokens=180,
            messages=[
                {"role": "system", "content": "You are a data analyst. JSON only."},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(raw)
        return [h for h in parsed if isinstance(h, dict) and h.get("dim") in available_dims]
    except Exception:
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

def _diagnose_to_investigations(
    event: str,
    overall: dict,
    driver_tables: dict,
    supply_check: Optional[dict],
    conversion_check: Optional[dict],
    quality_check: Optional[dict],
    failure_modes: dict,
    funnel_df: Optional[pd.DataFrame],
    top_dims: list,
    slice_tables: dict,
    overall_line: str,
    supply_line: str,
    conv_line: str,
    funnel_line: str,
    quality_line: str,
    failure_mode_line: str,
    driver_block: str,
    dim_lines: str,
) -> list:
    """
    Convert structured diagnose results into a list of pseudo-Investigation
    objects compatible with story_architect._summarise_investigations().
    """
    invs = []

    # 1. Overall change — scalar frame
    ov_df = pd.DataFrame([{
        "metric":          event,
        "previous_period": overall["previous"],
        "current_period":  overall["current"],
        "delta":           overall["delta"],
        "pct_change":      overall["pct_change"],
    }])
    invs.append(SimpleNamespace(
        name="overall_change",
        purpose=f"Overall {event.replace('_', ' ')} change — current vs previous period",
        df=ov_df,
        insight=overall_line,
        error="",
    ))

    # 2. Contribution drivers — one investigation per dimension
    for dim, ddf in driver_tables.items():
        if ddf.empty:
            continue
        invs.append(SimpleNamespace(
            name=f"driver_contribution_{dim}",
            purpose=f"Which {dim.replace('_', ' ')} values contributed most to the change?",
            df=ddf,
            insight=driver_block,
            error="",
        ))

    # 3. Supply / upstream check
    if supply_check:
        sc_df = pd.DataFrame([supply_check])
        invs.append(SimpleNamespace(
            name="supply_check",
            purpose=f"Did the upstream event ({supply_check['upstream_event']}) also drop? (acquisition vs conversion problem)",
            df=sc_df,
            insight=supply_line,
            error="",
        ))

    # 4. Conversion rate check
    if conversion_check:
        cc_df = pd.DataFrame([conversion_check])
        invs.append(SimpleNamespace(
            name="conversion_check",
            purpose=f"Did the conversion rate from upstream to {event} change?",
            df=cc_df,
            insight=conv_line,
            error="",
        ))

    # 5. Quality / demand check
    if quality_check:
        qc_df = pd.DataFrame([quality_check])
        invs.append(SimpleNamespace(
            name="quality_check",
            purpose="Was this a demand problem (fewer attempts) or a quality problem (higher failure rate)?",
            df=qc_df,
            insight=quality_line,
            error="",
        ))

    # 6. Failure modes
    for dim, fm_df in failure_modes.items():
        if fm_df.empty:
            continue
        invs.append(SimpleNamespace(
            name=f"failure_mode_{dim}",
            purpose=f"Which failure mode ({dim.replace('_', ' ')}) grew the most?",
            df=fm_df,
            insight=failure_mode_line,
            error="",
        ))

    # 7. Funnel step comparison
    if funnel_df is not None and not funnel_df.empty:
        invs.append(SimpleNamespace(
            name="funnel_comparison",
            purpose="How did each upstream funnel step change between periods?",
            df=funnel_df,
            insight=funnel_line,
            error="",
        ))

    # 8. Top dimension mix-shift slices
    for finding in top_dims[:3]:
        dim = finding["dim"]
        sdf = slice_tables.get(dim)
        if sdf is None or sdf.empty:
            continue
        invs.append(SimpleNamespace(
            name=f"dimension_mix_{dim}",
            purpose=f"How did the {dim.replace('_', ' ')} mix shift between periods?",
            df=sdf,
            insight=(
                f"{dim}={finding['slice']}: share {finding.get('prev_pct', 0):.1f}% → "
                f"{finding.get('curr_pct', 0):.1f}% "
                f"(mix shift {finding.get('mix_shift_pp', 0):+.1f} pp)"
            ),
            error="",
        ))

    return invs


@track(name="diagnose", tags=["analysis"], capture_input=False, capture_output=False)
def run_diagnosis(
    event: str,
    time_range_days: int,
    db_path: str,
    openai_api_key: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
    exclude_dims: Optional[list] = None,
    catalog: Optional[dict] = None,
    event_sampled_values: Optional[dict[str, list]] = None,
    question: Optional[str] = None,
    hypothesis_doc=None,
    narrative_thread: Optional[str] = None,
    narrative_qo: Optional[QueryObject] = None,
) -> dict:
    """
    Returns a structured diagnosis dict with supply/conversion/funnel/segment checks,
    plus — when the event has a status column — a quality check (demand vs failure rate)
    and failure mode breakdown.

    Time comparison mode (auto-detected):
      • If narrative_qo has time_granularity=month and date_from/date_to span ≥2 calendar
        months → compare the **first** vs **last** month in that half-open window
        (e.g. Jan–Mar → January vs March).
      • Else if period_end is last day of a calendar month → that month vs prior month
        (MOM-style).
      • Else → rolling N/2-day halves

    Quality analysis (automatic when catalog + sampled_values are provided):
      • Detects status column via naming convention (e.g. transaction_status)
      • Compares total attempts vs successful completions in both periods
        → tells you: DEMAND problem or QUALITY problem?
      • Breaks down by failure_reason / error_code to find the dominant mode
    """
    ref     = date.fromisoformat(period_end) if period_end else date.today()
    ref_str = ref.isoformat()

    # ── Decide comparison mode ────────────────────────────────────────────────
    literal = _try_literal_month_span_comparison(narrative_qo)
    if literal:
        cc = literal["cc"]
        pc = literal["pc"]
        wf = literal["wf"]
        period_label = literal["period_label"]
        curr_start_disp = literal["curr_start_disp"]
        prev_start_disp = literal["prev_start_disp"]
        prev_end_disp = literal["prev_end_disp"]
        ref_str = literal["curr_end_disp"]
    elif period_end and _is_month_end(ref):
        # Calendar-month comparison — aligns with the MOM trend chart
        curr_month_start  = ref.replace(day=1)
        prev_m_start, prev_m_end = _prev_month_bounds(ref)

        cc = (f"timestamp::DATE >= DATE '{curr_month_start}' "
              f"AND timestamp::DATE <= DATE '{ref_str}'")
        pc = (f"timestamp::DATE >= DATE '{prev_m_start}' "
              f"AND timestamp::DATE <= DATE '{prev_m_end}'")
        wf = (f"timestamp::DATE >= DATE '{prev_m_start}' "
              f"AND timestamp::DATE <= DATE '{ref_str}'")

        curr_start_disp = curr_month_start.isoformat()
        prev_start_disp = prev_m_start.isoformat()
        prev_end_disp   = prev_m_end.isoformat()
        period_label    = f"{curr_month_start.strftime('%b %Y')} vs {prev_m_start.strftime('%b %Y')}"
        half            = ref.day  # used only for narrative label
    else:
        # Rolling-window comparison
        half            = time_range_days // 2

        def _cc(pe: str, h: int) -> str:
            return (f"timestamp::DATE > DATE '{pe}' - INTERVAL '{h} days' "
                    f"AND timestamp::DATE <= DATE '{pe}'")

        def _pc(pe: str, h: int) -> str:
            return (f"timestamp::DATE > DATE '{pe}' - INTERVAL '{h * 2} days' "
                    f"AND timestamp::DATE <= DATE '{pe}' - INTERVAL '{h} days'")

        def _wf(pe: str, h: int) -> str:
            return (f"timestamp::DATE > DATE '{pe}' - INTERVAL '{h * 2} days' "
                    f"AND timestamp::DATE <= DATE '{pe}'")

        cc = _cc(ref_str, half)
        pc = _pc(ref_str, half)
        wf = _wf(ref_str, half)

        curr_start_disp = (ref - timedelta(days=half - 1)).isoformat()
        prev_end_disp   = (ref - timedelta(days=half)).isoformat()
        prev_start_disp = (ref - timedelta(days=half * 2 - 1)).isoformat()
        period_label    = f"last {half} days vs prior {half} days"

    filters_     = filters or {}
    exclude_set  = set(exclude_dims or [])
    filter_sql   = _filter_clause(filters_)
    filter_label = (
        "Filtered to: " + ", ".join(f"{k}={v}" for k, v in filters_.items())
        if filters_ else ""
    )

    client = make_llm_client(openai_api_key)
    # read_only=True: share DB with other local processes holding a write lock.
    # Diagnose uses SELECT-only queries.
    conn   = duckdb.connect(db_path, read_only=True)

    # ── Quality context (derive once, use in steps 2.5 and 6) ────────────────
    qctx: Optional[QualityContext] = None
    if catalog is not None and event_sampled_values is not None:
        qctx = detect_quality_context(event, catalog, event_sampled_values)

    # ── 1. Overall delta ───────────────────────────────────────────────────────
    curr_sql, prev_sql = _overall_sql(event, cc, pc, filter_sql)
    curr_n = int(_run(conn, curr_sql)["n"].iloc[0])
    prev_n = int(_run(conn, prev_sql)["n"].iloc[0])
    delta     = curr_n - prev_n
    direction = "dropped" if delta < 0 else "increased"

    candidate_dims_pre = _catalog_slice_dims(catalog or {}, event_sampled_values or {}, exclude_set)
    driver_tables: dict[str, pd.DataFrame] = {}
    dim_delta_score: dict[str, float] = {}

    for dim in candidate_dims_pre[:_MAX_DRIVER_DIMS_DIAG]:
        try:
            ddf = _run(conn, _delta_contribution_sql(event, dim, cc, pc, wf, filter_sql))
        except Exception:
            continue
        if ddf.empty or "pct_contrib" not in ddf.columns:
            continue
        ddf = _annotate_significance(ddf, curr_n, prev_n)
        driver_tables[dim] = ddf
        try:
            dim_delta_score[dim] = float(ddf["pct_contrib"].abs().max())
        except Exception:
            dim_delta_score[dim] = 0.0

    driver_lines = []
    for dim, ddf in driver_tables.items():
        if ddf.empty:
            continue
        worst = ddf.loc[ddf["abs_contrib"].idxmin()] if "abs_contrib" in ddf.columns else None
        if worst is None:
            continue
        try:
            sig_flag = ""
            if "significant" in ddf.columns and bool(worst.get("significant")):
                sig_flag = " ⚠ statistically significant"
            elif "significant" in ddf.columns:
                sig_flag = " (within noise range)"
            driver_lines.append(
                f"- {dim}: largest negative driver **{worst[dim]}** "
                f"(Δ users {int(worst['abs_contrib']):+,}, {float(worst['pct_contrib']):+.1f}% of total delta{sig_flag})"
            )
        except Exception:
            continue
    driver_block = "\n".join(driver_lines) if driver_lines else "(no slice-level contribution data)"

    # ── 2. Supply check ────────────────────────────────────────────────────────
    supply_check = None
    upstream = _infer_upstream(event, catalog or {})
    if upstream:
        try:
            sc_df = _run(conn, _volume_check_sql(upstream, cc, pc, wf, filter_sql))
            if not sc_df.empty:
                row = sc_df.iloc[0]
                supply_check = {
                    "upstream_event": upstream,
                    "curr_n":     int(row["curr_n"]),
                    "prev_n":     int(row["prev_n"]),
                    "pct_change": float(row["pct_change"]) if row["pct_change"] is not None else 0.0,
                }
        except Exception:
            pass

    # ── Self-correction: decide whether supply fully explains the drop ────────
    #
    # If the upstream event dropped ≥80% as much as the target metric (both
    # negative), this is an acquisition-side problem. Running the funnel
    # comparison in that case would show every funnel step dropping
    # proportionally — mirroring the supply change, not adding new signal.
    # Skipping it sharpens the investigation and avoids misleading noise.
    #
    # Exception: if upstream GREW while the target dropped, conversion is
    # clearly the issue — run all downstream steps.
    correction_signals: list[str] = []
    supply_explains_drop = False

    if supply_check and delta != 0:
        sc_pct       = supply_check["pct_change"]
        overall_pct  = _pct(curr_n, prev_n)
        # Both dropped and upstream drop is proportionally as large
        if sc_pct < -1 and overall_pct < -1:
            supply_explains_ratio = abs(sc_pct) / abs(overall_pct)
            if supply_explains_ratio >= 0.80:
                supply_explains_drop = True
                correction_signals.append(
                    f"Acquisition-side root cause identified: upstream event "
                    f"'{upstream}' dropped {sc_pct:+.1f}% vs target {overall_pct:+.1f}% "
                    f"({supply_explains_ratio:.0%} overlap). "
                    f"Funnel comparison skipped — every step would mirror the supply drop. "
                    f"Dimensional drill redirected to upstream event to find the acquisition gap."
                )

    # ── 3. Conversion check ────────────────────────────────────────────────────
    conversion_check = None
    if upstream:
        try:
            cc_df = _run(conn, _conversion_check_sql(upstream, event, cc, pc, wf, filter_sql))
            if not cc_df.empty:
                row = cc_df.iloc[0]
                conversion_check = {
                    "upstream":        upstream,
                    "downstream":      event,
                    "upstream_curr":   int(row["upstream_curr"]),
                    "upstream_prev":   int(row["upstream_prev"]),
                    "downstream_curr": int(row["downstream_curr"]),
                    "downstream_prev": int(row["downstream_prev"]),
                    "curr_pct":  float(row["curr_conversion_pct"])  if row["curr_conversion_pct"]  is not None else 0.0,
                    "prev_pct":  float(row["prev_conversion_pct"])  if row["prev_conversion_pct"]  is not None else 0.0,
                    "delta_pp":  float(row["conversion_delta_pp"])  if row["conversion_delta_pp"]  is not None else 0.0,
                }
        except Exception:
            pass

    # ── 3.5 Quality check (demand vs success-rate) ────────────────────────────
    # Only runs when the event has a detectable status column (e.g. transaction_status).
    # Answers: did fewer users *attempt* the action, or did more *fail* per attempt?
    quality_check  = None
    failure_modes: dict[str, pd.DataFrame] = {}

    if qctx and qctx.has_quality_signal:
        try:
            qc_df = _run(conn, _attempt_rate_sql(
                event, qctx.status_col, qctx.success_val,
                cc, pc, wf,
                count_unit=qctx.count_unit,
                filter_sql=filter_sql,
            ))
            if not qc_df.empty:
                row = qc_df.iloc[0]
                curr_att  = int(row["curr_attempts"])
                prev_att  = int(row["prev_attempts"])
                curr_suc  = int(row["curr_success"])
                prev_suc  = int(row["prev_success"])
                curr_rate = float(row["curr_success_rate_pct"] or 0)
                prev_rate = float(row["prev_success_rate_pct"] or 0)
                quality_check = {
                    "status_col":       qctx.status_col,
                    "curr_attempts":    curr_att,
                    "prev_attempts":    prev_att,
                    "curr_success":     curr_suc,
                    "prev_success":     prev_suc,
                    "curr_success_rate": curr_rate,
                    "prev_success_rate": prev_rate,
                    "rate_delta_pp":    round(curr_rate - prev_rate, 1),
                    "demand_dropped":   curr_att < prev_att * 0.97,   # >3% drop in attempts
                    "quality_degraded": curr_rate < prev_rate - 0.5,  # >0.5pp drop in rate
                }
        except Exception:
            pass

        # Failure mode breakdown — only when we know which value means "failure"
        if qctx.fail_val and qctx.failure_dims:
            for fail_dim in qctx.failure_dims[:2]:   # top 2 failure dimensions
                try:
                    fm_df = _run(conn, _failure_mode_sql(
                        event, fail_dim, qctx.status_col, qctx.fail_val,
                        cc, pc, wf, filter_sql,
                    ))
                    if not fm_df.empty:
                        failure_modes[fail_dim] = fm_df
                except Exception:
                    pass

    # ── 4. Funnel step comparison ──────────────────────────────────────────────
    # Skipped when supply fully explains the drop: every funnel step would
    # drop proportionally (mirroring the acquisition gap), adding no signal.
    funnel_df    = None
    funnel_steps = _infer_funnel_steps(event, catalog or {})
    if funnel_steps and len(funnel_steps) > 1 and not supply_explains_drop:
        try:
            funnel_df = _run(conn, _funnel_comparison_sql(funnel_steps, cc, pc, wf, filter_sql))
        except Exception:
            funnel_df = None
    elif supply_explains_drop and funnel_steps and len(funnel_steps) > 1:
        correction_signals.append(
            f"Funnel steps [{', '.join(funnel_steps)}] skipped — upstream acquisition "
            f"explains {abs(_pct(supply_check['curr_n'], supply_check['prev_n'])):.0f}% "
            f"of the drop; step-by-step comparison would not isolate additional causes."
        )

    # ── 5. Dimension hypotheses ────────────────────────────────────────────────
    candidate_dims = candidate_dims_pre
    hypotheses     = _hypothesize(
        event, direction, curr_n, prev_n,
        candidate_dims, filter_label, client,
    )
    hyp_dim_order = [h["dim"] for h in hypotheses if h.get("dim") in candidate_dims]
    contrib_first = sorted(
        dim_delta_score.keys(),
        key=lambda d: dim_delta_score.get(d, 0.0),
        reverse=True,
    )
    ordered_dims = (
        [d for d in contrib_first if d in candidate_dims]
        + [d for d in hyp_dim_order if d not in contrib_first]
        + [d for d in candidate_dims if d not in contrib_first and d not in hyp_dim_order]
    )

    # ── 6. Dimension slices (mix shift) ───────────────────────────────────────
    slice_tables = {}
    findings     = []

    for dim in ordered_dims:
        try:
            df = _run(conn, _slice_sql(event, dim, cc, pc, wf, filter_sql))
        except Exception:
            continue
        if df.empty:
            continue

        slice_tables[dim] = df

        if "mix_shift_pp" in df.columns:
            df_sorted = df.reindex(df["mix_shift_pp"].abs().sort_values(ascending=False).index)
            worst     = df_sorted.iloc[0]
            mix_shift = float(worst["mix_shift_pp"]) if worst["mix_shift_pp"] is not None else 0.0
            if abs(mix_shift) >= 1.0:
                findings.append({
                    "dim":          dim,
                    "slice":        str(worst["slice"]),
                    "curr":         int(worst["curr_n"]),
                    "prev":         int(worst["prev_n"]),
                    "curr_pct":     float(worst["curr_pct"]) if worst["curr_pct"] is not None else 0.0,
                    "prev_pct":     float(worst["prev_pct"]) if worst["prev_pct"] is not None else 0.0,
                    "mix_shift_pp": mix_shift,
                })

    conn.close()

    findings.sort(key=lambda x: abs(x.get("mix_shift_pp", 0)), reverse=True)
    top_dims   = findings[:4]
    drill_hint = (
        f'drill into {top_dims[0]["dim"]} {top_dims[0]["slice"]}'
        if top_dims else ""
    )

    # ── 7. Narrative ─────────────────────────────────────────────────────────
    overall_line = (
        f"{event}: {prev_n:,} → {curr_n:,} "
        f"({delta:+,}, {_pct(curr_n, prev_n):+.1f}%)"
    )

    supply_line = ""
    if supply_check:
        sc_pct = supply_check["pct_change"]
        if sc_pct < -2:
            sc_verdict = "ACQUISITION PROBLEM — fewer users entered the funnel at all"
        elif sc_pct > 2:
            sc_verdict = "upstream GREW — the drop is not from acquisition, check conversion/activation"
        else:
            sc_verdict = "CONVERSION/ACTIVATION PROBLEM — upstream supply held flat, the drop is in the funnel"
        supply_line = (
            f"Upstream ({upstream}): {supply_check['prev_n']:,} → {supply_check['curr_n']:,} "
            f"({sc_pct:+.1f}%) → {sc_verdict}."
        )

    conv_line = ""
    if conversion_check:
        conv_line = (
            f"Conversion rate ({upstream} → {event}): "
            f"{conversion_check['prev_pct']:.1f}% → {conversion_check['curr_pct']:.1f}% "
            f"({conversion_check['delta_pp']:+.1f} pp)."
        )

    # Funnel: only report on UPSTREAM steps (not the event being diagnosed).
    funnel_line = ""
    if funnel_df is not None and not funnel_df.empty:
        upstream_steps_df = funnel_df[funnel_df["step"] != event]
        if not upstream_steps_df.empty:
            worst_step = upstream_steps_df.loc[
                upstream_steps_df["pct_change"].fillna(0).idxmin()
            ]
            funnel_line = (
                f"Upstream step comparison: {worst_step['step']} "
                f"({worst_step['prev_n']:,} → {worst_step['curr_n']:,}, "
                f"{worst_step['pct_change']:+.1f}%)."
            )

    # Quality check narrative
    quality_line = ""
    if quality_check:
        qc = quality_check
        att_chg = _pct(qc["curr_attempts"], qc["prev_attempts"])
        rdelta  = qc["rate_delta_pp"]
        if qc["demand_dropped"] and not qc["quality_degraded"]:
            quality_line = (
                f"DEMAND PROBLEM: Total attempts fell "
                f"({qc['prev_attempts']:,} → {qc['curr_attempts']:,}, {att_chg:+.1f}%), "
                f"but success rate held steady "
                f"({qc['prev_success_rate']:.1f}% → {qc['curr_success_rate']:.1f}%). "
                f"Fewer users are trying — not a product quality issue."
            )
        elif qc["quality_degraded"] and not qc["demand_dropped"]:
            quality_line = (
                f"QUALITY PROBLEM: Attempts were roughly flat "
                f"({qc['prev_attempts']:,} → {qc['curr_attempts']:,}), "
                f"but success rate dropped "
                f"({qc['prev_success_rate']:.1f}% → {qc['curr_success_rate']:.1f}%, "
                f"{rdelta:+.1f} pp). More failures per attempt — not a demand issue."
            )
        elif qc["demand_dropped"] and qc["quality_degraded"]:
            quality_line = (
                f"BOTH PROBLEMS: Attempts fell "
                f"({qc['prev_attempts']:,} → {qc['curr_attempts']:,}, {att_chg:+.1f}%) "
                f"AND success rate dropped "
                f"({qc['prev_success_rate']:.1f}% → {qc['curr_success_rate']:.1f}%, "
                f"{rdelta:+.1f} pp)."
            )

    # Failure mode narrative
    failure_mode_line = ""
    for dim, fm_df in failure_modes.items():
        if fm_df.empty:
            continue
        df_valid = fm_df.dropna(subset=["pct_change"])
        if df_valid.empty:
            continue
        # The failure mode that grew most in current vs previous period
        worst = df_valid.loc[df_valid["pct_change"].idxmax()]
        if worst["pct_change"] > 0:
            failure_mode_line = (
                f"Dominant failure mode ({dim.replace('_', ' ')}): "
                f"'{worst['failure_mode']}' grew "
                f"({int(worst['prev_n']):,} → {int(worst['curr_n']):,}, "
                f"{float(worst['pct_change']):+.1f}%)."
            )
            break

    dim_lines = "\n".join(
        f"- {f['dim']}={f['slice']}: share {f.get('prev_pct', 0):.1f}% → {f.get('curr_pct', 0):.1f}% "
        f"(mix shift {f.get('mix_shift_pp', 0):+.1f} pp)"
        for f in top_dims
    ) or "No single dimension shows a notable mix shift."

    hyp_block = ""
    if hypotheses:
        hyp_block = "\nInitial hypotheses:\n" + "\n".join(
            f"  • {h['hypothesis']}" for h in hypotheses
        ) + "\n"

    filter_block = f"\n{filter_label}\n" if filter_label else ""
    correction_block = (
        "\nSELF-CORRECTION NOTES (steps skipped based on intermediate findings):\n"
        + "\n".join(f"  • {s}" for s in correction_signals)
        + "\n"
    ) if correction_signals else ""

    # Compose the narrative prompt with all available evidence
    quality_section = ""
    if quality_line or failure_mode_line:
        quality_section = f"""
QUALITY CHECK — was it fewer attempts or more failures?
{quality_line or '(no status column detected for this event)'}
{failure_mode_line}
"""

    # ── 7. Narrative — route through story_architect for consistent quality ───────
    # Build pseudo-investigations from the structured evidence above, then
    # pass them through build_story_arc() to get a CTR narrative arc with
    # prescriptive next steps, validation-calibrated confidence, and
    # hypothesis verdict — identical quality to all other analysis types.
    diag_invs = _diagnose_to_investigations(
        event=event,
        overall={"current": curr_n, "previous": prev_n,
                 "delta": delta, "pct_change": _pct(curr_n, prev_n)},
        driver_tables=driver_tables,
        supply_check=supply_check,
        conversion_check=conversion_check,
        quality_check=quality_check,
        failure_modes=failure_modes,
        funnel_df=funnel_df,
        top_dims=top_dims,
        slice_tables=slice_tables,
        overall_line=overall_line,
        supply_line=supply_line,
        conv_line=conv_line,
        funnel_line=funnel_line,
        quality_line=quality_line,
        failure_mode_line=failure_mode_line,
        driver_block=driver_block,
        dim_lines=dim_lines,
    )

    narrative = ""
    next_steps: list[str] = []
    try:
        story_slots: dict = {
            "analysis_type": "diagnose",
            "event": event,
            "filters": filters or {},
            "time_range_days": time_range_days,
            "time_granularity": "day",
        }
        if narrative_qo is not None:
            d = narrative_qo.to_dict()
            for k in (
                "metric_id",
                "date_from",
                "date_to",
                "diagnose_period_end",
                "time_source",
                "metric_variant",
                "metric_value_col",
                "metric_status_col",
                "metric_status_target",
            ):
                v = d.get(k)
                if v not in (None, "", []):
                    story_slots[k] = v
            tg = d.get("time_granularity")
            if tg:
                story_slots["time_granularity"] = tg
        diag_qo = QueryObject.from_dict(story_slots)
        # Append self-correction context to the narrative thread so story_architect
        # knows which steps were skipped and why, and can reflect that in the narrative.
        combined_thread = "\n".join(filter(None, [
            narrative_thread or "",
            correction_block.strip() if correction_block else "",
        ])) or None

        arc = build_story_arc(
            question=question or f"Why did {event.replace('_', ' ')} change?",
            qo=diag_qo,
            plan=diag_invs,
            hypothesis_doc=hypothesis_doc,
            catalog=catalog,
            openai_api_key=openai_api_key,
            narrative_thread=combined_thread,
        )
        narrative = arc.full_narrative or arc.executive_summary
        next_steps = arc.next_steps
        if (narrative or "").strip() == STORY_ARC_FAILURE_SUMMARY:
            narrative = ""
            next_steps = []
    except Exception:
        pass

    # Fallback to raw LLM narrative if story_architect fails
    if not narrative:
        narrative_prompt = f"""You are a senior data analyst diagnosing a metric change.

Event: {event}
Comparison: {period_label}{filter_block}{hyp_block}
OVERALL: {overall_line}
CONTRIBUTION / DRIVERS: {driver_block}
SUPPLY: {supply_line or '(no upstream data)'}
CONVERSION: {conv_line or '(no conversion data)'}
{quality_section}FUNNEL: {funnel_line or '(no funnel data)'}
DIMENSION MIX SHIFTS: {dim_lines}
{correction_block}
Write 4-6 sentences. State overall change with exact numbers, name top drivers,
diagnose ACQUISITION vs CONVERSION vs QUALITY problem, end with one concrete next step.
No hedging.""".strip()
        from core.infra.llm import call_llm
        try:
            resp = call_llm(
                client,
                call_site="diagnose.narrative",
                model=LLM_STRONG,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": "You are a concise senior data analyst."},
                    {"role": "user",   "content": narrative_prompt},
                ],
            )
            narrative = (resp.choices[0].message.content or "").strip()
        except Exception:
            narrative = _diagnose_evidence_narrative_fallback(
                period_label=period_label,
                overall_line=overall_line,
                driver_block=driver_block,
                supply_line=supply_line or "",
                conv_line=conv_line or "",
                funnel_line=funnel_line or "",
                quality_section=quality_section,
                dim_lines=dim_lines,
            )

    return {
        "overall":            {"current": curr_n, "previous": prev_n,
                               "delta": delta, "pct_change": _pct(curr_n, prev_n)},
        "supply_check":       supply_check,
        "conversion_check":   conversion_check,
        "quality_check":      quality_check,
        "failure_modes":      failure_modes,
        "funnel_df":          funnel_df,
        "top_dims":           top_dims,
        "hypotheses":         hypotheses,
        "narrative":          narrative,
        "next_steps":         next_steps,
        "slice_tables":       slice_tables,
        "drill_hint":         drill_hint,
        "curr_period":        f"{curr_start_disp} → {ref_str}",
        "prev_period":        f"{prev_start_disp} → {prev_end_disp}",
        "correction_signals": correction_signals,   # steps skipped + rationale
    }
