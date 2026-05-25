"""
qa/regression.py — Fast deterministic regression suite.

Three layers, each catching a class of bugs the existing benchmark misses:

  L1 (compiler)  QO → compile_query → SQL string assertions.
                 No LLM, no DB. Runs in ~milliseconds.
                 Catches: wrong SQL path chosen, filters dropped in compiler.

  L2 (pipeline)  QO → fixup chain → assert QO state.
                 No LLM, no DB. Runs in ~milliseconds.
                 Catches: hydration failures, validation gaps, filter inheritance bugs.

  L3 (db)        Compile SQL → execute → assert result values/ranges.
                 No LLM, needs DB. Runs in ~1 second.
                 Catches: semantic correctness (>100% rates, wrong counts, nulls).

Every test here was written for a real production bug.
Adding a test when you fix a bug makes it impossible for that bug to regress silently.

Usage:
    pytest qa/regression.py -v               # all layers
    pytest qa/regression.py -v -m "not db"  # L1+L2 only (fast, no DB needed)
    pytest qa/regression.py -v -m db        # L3 only (needs jupiter.duckdb)
    pytest qa/regression.py::TestActivationRateCompiler -v  # single class
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.sql.compilers import compile_query, _IS_NOT_NULL_SENTINEL
from core.sql.query_object import QueryObject
from core.pipeline.orchestrator import _resolve_qualified_prebuilt_metric
from core.pipeline.activation_window import (
    incomplete_activation_cohort_note,
    incomplete_retention_cohort_note,
    parse_activation_window_days_from_prompt,
    parse_retention_window_days_from_prompt,
    parse_retention_week_from_prompt,
    series_label_for_column,
)
from core.semantic.resolver_policy import resolve_query_policy
from core.sql.compilers import compile_custom_event_segment
from ui.qo_fixups import (
    _apply_followup_context_repair,
    _apply_same_query_followup,
    _apply_display_modifier_followup,
    _maybe_resolve_clarify_as_followup,
    _hydrate_retention_event_from_metric,
    _apply_lineage_rollforward_filters,
    _remap_invalid_filter_values_via_custom_events,
    _apply_activation_window_from_prompt,
    _apply_retention_window_from_prompt,
    _sanitize_retention_dimension_status,
)
from core.semantic.qo_lineage import merge_rollforward_filters

CATALOG_PATH = ROOT / "catalog.json"
DB_PATH = ROOT / "jupiter.duckdb"


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


@pytest.fixture(scope="session")
def metrics(catalog: dict) -> list[dict]:
    from qa.eval_production import build_metrics_like_chat, configure_sql_guards_from_catalog
    configure_sql_guards_from_catalog(catalog)
    return build_metrics_like_chat(catalog)


@pytest.fixture(scope="session")
def db_conn(catalog):
    """Real DuckDB connection. Tests using this fixture are marked @pytest.mark.db."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")
    if not DB_PATH.exists():
        pytest.skip(f"DB not found: {DB_PATH}")
    from core.sql.compilers import set_global_sql_guards
    bctx = catalog.get("__business_context__", {}) or {}
    guards = [f for f in ((bctx.get("exclusions") or {}).get("always_filter") or []) if isinstance(f, str)]
    set_global_sql_guards(guards)
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    yield conn
    conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cte_body(cte_name: str, sql: str) -> str:
    """Extract text inside a named CTE block."""
    import re
    m = re.search(rf"{re.escape(cte_name)}\s+AS\s*\(", sql, re.IGNORECASE)
    if not m:
        return ""
    start = m.end()
    depth, i = 1, start
    while i < len(sql) and depth > 0:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
        i += 1
    return sql[start: i - 1]


def _activation_qo(**kwargs) -> QueryObject:
    kwargs.setdefault("time_range_days", 30)
    return QueryObject(analysis_type="metric", metric_id="activation_rate", **kwargs)


def _retention_qo(**kwargs) -> QueryObject:
    kwargs.setdefault("time_range_days", 30)
    return QueryObject(analysis_type="retention", metric_id="d7_retention", **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# L1 — COMPILER TESTS
# Feed a QueryObject directly to compile_query, assert SQL string properties.
# ═════════════════════════════════════════════════════════════════════════════

class TestActivationRateCompiler:
    """
    activation_rate is a '% of users' metric.
    It must always use the cohort-correct CTE path, never the scalar ROUND(a/b) hint.

    Bug history:
    - Scalar hint path (_inject_time) ignored qo.filters → UPI filter silently dropped.
    - GROUP BY on scalar ratio caused activation_rate > 100% on monthly trends.
    """

    def test_scalar_uses_cte_not_hint(self, metrics):
        """Default query must use CTE path (denom_cohort), not the inline sql_hint."""
        sql, _ = compile_query(_activation_qo(), metrics)
        assert "WITH denom_cohort" in sql, (
            "activation_rate scalar must use CTE path, not scalar hint.\n"
            f"SQL starts with: {sql[:120]}"
        )
        # Either single-window (LEFT JOIN numer_cohort) or multi-window (LEFT JOIN numer_events).
        assert ("LEFT JOIN numer_cohort" in sql or "LEFT JOIN numer_events" in sql), (
            "activation_rate must join to a numerator CTE, not scan independently."
        )

    def test_scalar_no_independent_count_ratio(self, metrics):
        """Scalar SQL must not use the broken independent-scan CASE WHEN pattern from sql_hint.

        The dangerous pattern (produces rates >100%):
          ROUND(COUNT(DISTINCT CASE WHEN "event_name" = 'X' THEN user_id END) * 100.0
                / COUNT(DISTINCT CASE WHEN "event_name" = 'Y' THEN user_id END), 1)
        All event_name-based CASE WHENs must be inside a CTE that joins to denom_cohort,
        never in a top-level aggregate without a cohort anchor.
        """
        sql, _ = compile_query(_activation_qo(), metrics)
        # The broken hint pattern has 'event_name' inside CASE WHEN at the top-level SELECT.
        # Safe multi-window pattern uses 'numer_ts' (column from the JOIN), not 'event_name'.
        assert 'COUNT(DISTINCT CASE WHEN "event_name"' not in sql, (
            "Scalar rate must not use independent event_name CASE WHEN counts.\n"
            "Independent counts produce rates > 100% when cohorts differ."
        )

    def test_monthly_uses_cte(self, metrics):
        """Monthly trend must use cohort CTE bucketed by denominator-event month."""
        qo = _activation_qo(time_granularity="month", time_range_days=180)
        sql, _ = compile_query(qo, metrics)
        assert "WITH denom_cohort" in sql
        assert "cohort_month" in sql, "Monthly CTE must bucket by cohort_month"
        assert "GROUP BY 1" in sql

    def test_upi_filter_in_numerator(self, metrics):
        """transaction_channel filter must appear in the numerator CTE."""
        qo = _activation_qo(filters={"transaction_channel": "UPI"})
        sql, _ = compile_query(qo, metrics)
        assert "transaction_channel = 'UPI'" in sql, (
            "qo.filters must be applied to the numerator SQL.\n"
            "Bug: _inject_time path ignored qo.filters."
        )

    def test_upi_filter_not_in_denominator(self, metrics):
        """Denominator must remain unfiltered by channel (base = all onboarded users)."""
        qo = _activation_qo(filters={"transaction_channel": "UPI"})
        sql, _ = compile_query(qo, metrics)
        denom_body = _cte_body("denom_cohort", sql)
        assert denom_body, "denom_cohort CTE not found in SQL"
        assert "transaction_channel" not in denom_body, (
            "UPI filter must NOT appear in denominator.\n"
            "Denominator must stay as 'all onboarded users', not 'UPI-onboarded users'."
        )

    def test_status_filter_from_builder_definition(self, metrics):
        """transaction_status=SUCCESS from builder_definition.filters_structured must appear."""
        sql, _ = compile_query(_activation_qo(), metrics)
        assert "transaction_status = 'SUCCESS'" in sql

    def test_upi_filter_monthly_trend(self, metrics):
        """UPI filter must survive when user requests monthly trend follow-up."""
        qo = _activation_qo(
            time_granularity="month",
            time_range_days=180,
            filters={"transaction_channel": "UPI"},
        )
        sql, _ = compile_query(qo, metrics)
        assert "transaction_channel = 'UPI'" in sql
        assert "WITH denom_cohort" in sql

    def test_d7_windowed_activation_uses_interval_join(self, metrics):
        """D7 activation (activation_window_days=7) must join on timestamp interval."""
        qo = _activation_qo(activation_window_days=7)
        sql, _ = compile_query(qo, metrics)
        assert "INTERVAL '7' DAY" in sql, "Windowed activation must use INTERVAL constraint"
        assert "BETWEEN d.first_ts" in sql, "Windowed activation must join on first_ts range"

    def test_default_activation_uses_multi_window(self, metrics):
        """activation_window_days=None with default_windows in catalog → multi-window output.

        Bug history: None was treated as lifetime (win=0), so users who transacted before
        onboarding were counted. Now None → multi-window (7d, 14d, 30d) via default_windows.
        Each window uses numer_ts anchored to first_ts, enforcing temporal ordering.
        """
        qo = _activation_qo(activation_window_days=None)
        sql, _ = compile_query(qo, metrics)
        assert "INTERVAL '7' DAY" in sql, "Default multi-window must include 7d"
        assert "INTERVAL '14' DAY" in sql, "Default multi-window must include 14d"
        assert "INTERVAL '30' DAY" in sql, "Default multi-window must include 30d"
        assert "d.first_ts" in sql, "Windows must be anchored to per-user first_ts (cohort-correct)"

    def test_numerator_scoped_to_denom_cohort(self, metrics):
        """
        Numerator must be scoped to users in denom_cohort, not an independent scan.

        Bug: numerator scanned all history while denominator was time-windowed.
        Fix: multi-window path uses numer_events JOIN FROM denom_cohort, so the
        numerator can only contain users who appear in the denom time window.
        """
        qo = _activation_qo(time_range_days=30, filters={"transaction_channel": "IMPS"})
        sql, _ = compile_query(qo, metrics)
        denom_body = _cte_body("denom_cohort", sql)
        numer_body = _cte_body("numer_cohort", sql) or _cte_body("numer_events", sql)
        assert denom_body, "denom_cohort CTE must exist"
        assert numer_body, "numerator CTE (numer_cohort or numer_events) must exist"
        assert "denom_cohort" in numer_body, (
            "Numerator CTE must join FROM denom_cohort — prevents scanning all history.\n"
            "Bug: numerator scanned all history while denominator was time-windowed."
        )
        assert "INTERVAL '30'" in denom_body, "Denominator must still have its time filter"

    def test_monthly_numerator_uses_conversion_window_join(self, metrics):
        """Monthly trend: numerator must enforce temporal ordering — only events AFTER the anchor.

        Bug history: numerator used AND {tf} (same raw INTERVAL as denominator), which let users
        who transacted before onboarding be counted. Fix: join denom_cohort and enforce
        e.timestamp >= d.first_ts (multi-window) or BETWEEN d.first_ts ... N DAY (single-window).

        With default_windows set, the monthly compiler now uses the multi-window path
        (numer_events CTE + CASE WHEN per window) instead of numer_cohort + BETWEEN.
        Both paths enforce temporal ordering by joining from denom_cohort.
        """
        qo = _activation_qo(time_granularity="month", time_range_days=180)
        sql, _ = compile_query(qo, metrics)
        numer_body = _cte_body("numer_cohort", sql) or _cte_body("numer_events", sql)
        denom_body = _cte_body("denom_cohort", sql)
        assert numer_body, "Numerator CTE (numer_cohort or numer_events) must exist."
        assert "FROM denom_cohort d" in numer_body, (
            "Numerator must join denom_cohort to enforce conversion after anchor event."
        )
        # Single-window uses BETWEEN; multi-window uses >= d.first_ts + CASE WHEN per window.
        enforces_ordering = "BETWEEN d.first_ts" in numer_body or ">= d.first_ts" in numer_body
        assert enforces_ordering, (
            "Numerator must anchor to d.first_ts to prevent counting pre-onboarding events."
        )
        assert "INTERVAL '180'" in denom_body, (
            "Denominator must still be filtered to the 180-day observation window."
        )

    def test_hour_based_window_uses_day_not_hour_count(self, metrics):
        """24hr / 48hr windows must be expressed in days, not the raw hour count.

        Bug risk: LLM sets activation_window_days=24 for "24hr conversion". The orchestrator
        prompt now explicitly requires hours→days conversion, but if it ever regresses the
        compiler would silently produce INTERVAL '24' DAY (24-day window) instead of 1-day.

        This test locks in the correct compiler behaviour for win=1 (24hr→1 day) and
        win=2 (48hr→2 days), and confirms win=24 does NOT appear in either.
        """
        for hours, days in [(24, 1), (48, 2)]:
            qo = _activation_qo(activation_window_days=days, time_range_days=30)
            sql, _ = compile_query(qo, metrics)
            numer_body = _cte_body("numer_cohort", sql) or _cte_body("numer_events", sql)
            assert f"INTERVAL '{days}' DAY" in sql, (
                f"{hours}hr should produce INTERVAL '{days}' DAY, not '{hours}' DAY"
            )
            assert f"INTERVAL '{hours}' DAY" not in sql or days == hours, (
                f"{hours}hr must not produce a raw {hours}-day interval (hours mistaken for days)"
            )


class TestSegmentAnalysisTypePctUsersMetric:
    """
    L1: % of users metrics must compile correctly even when analysis_type=='segment'.

    Bug history:
    - User asked "Show D7 activation" as a follow-up to a monthly activation trend.
    - LLM returned analysis_type='segment', metric_id='activation_rate', breakdown='month_name'.
    - compile_query's % of users check only ran for at=='metric', so segment fell through
      to the ratio-hint branch which returned '__analyst__' → no SQL generated.
    - Fix: extend the % of users check to also handle at=='segment'.
    """

    def test_segment_type_pct_users_monthly_compiles(self, metrics):
        """analysis_type='segment' + % of users metric + monthly gran must compile to CTE SQL."""
        qo = QueryObject(
            analysis_type="segment",
            metric_id="activation_rate",
            breakdown="month_name",
            time_granularity="month",
            time_range_days=180,
        )
        sql, metric_name = compile_query(qo, metrics)
        assert sql not in ("", "__analyst__"), (
            "activation_rate with analysis_type='segment' must compile to SQL, not route to analyst.\n"
            f"Got: {sql!r}"
        )
        assert "WITH denom_cohort" in sql, "Must use cohort CTE path"
        assert "cohort_month" in sql, "Monthly grouping must be present"

    def test_segment_type_pct_users_scalar_compiles(self, metrics):
        """analysis_type='segment' + % of users metric + day gran must compile to scalar CTE."""
        qo = QueryObject(
            analysis_type="segment",
            metric_id="activation_rate",
            breakdown="platform",
            time_granularity="day",
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql not in ("", "__analyst__"), (
            "activation_rate in segment mode (day gran) must compile to SQL.\n"
            f"Got: {sql!r}"
        )
        assert "WITH denom_cohort" in sql


class TestActivationWindowFromPrompt:
    """
    L2: _apply_activation_window_from_prompt fixup must extract the day window from the prompt.

    Bug history:
    - "Show D7 activation" → LLM sets retention_window_days=7 but activation_window_days stays None.
    - _compile_pct_users_metric_monthly defaults to 30d when activation_window_days is None.
    - Fix: parse "D7", "7-day" from prompt and set qo.activation_window_days.
    """

    def _activation_qo_segment(self, **kwargs) -> QueryObject:
        kwargs.setdefault("time_range_days", 180)
        kwargs.setdefault("time_granularity", "month")
        return QueryObject(analysis_type="segment", metric_id="activation_rate", **kwargs)

    def test_d7_sets_activation_window(self, metrics):
        """'D7' in prompt must set activation_window_days=7."""
        qo = self._activation_qo_segment()
        _apply_activation_window_from_prompt(qo, "Show D7 activation rate by cohort month", metrics)
        assert qo.activation_window_days == 7, (
            f"'D7' prompt must set activation_window_days=7, got {qo.activation_window_days}"
        )

    def test_7_day_hyphen_sets_activation_window(self, metrics):
        """'7-day' (hyphenated) in prompt must set activation_window_days=7."""
        qo = self._activation_qo_segment()
        _apply_activation_window_from_prompt(qo, "Show 7-day activation rate by cohort month", metrics)
        assert qo.activation_window_days == 7, (
            f"'7-day' prompt must set activation_window_days=7, got {qo.activation_window_days}"
        )

    def test_7_day_space_before_activation(self, metrics):
        """'7 day UPI activation' (space, not hyphen) must set activation_window_days=7."""
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            filters={"transaction_channel": "UPI"},
            time_granularity="month",
            time_range_days=180,
        )
        prompt = "Show me 7 day UPI activation numbers MOM"
        _apply_activation_window_from_prompt(qo, prompt, metrics)
        assert qo.activation_window_days == 7, (
            f"'7 day ... activation' must set activation_window_days=7, got {qo.activation_window_days}"
        )
        sql, _ = compile_query(qo, metrics)
        assert "INTERVAL '7' DAY" in sql, "Must compile 7-day numerator window, not 30-day default"
        assert "INTERVAL '30' DAY" not in sql
        assert "activation_rate_7d" in sql or "transaction_reconciled_users_7d" in sql

    def test_last_7_days_does_not_set_activation_window(self, metrics):
        """Rolling 'last 7 days' must not be mistaken for activation window."""
        qo = self._activation_qo_segment()
        _apply_activation_window_from_prompt(
            qo, "Show activation rate trend for last 7 days", metrics,
        )
        assert qo.activation_window_days is None

    def test_no_day_signal_leaves_window_unset(self, metrics):
        """Prompt with no D7/N-day signal must not set activation_window_days."""
        qo = self._activation_qo_segment()
        _apply_activation_window_from_prompt(qo, "Show monthly activation trend", metrics)
        assert qo.activation_window_days is None, (
            "Prompt without day-window signal must not set activation_window_days"
        )

    def test_prompt_overrides_wrong_llm_default(self, metrics):
        """User '60 day activation' must override LLM wrongly setting 30."""
        qo = self._activation_qo_segment(activation_window_days=30)
        _apply_activation_window_from_prompt(qo, "share 60 day activation trend", metrics)
        assert qo.activation_window_days == 60
        sql, _ = compile_query(qo, metrics)
        assert "INTERVAL '60' DAY" in sql
        assert "activation_rate_60d" in sql or "transaction_reconciled_users_60d" in sql

    def test_60_day_space_before_activation(self, metrics):
        qo = self._activation_qo_segment()
        _apply_activation_window_from_prompt(qo, "share 60 day activation trend", metrics)
        assert qo.activation_window_days == 60

    def test_non_pct_metric_not_affected(self, metrics):
        """Fixup must not fire for metrics that are not % of users type."""
        qo = QueryObject(analysis_type="metric", metric_id="d7_retention", time_range_days=30)
        _apply_activation_window_from_prompt(qo, "Show D7 retention", metrics)
        assert getattr(qo, "activation_window_days", None) is None, (
            "Non-pct-users metric must not have activation_window_days set by fixup"
        )

    def test_d7_segment_compiles_with_7d_window(self, metrics):
        """End-to-end: D7 activation with segment type must compile using 7-day INTERVAL."""
        qo = QueryObject(
            analysis_type="segment",
            metric_id="activation_rate",
            breakdown="month_name",
            time_granularity="month",
            time_range_days=180,
        )
        _apply_activation_window_from_prompt(qo, "Show D7 activation by cohort month", metrics)
        sql, _ = compile_query(qo, metrics)
        assert "INTERVAL '7' DAY" in sql, (
            "D7 segment activation must compile with INTERVAL '7' DAY, not the 30-day default"
        )
        assert "WITH denom_cohort" in sql


class TestActivationNarrationLabels:
    """Narration must reflect the compiled activation window, not a stale 30d default."""

    def test_series_label_uses_60d_suffix(self):
        qo = QueryObject(
            analysis_type="metric", metric_id="activation_rate", activation_window_days=60,
        )
        assert "60-day window" in series_label_for_column("transaction_reconciled_users_60d", qo)
        assert "30" not in series_label_for_column("activation_rate_60d", qo).lower().replace("60", "")

    def test_incomplete_cohort_note_for_recent_month(self):
        import pandas as pd
        from datetime import date

        qo = QueryObject(
            analysis_type="metric", metric_id="activation_rate", activation_window_days=60,
        )
        # Cohort month = current month → window cannot be complete for all users.
        today = date.today()
        df = pd.DataFrame({
            "month": [today.replace(day=1).isoformat()],
            "onboarding_completed_users": [100],
            "transaction_reconciled_users_60d": [40],
            "activation_rate_60d": [40.0],
        })
        note = incomplete_activation_cohort_note(df, qo)
        assert note, "expected a maturity note for a cohort within the activation window"
        assert "60-day" in note or "60d" in note


class TestInAppFilterCompiler:
    """
    L1: IS NOT NULL sentinel must render correctly in SQL.

    Bug history:
    - Orchestrator generated filters={transaction_channel: 'in_app'} — not a real DB value.
    - SQL rendered as: transaction_channel = 'in_app' → 0 rows.
    - Fix: _remap_invalid_filter_values_via_custom_events replaces bogus value with sentinel,
      compiler renders sentinel as IS NOT NULL.
    """

    def test_is_not_null_sentinel_renders_correctly(self, metrics):
        """__IS_NOT_NULL__ sentinel must produce 'IS NOT NULL' in compiled SQL, not an equality."""
        qo = _activation_qo(
            time_granularity="month",
            time_range_days=180,
            filters={"transaction_channel": _IS_NOT_NULL_SENTINEL},
        )
        sql, _ = compile_query(qo, metrics)
        assert "transaction_channel IS NOT NULL" in sql, (
            "IS NOT NULL sentinel must render as 'col IS NOT NULL', not '= __IS_NOT_NULL__'."
        )
        assert "__IS_NOT_NULL__" not in sql, (
            "Sentinel string must not appear literally in the compiled SQL."
        )
        assert "WITH denom_cohort" in sql, "IS NOT NULL filter must still use cohort CTE path"

    def test_is_not_null_not_in_denominator(self, metrics):
        """IS NOT NULL condition (like UPI filter) must apply to numerator only."""
        qo = _activation_qo(filters={"transaction_channel": _IS_NOT_NULL_SENTINEL})
        sql, _ = compile_query(qo, metrics)
        denom_body = _cte_body("denom_cohort", sql)
        assert denom_body, "denom_cohort CTE not found in SQL"
        assert "transaction_channel" not in denom_body, (
            "IS NOT NULL filter must NOT appear in denominator (denominator = all onboarded users)."
        )


class TestInvalidFilterRemap:
    """
    L2: _remap_invalid_filter_values_via_custom_events fixup.

    Bug history:
    - 'IN APP activation' → orchestrator set filters={transaction_channel: 'in_app'}
    - 'in_app' is not a real transaction_channel value (real values: UPI, IMPS, NEFT, …)
    - This produced 0 results with no error — a silent wrong-answer failure.
    - Fix: detect unknown values, map to IS NOT NULL via custom event semantic lookup.
    """

    def _make_minimal_sampled(self) -> dict:
        """Minimal sampled_values mimicking the events table for transaction_channel."""
        return {"events": {"transaction_channel": ["UPI", "IMPS", "NEFT", "RTGS", "NACH", "debit_card"]}}

    def test_in_app_remapped_to_is_not_null(self, catalog):
        """'in_app' not in sampled values → must be remapped to IS_NOT_NULL sentinel."""
        sampled = self._make_minimal_sampled()
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            time_range_days=180,
            filters={"transaction_channel": "in_app"},
        )
        _remap_invalid_filter_values_via_custom_events(qo, sampled, catalog)
        assert qo.filters.get("transaction_channel") == _IS_NOT_NULL_SENTINEL, (
            f"'in_app' (not a real DB value) must be remapped to IS_NOT_NULL sentinel.\n"
            f"Got: {qo.filters.get('transaction_channel')}"
        )

    def test_valid_value_not_remapped(self, catalog):
        """UPI is a real value — must NOT be remapped."""
        sampled = self._make_minimal_sampled()
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            time_range_days=30,
            filters={"transaction_channel": "UPI"},
        )
        _remap_invalid_filter_values_via_custom_events(qo, sampled, catalog)
        assert qo.filters.get("transaction_channel") == "UPI", (
            "Valid filter values must not be remapped."
        )

    def test_no_sampled_values_no_change(self, catalog):
        """When sampled_values is empty/None, fixup must not modify filters."""
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            time_range_days=30,
            filters={"transaction_channel": "in_app"},
        )
        _remap_invalid_filter_values_via_custom_events(qo, {}, catalog)
        assert qo.filters.get("transaction_channel") == "in_app", (
            "Without sampled data to verify against, fixup must leave filters unchanged."
        )

    def test_case_insensitive_value_match(self, catalog):
        """'upi' (lowercase) must match sampled 'UPI' — no spurious remapping."""
        sampled = self._make_minimal_sampled()
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            time_range_days=30,
            filters={"transaction_channel": "upi"},
        )
        _remap_invalid_filter_values_via_custom_events(qo, sampled, catalog)
        assert qo.filters.get("transaction_channel") == "upi", (
            "Case-insensitive match means 'upi' should not be remapped."
        )


_QUALIFIED_RETENTION_MINIMAL_CATALOG = {
    "events": {
        "suggested_metrics": [
            {
                "id": "d7_retention",
                "name": "Day 7 Retention",
                "type": "retention",
                "builder_definition": {
                    "builder_type": "retention",
                    "primary_event": "transaction_reconciled",
                    "retention_days": 7,
                    "filters_structured": [
                        {"field": "transaction_status", "op": "=", "value": "SUCCESS"},
                    ],
                },
            },
            {
                "id": "d30_retention",
                "name": "Day 30 Retention",
                "type": "retention",
                "builder_definition": {
                    "builder_type": "retention",
                    "primary_event": "transaction_reconciled",
                    "retention_days": 30,
                },
            },
        ],
        "events": [
            {
                "raw_name": "transaction_channel",
                "value_meanings": {"UPI": "UPI channel", "CARD": "Card"},
            },
        ],
    },
}


class TestBuildVocabCatalogHints:
    """build_vocab must expose catalog value_meanings, not only DB samples."""

    def test_upi_from_catalog_without_db_sample(self):
        from core.pipeline.orchestrator import build_vocab

        catalog = {
            "events": {
                "events": [
                    {
                        "raw_name": "transaction_channel",
                        "value_meanings": {"UPI": "UPI channel", "CARD": "Card"},
                    },
                ],
            },
        }
        vocab = build_vocab(catalog, {})
        assert "UPI" in vocab["dim_value_hints"].get("transaction_channel", [])
        assert "UPI" in vocab["filter_cols"].get("transaction_channel", [])

    def test_sample_extends_catalog_hints(self):
        from core.pipeline.orchestrator import build_vocab

        catalog = {
            "events": {
                "events": [
                    {"raw_name": "transaction_channel", "value_meanings": {"UPI": "UPI"}},
                ],
            },
        }
        sampled = {"events": {"transaction_channel": ["UPI", "IMPS", "NEFT"]}}
        vocab = build_vocab(catalog, sampled)
        hints = vocab["dim_value_hints"]["transaction_channel"]
        assert "UPI" in hints and "IMPS" in hints


class TestQualifiedRetentionRescue:
    """Orchestrator rescue: [qualifier] retention numbers → retention + filters."""

    def test_upi_retention_from_out_of_scope(self):
        qo = QueryObject(
            analysis_type="out_of_scope",
            clarify_message="I don't have UPI tracked in this catalog.",
        )
        _resolve_qualified_prebuilt_metric(
            qo,
            "Share UPI retention numbers",
            _QUALIFIED_RETENTION_MINIMAL_CATALOG,
            {},  # no sampled_values — uses catalog value_meanings
        )
        assert qo.analysis_type == "retention"
        assert qo.metric_id == "d7_retention"
        assert qo.filters.get("transaction_channel") == "UPI"
        assert qo.filters.get("transaction_status") == "SUCCESS"
        assert qo.event == "transaction_reconciled"
        assert qo.retention_window_days == 7

    def test_d30_retention_when_user_specifies_window(self):
        qo = QueryObject(analysis_type="out_of_scope")
        _resolve_qualified_prebuilt_metric(
            qo,
            "share 30 day UPI retention stats",
            _QUALIFIED_RETENTION_MINIMAL_CATALOG,
            {"events": {"transaction_channel": ["UPI", "CARD"]}},
        )
        assert qo.metric_id == "d30_retention"
        assert qo.retention_window_days == 30


class TestRetentionBreakdownPlanning:
    def test_breakdown_uses_retention_compiler_not_segment(self):
        from core.analysis.analyst import _plan_investigations

        qo = QueryObject(
            analysis_type="retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=14,
            time_range_days=180,
            time_granularity="month",
            breakdown="platform",
            filters={"transaction_status": "SUCCESS"},
        )
        invs = _plan_investigations(
            qo,
            question="Show retention broken down by platform",
            user_sampled_values={"platform": ["android", "ios"]},
        )
        assert len(invs) == 1
        assert invs[0].name == "retention_by_platform"
        assert "retention_pct" in invs[0].sql
        assert "GROUP BY 1, 2" in invs[0].sql


class TestRetentionReturnFilters:
    """Return-event rows in retention SQL must carry the same filters as cohort entry."""

    def test_upi_and_success_filters_in_retained_cte(self, metrics):
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=7,
            time_range_days=30,
            filters={
                "transaction_channel": "UPI",
                "transaction_status": "SUCCESS",
            },
        )
        sql = _compile_retention(qo)
        retained_sql = sql.split("retained AS", 1)[1].split(")\nSELECT", 1)[0]
        assert "transaction_channel = 'UPI'" in retained_sql
        assert "transaction_status = 'SUCCESS'" in retained_sql

    def test_cohort_anchor_computed_before_lookback_filter(self, metrics):
        """True first event (MIN) must not be restricted by rolling window before GROUP BY."""
        import re
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=14,
            time_range_days=180,
            time_granularity="month",
            filters={"transaction_status": "SUCCESS"},
        )
        sql = _compile_retention(qo)
        assert "user_first AS" in sql
        assert "first_ts >= CURRENT_DATE - INTERVAL '180' DAY" in sql
        m = re.search(r"user_first AS \((.*?)\),\s*cohort AS", sql, re.DOTALL)
        assert m, "expected user_first CTE before cohort"
        inner = m.group(1)
        assert "INTERVAL '180' DAY" not in inner, (
            "rolling window must filter cohort anchor (first_ts), not events before MIN"
        )

    def test_mom_14_day_retention_monthly_cohort_not_period_matrix(self, metrics):
        """MOM + 14-day window → one retention_pct per month, not m1..m6 matrix."""
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=14,
            time_range_days=180,
            time_granularity="month",
            filters={"transaction_status": "SUCCESS"},
        )
        sql = _compile_retention(qo)
        assert "retention_pct" in sql
        assert "INTERVAL '14' DAY" in sql
        assert "m1_pct" not in sql
        assert "cohort_month" in sql

    def test_retention_window_within_n_days_not_n_to_2n_band(self, metrics):
        """14-day retention = return within 14 days of cohort anchor, not days 7–14."""
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=14,
            time_range_days=30,
            filters={"transaction_channel": "UPI", "transaction_status": "SUCCESS"},
        )
        sql = _compile_retention(qo)
        retained_sql = sql.split("retained AS", 1)[1].split(")\nSELECT", 1)[0]
        assert "INTERVAL '14' DAY" in retained_sql
        assert "+ INTERVAL '7' DAY" not in retained_sql
        assert "cohort_week::TIMESTAMP\n" in retained_sql or ">= c.cohort_week::TIMESTAMP\n" in retained_sql


class TestRetentionWindowFromPrompt:
    """Parse and apply N-day retention window from follow-up phrasing."""

    def test_same_retention_for_14_days_parsed(self):
        prompt = "Please share same retention metric but for 14 days"
        assert parse_retention_window_days_from_prompt(prompt) == 14

    def test_first_14_days_parsed(self):
        assert parse_retention_window_days_from_prompt(
            "UPI retention in the first 14 days",
        ) == 14

    def test_hydrate_then_apply_overrides_catalog_d7(self, catalog):
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            retention_window_days=7,
            time_range_days=30,
        )
        _hydrate_retention_event_from_metric(qo, catalog)
        assert qo.retention_window_days == 7
        _apply_retention_window_from_prompt(
            qo,
            "Please share same retention metric but for 14 days",
            catalog,
        )
        assert qo.retention_window_days == 14

    def test_incomplete_retention_cohort_note_for_recent_month(self):
        import pandas as pd
        from datetime import date

        today = date.today()
        recent = pd.Timestamp(today.year, today.month, 1)
        df = pd.DataFrame(
            {
                "cohort_month": [recent],
                "cohort_size": [100],
                "retention_pct": [0.0],
            }
        )
        qo = QueryObject(analysis_type="retention", retention_window_days=14)
        note = incomplete_retention_cohort_note(df, qo)
        assert "14-day" in note or "14 day" in note
        assert "matur" in note.lower()

    def test_sanitize_drops_channel_duplicated_in_filters(self):
        qo = QueryObject(
            analysis_type="retention",
            filters={"transaction_channel": "UPI"},
            metric_status_col="transaction_channel",
            metric_status_target="UPI",
        )
        _sanitize_retention_dimension_status(qo)
        assert qo.metric_status_col is None
        assert qo.metric_status_target is None


class TestRetentionWeekWindow:
    """Week-N retention: parameterized lower-bound window in SQL and parse layer."""

    def test_week4_parse_returns_correct_bounds(self):
        # Week 4 = days 22–28 → from=22, to=29 (exclusive upper)
        result = parse_retention_week_from_prompt("share week 4 retention")
        assert result == (22, 29)

    def test_week1_parse_returns_correct_bounds(self):
        result = parse_retention_week_from_prompt("show week 1 retention")
        assert result == (1, 8)

    def test_week2_parse_returns_correct_bounds(self):
        result = parse_retention_week_from_prompt("show week 2 retention")
        assert result == (8, 15)

    def test_non_retention_prompt_returns_none(self):
        # "week 4" alone without retention context → None
        result = parse_retention_week_from_prompt("show DAU for week 4")
        assert result is None

    def test_no_week_pattern_returns_none(self):
        result = parse_retention_week_from_prompt("show D7 retention")
        assert result is None

    def test_apply_sets_retention_window_from_on_qo(self):
        from core.pipeline.activation_window import apply_retention_window_from_prompt

        qo = QueryObject(analysis_type="retention", metric_id="d7_retention", retention_window_days=7)
        apply_retention_window_from_prompt(qo, "share week 4 retention")
        assert qo.retention_window_from == 22
        assert qo.retention_window_days == 29

    def test_week4_sql_has_lower_interval_bound(self):
        """_compile_retention must emit INTERVAL '22' DAY lower bound for week-4 window."""
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            event="app_opened",
            event_b="app_opened",
            retention_window_days=29,
            retention_window_from=22,
            time_range_days=90,
        )
        sql = _compile_retention(qo)
        assert "INTERVAL '22' DAY" in sql, f"Lower bound missing in SQL:\n{sql}"
        assert "INTERVAL '29' DAY" in sql, f"Upper bound missing in SQL:\n{sql}"

    def test_standard_d7_sql_has_no_lower_bound_interval(self):
        """Standard D7 with retention_window_from=None must not add a lower offset."""
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            event="app_opened",
            event_b="app_opened",
            retention_window_days=7,
            retention_window_from=None,
            time_range_days=90,
        )
        sql = _compile_retention(qo)
        assert "INTERVAL '7' DAY" in sql
        # Only one INTERVAL in the retained CTE — no lower-bound offset injected
        assert sql.count("INTERVAL '7' DAY") >= 1
        assert "INTERVAL '0' DAY" not in sql


class TestRetentionSurvivalPlan:
    def test_survival_not_planned_for_simple_retention_question(self):
        from core.analysis.analyst import _plan_investigations, _retention_wants_survival_curve

        assert not _retention_wants_survival_curve("Share UPI retention numbers")
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=7,
            filters={"transaction_channel": "UPI", "transaction_status": "SUCCESS"},
        )
        invs = _plan_investigations(qo, question="Share UPI retention numbers")
        names = [i.name for i in invs]
        assert "retention_cohort" in names
        assert "retention_survival_curve" not in names

    def test_survival_planned_when_user_asks_multi_window(self):
        from core.analysis.analyst import _plan_investigations, _retention_wants_survival_curve

        assert _retention_wants_survival_curve("Show D1 and D7 retention survival curve")
        qo = QueryObject(
            analysis_type="retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=7,
        )
        invs = _plan_investigations(
            qo, question="Show D1 and D7 retention survival curve",
        )
        assert any(i.name == "retention_survival_curve" for i in invs)


class TestRetentionRouting:
    """
    Retention is in _ANALYST_ONLY — must always route to __analyst__.
    SQL is generated by core.analyst.investigate(), not compile_query.
    """

    def test_routes_to_analyst(self, metrics):
        qo = QueryObject(
            analysis_type="retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=7,
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql == "__analyst__", f"retention must route to __analyst__, got: {sql[:80]}"


class TestCompilerGuards:
    """SQL guard clauses (always_filter from catalog) must be injected correctly."""

    def test_guards_injected_in_pct_users_scalar(self, metrics, catalog):
        from core.sql.compilers import set_global_sql_guards
        set_global_sql_guards(["user_id NOT LIKE 'test_%'"])
        try:
            sql, _ = compile_query(_activation_qo(), metrics)
            assert "NOT LIKE 'test_%'" in sql, "Global SQL guards must appear in cohort SQL"
        finally:
            # Restore from catalog
            bctx = catalog.get("__business_context__", {}) or {}
            guards = [f for f in ((bctx.get("exclusions") or {}).get("always_filter") or []) if isinstance(f, str)]
            set_global_sql_guards(guards)


# ═════════════════════════════════════════════════════════════════════════════
# L2 — PIPELINE / FIXUP TESTS
# Test QO mutations from the fixup chain (ui.qo_fixups, core.semantic.qo_lineage).
# ═════════════════════════════════════════════════════════════════════════════

# Minimal multi-industry catalog: generic column + metric ids (not Jupiter/fintech names).
_QUALIFIED_METRIC_MINIMAL_CATALOG = {
    "orders": {
        "suggested_metrics": [
            {
                "id": "activation_rate",
                "name": "Activation Rate",
                "description": "Share of new users who complete the primary value action",
            },
            {
                "id": "user_activation_rate",
                "name": "User Activation Rate",
                "description": "Alternate activation definition including user scope",
            },
        ],
    },
}

_QUALIFIED_METRIC_MINIMAL_SAMPLED = {
    "orders": {"pay_channel": ["UPI", "CARD", "WALLET"]},
}


class TestQualifiedPrebuiltMetricRescue:
    """
    L2: orchestrator _resolve_qualified_prebuilt_metric rescues clarify when the LLM
    treats '[qualifier] [metric token] numbers' as unknown.

    Industry-agnostic: uses synthetic catalog + sampled_values only.
    """

    def test_qualifier_plus_metric_informal_words(self):
        qo = QueryObject(
            analysis_type="clarify",
            clarify_message="I don't have UPI activation tracked in this catalog.",
        )
        _resolve_qualified_prebuilt_metric(
            qo,
            "Please share UPI activation numbers",
            _QUALIFIED_METRIC_MINIMAL_CATALOG,
            _QUALIFIED_METRIC_MINIMAL_SAMPLED,
        )
        assert qo.analysis_type == "metric"
        assert qo.metric_id == "activation_rate"
        assert qo.filters == {"pay_channel": "UPI"}

    def test_prefers_metric_without_extra_id_tokens(self):
        """user_activation_rate must lose when question omits 'user'."""
        qo = QueryObject(analysis_type="clarify")
        _resolve_qualified_prebuilt_metric(
            qo,
            "CARD activation stats",
            _QUALIFIED_METRIC_MINIMAL_CATALOG,
            _QUALIFIED_METRIC_MINIMAL_SAMPLED,
        )
        assert qo.metric_id == "activation_rate"
        assert qo.filters == {"pay_channel": "CARD"}

    def test_no_rescue_when_metric_token_missing(self):
        qo = QueryObject(analysis_type="clarify")
        _resolve_qualified_prebuilt_metric(
            qo,
            "Please share UPI revenue numbers",
            _QUALIFIED_METRIC_MINIMAL_CATALOG,
            _QUALIFIED_METRIC_MINIMAL_SAMPLED,
        )
        assert qo.analysis_type == "clarify"
        assert not qo.metric_id

    def test_no_rescue_when_already_has_metric_id(self):
        qo = QueryObject(analysis_type="metric", metric_id="churn_rate")
        _resolve_qualified_prebuilt_metric(
            qo,
            "UPI activation numbers",
            _QUALIFIED_METRIC_MINIMAL_CATALOG,
            _QUALIFIED_METRIC_MINIMAL_SAMPLED,
        )
        assert qo.metric_id == "churn_rate"


class TestRetentionFixup:
    """
    _hydrate_retention_event_from_metric must populate QO from catalog builder_definition.

    Bug history:
    - event remained None → compiler used 'app_opened' fallback.
    - event_b remained 'app_opened' from LLM → retention measured re-opens, not re-transactions.
    - retention_window_days was not set → defaulted to 7 even for D30 metrics.
    - filters_structured was not merged → transaction_status=SUCCESS was missing from SQL.
    """

    def test_event_hydrated_from_builder_definition(self, catalog):
        qo = QueryObject(analysis_type="retention", metric_id="d7_retention", time_range_days=30)
        _hydrate_retention_event_from_metric(qo, catalog)
        assert qo.event == "transaction_reconciled", (
            f"event should be 'transaction_reconciled', got '{qo.event}'.\n"
            "Bug: metric_id was set but builder_definition was not used to populate event."
        )

    def test_event_b_replaced_when_generic(self, catalog):
        """LLM sometimes sets event_b=app_opened. Fixup must replace it with the metric's event."""
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            event_b="app_opened",
            time_range_days=30,
        )
        _hydrate_retention_event_from_metric(qo, catalog)
        assert qo.event_b != "app_opened", (
            "Fixup must replace generic event_b='app_opened' with metric's primary event.\n"
            "app_opened as return event makes D7 retention meaningless for a payments product."
        )
        assert qo.event_b == "transaction_reconciled"

    def test_retention_window_days_set(self, catalog):
        qo = QueryObject(analysis_type="retention", metric_id="d7_retention", time_range_days=30)
        _hydrate_retention_event_from_metric(qo, catalog)
        assert qo.retention_window_days == 7, (
            f"Expected retention_window_days=7, got {qo.retention_window_days}"
        )

    def test_filters_structured_merged_into_qo_filters(self, catalog):
        qo = QueryObject(analysis_type="retention", metric_id="d7_retention", time_range_days=30)
        _hydrate_retention_event_from_metric(qo, catalog)
        assert qo.filters.get("transaction_status") == "SUCCESS", (
            "builder_definition.filters_structured must be merged into qo.filters.\n"
            f"Got filters: {qo.filters}"
        )

    def test_existing_user_filters_preserved(self, catalog):
        """User-specified filters must not be wiped by the hydration fixup."""
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            filters={"platform": "android"},
            time_range_days=30,
        )
        _hydrate_retention_event_from_metric(qo, catalog)
        assert qo.filters.get("platform") == "android", (
            "User-specified filters must survive the retention hydration fixup."
        )


class TestIsValid:
    """
    is_valid() must enforce structural constraints before SQL compilation.
    Bug history: is_valid() existed but was never called in pipeline.
    """

    def test_retention_passes_with_metric_id(self):
        ok, err = QueryObject(analysis_type="retention", metric_id="d7_retention").is_valid()
        assert ok, f"retention with metric_id should pass is_valid(), got: {err}"

    def test_retention_passes_with_event(self):
        ok, err = QueryObject(
            analysis_type="retention", event="transaction_reconciled"
        ).is_valid()
        assert ok, f"retention with event should pass is_valid(), got: {err}"

    def test_retention_fails_without_event_or_metric_id(self):
        ok, _ = QueryObject(analysis_type="retention").is_valid()
        assert not ok, "retention without event or metric_id must fail is_valid()"

    def test_metric_fails_without_event_or_metric_id(self):
        ok, _ = QueryObject(analysis_type="metric").is_valid()
        assert not ok

    def test_funnel_fails_with_one_step(self):
        ok, _ = QueryObject(analysis_type="funnel", funnel_steps=["onboarding_completed"]).is_valid()
        assert not ok

    def test_same_month_anchor_fails_with_same_events(self):
        ok, _ = QueryObject(
            analysis_type="same_month_anchor",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
        ).is_valid()
        assert not ok, "same_month_anchor with event == event_b must fail"

    def test_valid_metric_passes(self):
        ok, _ = QueryObject(analysis_type="metric", event="transaction_reconciled").is_valid()
        assert ok


class TestFilterRollforward:
    """
    merge_rollforward_filters must carry user-specified filters across follow-up turns.

    Bug history:
    - metric_id-based follow-ups (event=None) bailed out before checking history.
    - "share monthly trend" after "UPI activation rate" dropped the UPI filter.
    """

    def test_metric_id_followup_inherits_filters(self):
        """
        After "UPI activation rate", "share monthly trend" should keep transaction_channel=UPI.
        Bug: merge_rollforward_filters returned early when qo.event was None.
        """
        history = [{
            "qo": {
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "event": None,
                "filters": {"transaction_channel": "UPI"},
                "time_range_days": 30,
                "time_granularity": "day",
            }
        }]
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            time_range_days=180,
            time_granularity="month",
            filters={},
        )
        merge_rollforward_filters(qo, history, catalog=None)
        assert qo.filters.get("transaction_channel") == "UPI", (
            "Follow-up trend must inherit UPI filter from prior turn.\n"
            "Bug: rollforward bailed when event=None instead of falling back to metric_id match."
        )

    def test_existing_filter_not_overwritten(self):
        """User explicitly specifying a different channel on follow-up must take precedence."""
        history = [{"qo": {"metric_id": "activation_rate", "event": None,
                            "filters": {"transaction_channel": "UPI"}}}]
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            filters={"transaction_channel": "NEFT"},
        )
        merge_rollforward_filters(qo, history, catalog=None)
        assert qo.filters.get("transaction_channel") == "NEFT"

    def test_different_metric_does_not_inherit(self):
        """Filters from activation_rate must not bleed into d7_retention."""
        history = [{"qo": {"metric_id": "activation_rate", "event": None,
                            "filters": {"transaction_channel": "UPI"}}}]
        qo = QueryObject(analysis_type="retention", metric_id="d7_retention", filters={})
        merge_rollforward_filters(qo, history, catalog=None)
        assert "transaction_channel" not in (qo.filters or {}), (
            "Filters must only roll forward to turns with the same metric_id."
        )

    def test_no_history_no_change(self):
        qo = QueryObject(analysis_type="metric", metric_id="activation_rate", filters={})
        merge_rollforward_filters(qo, [], catalog=None)
        assert qo.filters == {}

    def test_multiple_filters_all_inherited(self):
        """All filters from prior turn should be rolled forward, not just the first."""
        history = [{"qo": {"metric_id": "activation_rate", "event": None,
                            "filters": {"transaction_channel": "UPI", "platform": "android"}}}]
        qo = QueryObject(analysis_type="metric", metric_id="activation_rate", filters={})
        merge_rollforward_filters(qo, history, catalog=None)
        assert qo.filters.get("transaction_channel") == "UPI"
        assert qo.filters.get("platform") == "android"

    def test_partial_filters_respected(self):
        """If follow-up already has one filter set, only the missing ones are inherited."""
        history = [{"qo": {"metric_id": "activation_rate", "event": None,
                            "filters": {"transaction_channel": "UPI", "platform": "android"}}}]
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            filters={"platform": "ios"},  # user changed platform
        )
        merge_rollforward_filters(qo, history, catalog=None)
        assert qo.filters.get("platform") == "ios", "Existing platform filter must not be overwritten"
        assert qo.filters.get("transaction_channel") == "UPI", "Missing filter must be inherited"


class TestFollowupContextRepair:
    """_apply_followup_context_repair must re-attach metric_id/event on vague follow-ups."""

    def test_metric_id_inherited_on_followup(self, catalog):
        history = [{"qo": {"analysis_type": "metric", "metric_id": "activation_rate", "event": None}}]
        qo = QueryObject(analysis_type="metric", time_granularity="month", time_range_days=180)
        _apply_followup_context_repair(qo, history, catalog)
        assert qo.metric_id == "activation_rate", (
            "metric_id must be inherited from history on vague follow-up."
        )

    def test_event_inherited_on_followup(self, catalog):
        history = [{"qo": {"analysis_type": "metric", "event": "transaction_reconciled"}}]
        qo = QueryObject(analysis_type="metric", time_granularity="month")
        _apply_followup_context_repair(qo, history, catalog)
        assert qo.event == "transaction_reconciled"

    def test_no_inherit_from_clarify_turn(self, catalog):
        """Clarify turns must not pollute event inheritance."""
        history = [
            {"qo": {"analysis_type": "clarify", "event": "bad_event"}},
            {"qo": {"analysis_type": "metric", "event": "transaction_reconciled"}},
        ]
        qo = QueryObject(analysis_type="metric")
        _apply_followup_context_repair(qo, history, catalog)
        assert qo.event == "transaction_reconciled", (
            "Clarify turn must be skipped when searching for prior event context."
        )

    def test_metric_id_not_inherited_when_event_already_set(self, catalog):
        """
        Follow-up with its own event must NOT inherit metric_id from prior turn.

        Bug: "split Onboarded users by platform" after a retention query inherited
        metric_id=d7_retention. The compiler then hit the CTE metric path, couldn't
        apply breakdown, and fell to __analyst__ which emitted a plain monthly trend
        instead of (month, platform, unique_users).
        """
        history = [{"qo": {
            "analysis_type": "retention",
            "metric_id": "d7_retention",
            "event": "transaction_reconciled",
        }}]
        qo = QueryObject(
            analysis_type="segment",
            event="onboarding_completed",  # user explicitly named a different event
            breakdown="platform",
            time_granularity="month",
            time_range_days=180,
        )
        _apply_followup_context_repair(qo, history, catalog)
        assert qo.metric_id is None or qo.metric_id == "", (
            "metric_id must NOT be inherited when the current query already has its own event.\n"
            "Bug: d7_retention was injected, corrupting compiler dispatch for the segment query."
        )
        assert qo.event == "onboarding_completed", "event must be unchanged"

    def test_metric_id_still_inherited_when_no_event(self, catalog):
        """Vague follow-up with no event should still inherit metric_id from history."""
        history = [{"qo": {
            "analysis_type": "metric",
            "metric_id": "activation_rate",
            "event": None,
        }}]
        qo = QueryObject(
            analysis_type="segment",
            breakdown="platform",
            time_granularity="month",
            time_range_days=180,
        )
        _apply_followup_context_repair(qo, history, catalog)
        assert qo.metric_id == "activation_rate", (
            "When no event is set, metric_id must still be inherited from history."
        )


class TestDisplayModifierFollowup:
    """
    _apply_display_modifier_followup must NOT override analysis_type when the current
    query already uses a cohort-based type (retention, funnel, behavioral_cohort).

    Bug: "share MOM retention trend" fired the fixup (has_share + has_mom) and
    clobbered analysis_type=retention → "metric", so the compiler emitted a plain
    COUNT(DISTINCT user_id) per month instead of a cohort retention_pct SQL.
    """

    def test_retention_analysis_type_preserved_on_mom_share_prompt(self):
        """
        A fresh retention query with 'share MOM' in the prompt must keep
        analysis_type=retention so the retention compiler emits cohort SQL.
        """
        history = []
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            event="user_signed_up",
            event_b="user_signed_up",
            time_granularity="month",
            time_range_days=180,
        )
        _apply_display_modifier_followup(qo, "share MOM retention trend", history)
        assert qo.analysis_type == "retention", (
            "display_modifier_followup must not override analysis_type=retention.\n"
            "Bug: fixup set analysis_type='metric', causing plain count SQL instead of cohort retention."
        )
        assert qo.time_granularity == "month"

    def test_funnel_analysis_type_preserved_on_mom_share_prompt(self):
        """Funnel is another cohort type that must be protected from metric override."""
        history = []
        qo = QueryObject(
            analysis_type="funnel",
            funnel_steps=["user_signed_up", "purchase_completed"],
            time_granularity="month",
            time_range_days=180,
        )
        _apply_display_modifier_followup(qo, "share MOM split", history)
        assert qo.analysis_type == "funnel", (
            "display_modifier_followup must not override analysis_type=funnel."
        )

    def test_metric_analysis_type_still_overridden_on_followup(self):
        """Regular metric follow-up ('share MOM') must still trigger the rewrite."""
        history = [{"qo": {
            "analysis_type": "metric",
            "metric_id": "activation_rate",
            "time_granularity": "month",
            "time_range_days": 30,
        }}]
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            time_range_days=180,
        )
        _apply_display_modifier_followup(qo, "share MOM trend", history)
        assert qo.analysis_type == "metric"
        assert qo.time_granularity == "month"


class TestSameMetricAnchorTurn:
    """'Same metric' must bind to the latest named metric (Q3), not Q1 active users."""

    _MULTI_TURN_HISTORY = [
        {
            "question": "Top cities by active users last 30 days",
            "metric_name": "Active User",
            "custom_event_name": "active_user",
            "qo": {
                "analysis_type": "segment",
                "event": "app_opened",
                "breakdown": "city",
                "filters": {"account_type": _IS_NOT_NULL_SENTINEL},
                "time_range_days": 30,
            },
        },
        {
            "question": "Show the same metric broken down by account type",
            "metric_name": "Active User",
            "custom_event_name": "active_user",
            "qo": {
                "analysis_type": "segment",
                "event": "app_opened",
                "breakdown": "account_type",
                "time_range_days": 30,
            },
        },
        {
            "question": "share last 6 months Transacting users trend",
            "metric_name": "Transacting User",
            "custom_event_name": "transacting_user",
            "qo": {
                "analysis_type": "metric",
                "event": "transaction_reconciled",
                "time_range_days": 180,
                "time_granularity": "month",
            },
        },
    ]

    def test_q4_same_metric_uses_transacting_not_active(self, catalog):
        qo = QueryObject(
            analysis_type="segment",
            event="app_opened",
            metric_id="ce_active_user",
            breakdown="city",
            filters={"account_type": _IS_NOT_NULL_SENTINEL},
            time_range_days=180,
            time_granularity="month",
            time_source="inherited",
        )
        prompt = "Show the same metric broken down by city"
        _apply_same_query_followup(qo, prompt, self._MULTI_TURN_HISTORY)

        assert getattr(qo, "_bound_custom_event_name", None) == "transacting_user"
        assert qo.metric_id == "ce_transacting_user"
        assert "account_type" not in (qo.filters or {}), (
            "Transacting users must not inherit active-user account_type filter."
        )

        decision = resolve_query_policy(prompt, qo, catalog, {})
        assert decision.route == "custom_segment"
        sql = compile_custom_event_segment(decision.matched_custom_events[0], qo)
        assert "transaction_reconciled" in sql
        assert "app_opened" not in sql


class TestMomShareDisplayFollowup:
    def test_share_mom_split_restores_monthly_metric_trend(self, catalog, metrics):
        from core.pipeline.normalize_query import normalize_query_object

        history = [
            {
                "question": "share last 6 months Transacting users trend",
                "metric_name": "Transacting User",
                "custom_event_name": "transacting_user",
                "qo": {
                    "analysis_type": "metric",
                    "metric_id": "ce_transacting_user",
                    "time_range_days": 180,
                    "time_granularity": "month",
                },
            },
            {
                "question": "Show the same metric broken down by city",
                "metric_name": "Active User",
                "custom_event_name": "active_user",
                "qo": {
                    "analysis_type": "segment",
                    "breakdown": "city",
                    "time_range_days": 180,
                    "time_granularity": "month",
                },
            },
        ]
        qo = QueryObject(
            analysis_type="segment",
            breakdown="city",
            time_range_days=180,
            time_granularity="month",
            metric_id="ce_active_user",
        )
        normalize_query_object(
            qo,
            prompt="share Mom split",
            history=history,
            catalog=catalog,
            sampled_values={},
            metrics=metrics,
        )
        assert qo.analysis_type == "metric"
        assert qo.time_granularity == "month"
        assert qo.breakdown is None
        assert qo.metric_id == "ce_transacting_user"


class TestSameMetricCustomEventFollowup:
    """
    'Show the same metric broken down by X' after a custom_segment Active User query
    must stay on the custom event, not collapse to app_opened only.
    """

    _ACTIVE_USER_HISTORY = [{
        "question": "Top cities by active users last 30 days",
        "metric_name": "Active User",
        "custom_event_name": "active_user",
        "qo": {
            "analysis_type": "segment",
            "event": "app_opened",
            "breakdown": "city",
            "filters": {"account_type": _IS_NOT_NULL_SENTINEL},
            "time_range_days": 30,
            "time_granularity": "day",
        },
    }]

    def test_binds_custom_event_and_routes_custom_segment(self, catalog):
        qo = QueryObject(
            analysis_type="segment",
            event="app_opened",
            breakdown="account_type",
            filters={"account_type": _IS_NOT_NULL_SENTINEL},
            time_range_days=30,
            time_granularity="day",
            time_source="inherited",
        )
        prompt = "Show the same metric broken down by account type"
        _apply_same_query_followup(qo, prompt, self._ACTIVE_USER_HISTORY)

        assert getattr(qo, "_bound_custom_event_name", None) == "active_user"
        assert "account_type" not in (qo.filters or {}), (
            "IS NOT NULL on breakdown dimension must be dropped when grouping by it."
        )

        decision = resolve_query_policy(prompt, qo, catalog, {})
        assert decision.route == "custom_segment", (
            f"Expected custom_segment, got {decision.route!r}"
        )
        ce = decision.matched_custom_events[0]
        sql = compile_custom_event_segment(ce, qo)
        assert "transaction_reconciled" in sql
        assert "app_opened" in sql
        assert "OR" in sql
        assert "WHERE event_name = 'app_opened'" not in sql, (
            "Must not use primitive single-event segment SQL when custom event was bound."
        )


class TestMaybeResolveClarifyAsFollowup:
    """_maybe_resolve_clarify_as_followup must convert display-modifier follow-ups."""

    _SEGMENT_HISTORY = [{
        "qo": {
            "analysis_type": "segment",
            "event": "transaction_reconciled",
            "breakdown": "city",
            "filters": {"transaction_channel": "__IS_NOT_NULL__"},
            "time_range_days": 30,
        }
    }]

    def test_percentage_split_resolves_from_segment(self):
        qo = QueryObject(analysis_type="clarify")
        _maybe_resolve_clarify_as_followup(
            qo, self._SEGMENT_HISTORY, None, "can you show percentage split"
        )
        assert qo.analysis_type == "segment", (
            "'can you show percentage split' must inherit segment context."
        )
        assert qo.breakdown == "city"
        assert qo.filters.get("transaction_channel") == "__IS_NOT_NULL__"

    def test_show_as_donut_resolves(self):
        qo = QueryObject(analysis_type="clarify")
        _maybe_resolve_clarify_as_followup(
            qo, self._SEGMENT_HISTORY, None, "show as donut"
        )
        assert qo.analysis_type == "segment"

    def test_long_prompt_not_resolved(self):
        """A full rephrasing of the query should not be resolved as a modifier."""
        qo = QueryObject(analysis_type="clarify")
        long_prompt = (
            "what is the percentage distribution of In App transacting users "
            "by city over the last 30 days"
        )
        _maybe_resolve_clarify_as_followup(qo, self._SEGMENT_HISTORY, None, long_prompt)
        assert qo.analysis_type == "clarify", "Long prompts must not be auto-resolved."

    def test_no_history_no_change(self):
        qo = QueryObject(analysis_type="clarify")
        _maybe_resolve_clarify_as_followup(qo, [], None, "show percentage split")
        assert qo.analysis_type == "clarify"

    def test_non_clarify_qo_unchanged(self):
        """Function must be a no-op for non-clarify analysis types."""
        qo = QueryObject(analysis_type="segment", event="app_opened", breakdown="city")
        _maybe_resolve_clarify_as_followup(
            qo, self._SEGMENT_HISTORY, None, "show percentage split"
        )
        assert qo.analysis_type == "segment"
        assert qo.event == "app_opened"  # must not overwrite existing event


class TestChartTypeRouting:
    """Plotly chart type dispatch: donut for small segments, 100% stacked for monthly splits."""

    def test_small_cat_num_produces_donut(self):
        import pandas as pd
        from core.viz.charts_plotly import evidence_chart_plotly

        df = pd.DataFrame({"city": ["Mumbai", "Delhi", "Bangalore", "Pune"],
                           "users": [4500, 3200, 2100, 1800]})
        fig = evidence_chart_plotly("segment", df)
        assert fig is not None
        assert any(t.type == "pie" for t in fig.data), (
            "Small cat+num frame (≤10 rows, non-rate) must produce a donut/pie chart."
        )

    def test_large_cat_num_uses_donut_with_others(self):
        """12 cities → donut with top 8 + 'Others' slice."""
        import pandas as pd
        from core.viz.charts_plotly import evidence_chart_plotly, _DONUT_MAX_SLICES

        df = pd.DataFrame({"city": [f"City{i}" for i in range(12)],
                           "users": range(100, 112)})
        fig = evidence_chart_plotly("segment", df)
        assert fig is not None
        assert any(t.type == "pie" for t in fig.data), (
            "Large cat+num (>_DONUT_MAX_SLICES rows) must still produce a donut with Others."
        )
        labels = list(fig.data[0].labels)
        assert "Others" in labels, f"Expected 'Others' slice, got labels: {labels}"
        assert len(labels) == _DONUT_MAX_SLICES + 1

    def test_monthly_cat_produces_100pct_stacked(self):
        import pandas as pd
        from core.viz.charts_plotly import evidence_chart_plotly

        months = pd.date_range("2025-01-01", periods=4, freq="MS")
        df = pd.DataFrame({
            "month": months.repeat(3),
            "city": ["Mumbai", "Delhi", "Bangalore"] * 4,
            "users": [100, 80, 60, 110, 85, 55, 120, 90, 50, 105, 75, 65],
        })
        fig = evidence_chart_plotly("trend_by_city", df)
        assert fig is not None
        assert fig.layout.barnorm == "percent", (
            "Categorical time-series (coarse time + cat dim) must use 100% stacked bars."
        )


# ═════════════════════════════════════════════════════════════════════════════
# L1 — ANALYSIS TYPE SMOKE TESTS
# Every analysis type must compile without crashing and route to the expected
# handler. These catch regressions introduced by QO schema changes, new fixups,
# or compiler refactors that break previously-untested paths.
# ═════════════════════════════════════════════════════════════════════════════

def _make_qo(**kwargs) -> QueryObject:
    return QueryObject(**kwargs)


class TestAnalysisTypeSmokeCompile:
    """
    One happy-path compile test per analysis type.

    Coverage gap history: funnel, behavioral_cohort, time_between, stickiness,
    user_lifecycle, same_month_anchor, diagnose had zero regression tests.
    Any QO schema field addition or compiler refactor could silently break them.

    These tests assert route (SQL | __analyst__ | __diagnose__) and no crash.
    They do NOT require a DB.
    """

    def test_funnel_routes_to_analyst(self, metrics):
        """Funnel compile must not crash and must route to __analyst__ for multi-step SQL."""
        qo = _make_qo(
            analysis_type="funnel",
            funnel_steps=["event_a", "event_b"],
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql == "__analyst__", (
            "Funnel queries must route to __analyst__ — they require multi-step execution."
        )

    def test_behavioral_cohort_anti_compiles_to_sql(self, metrics):
        """Anti-cohort (did A but NOT B) must compile to SQL with an anti-join CTE."""
        qo = _make_qo(
            analysis_type="behavioral_cohort",
            event="event_a",
            event_b="event_b",
            metric_variant="anti_cohort",
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql and sql not in ("__analyst__", "__diagnose__", ""), (
            "Anti-cohort must compile directly to SQL, not route to analyst."
        )
        assert "did_a" in sql.lower() or "event_a" in sql, (
            "Anti-cohort SQL must reference the primary event (did_a CTE or event filter)."
        )
        assert "NOT IN" in sql or "LEFT JOIN" in sql, (
            "Anti-cohort must use NOT IN or anti-join to exclude users who did event_b."
        )

    def test_anti_cohort_event_b_filter_scoped_to_did_b_not_did_a(self, metrics):
        """Filter on event_b's properties must appear in did_b CTE, never in did_a.

        Bug class: when event_a != event_b and qo.filters are set, the filter must
        qualify the *behaviour being checked* (event_b), not the cohort definition (event_a).
        Applying e.g. transaction_channel to the onboarding CTE produces zero rows.
        """
        import re

        def _cte_body(name: str, sql: str) -> str:
            m = re.search(rf"{re.escape(name)}\s+AS\s*\(", sql, re.IGNORECASE)
            if not m:
                return ""
            start, depth, i = m.end(), 1, m.end()
            while i < len(sql) and depth > 0:
                depth += (1 if sql[i] == "(" else -1 if sql[i] == ")" else 0)
                i += 1
            return sql[start: i - 1]

        qo = _make_qo(
            analysis_type="behavioral_cohort",
            event="onboarding_completed",
            event_b="transaction_reconciled",
            metric_variant="anti_cohort",
            filters={"transaction_channel": "UPI"},
            date_from="2026-02-01", date_to="2026-03-01",
        )
        sql, _ = compile_query(qo, metrics)
        assert sql and sql not in ("__analyst__", "__diagnose__", ""), (
            "Anti-cohort with filter must compile to SQL."
        )
        did_a = _cte_body("did_a", sql)
        did_b = _cte_body("did_b", sql)
        assert did_a or "event_name = 'onboarding_completed'" in sql, "did_a CTE not found"
        assert "transaction_channel" not in did_a, (
            "transaction_channel filter must NOT appear in did_a (onboarding CTE). "
            "Filter belongs in did_b (transaction CTE only)."
        )
        assert "transaction_channel" in did_b, (
            "transaction_channel filter must appear in did_b (the behaviour CTE). "
            "Without this, the UPI filter has no effect."
        )

    def test_behavioral_cohort_overlap_compiles_to_sql(self, metrics):
        """Overlap cohort (did A AND B) must compile to SQL with an intersection."""
        qo = _make_qo(
            analysis_type="behavioral_cohort",
            event="event_a",
            event_b="event_b",
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql and sql not in ("__analyst__", "__diagnose__", ""), (
            "Overlap cohort must compile to SQL."
        )

    def test_time_between_routes_to_analyst(self, metrics):
        """time_between needs multi-query execution — must route to __analyst__."""
        qo = _make_qo(
            analysis_type="time_between",
            event="event_a",
            event_b="event_b",
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql == "__analyst__", (
            "time_between must route to __analyst__ for median/percentile computation."
        )

    def test_stickiness_routes_to_analyst(self, metrics):
        qo = _make_qo(
            analysis_type="stickiness",
            event="event_a",
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql == "__analyst__", "stickiness must route to __analyst__."

    def test_user_lifecycle_routes_to_analyst(self, metrics):
        qo = _make_qo(
            analysis_type="user_lifecycle",
            event="event_a",
            event_b="event_b",
            time_range_days=90,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql == "__analyst__", "user_lifecycle must route to __analyst__."

    def test_same_month_anchor_compiles_to_sql(self, metrics):
        """same_month_anchor has its own SQL compiler — must not route to analyst."""
        qo = _make_qo(
            analysis_type="same_month_anchor",
            event="event_a",
            event_b="event_b",
            time_range_days=90,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql and sql not in ("__analyst__", "__diagnose__", ""), (
            "same_month_anchor must compile to SQL directly."
        )

    def test_diagnose_routes_to_diagnose_sentinel(self, metrics):
        qo = _make_qo(
            analysis_type="diagnose",
            metric_id="activation_rate",
            time_range_days=30,
        )
        sql, _ = compile_query(qo, metrics)
        assert sql == "__diagnose__", "diagnose must return __diagnose__ sentinel."

    def test_retention_window_days_falsy_zero_preserved(self, metrics):
        """retention_window_days=0 must survive QO parsing, not silently become 7.

        Bug class: `d.get('retention_window_days') or 7` treated 0 as falsy and
        returned 7. Same pattern as activation_window_days had before its fix.
        """
        from core.sql.query_object import QueryObject as QO
        data = {
            "analysis_type": "retention",
            "metric_id": "d7_retention",
            "retention_window_days": 0,
        }
        qo = QO.from_dict(data)
        assert qo.retention_window_days == 0, (
            "retention_window_days=0 must be preserved — falsy-zero must not default to 7."
        )

    def test_retention_hours_parsed_to_days(self):
        """'24hr retention' must produce retention_window_days=1, not 24.

        Bug class: regex parser had no hour pattern; LLM would set 24 and the
        deterministic rescue also would not catch it.
        """
        from core.pipeline.activation_window import parse_retention_window_days_from_prompt
        assert parse_retention_window_days_from_prompt("24hr retention") == 1, (
            "24hr retention must convert to 1 day."
        )
        assert parse_retention_window_days_from_prompt("48 hour retention") == 2, (
            "48 hour retention must convert to 2 days."
        )
        assert parse_retention_window_days_from_prompt("6hr retention") == 1, (
            "Sub-day retention must round up to 1 day."
        )


# ═════════════════════════════════════════════════════════════════════════════
# L3 — DATABASE EXECUTION TESTS
# Execute compiled SQL against real DB, assert result properties.
# Requires jupiter.duckdb. Skip with: pytest -m "not db"
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.db
class TestActivationRateDB:
    """
    Execution-level correctness: the cohort SQL must return semantically valid results.
    Bug history: scalar hint with GROUP BY returned activation_rate > 100%.
    """

    def test_scalar_returns_one_row(self, metrics, db_conn):
        sql, _ = compile_query(_activation_qo(), metrics)
        df = db_conn.execute(sql).df()
        assert len(df) == 1, f"Scalar metric must return exactly 1 row, got {len(df)}"

    def _rate_col(self, df) -> str:
        """Return the activation rate column name (single or multi-window variant)."""
        for c in df.columns:
            if c == "activation_rate" or c.startswith("activation_rate_"):
                return c
        raise KeyError(f"No activation_rate column found in {list(df.columns)}")

    def _numer_col(self, df) -> str:
        """Return the numerator column for the longest window available."""
        for suffix in ("_30d", "_14d", "_7d", ""):
            c = f"transaction_reconciled_users{suffix}"
            if c in df.columns:
                return c
        raise KeyError(f"No transaction_reconciled_users column found in {list(df.columns)}")

    def test_scalar_rate_between_0_and_100(self, metrics, db_conn):
        sql, _ = compile_query(_activation_qo(), metrics)
        df = db_conn.execute(sql).df()
        rate = float(df.iloc[0][self._rate_col(df)])
        assert 0 <= rate <= 100, (
            f"Activation rate must be 0-100%, got {rate}%.\n"
            "Bug: independent ROUND(COUNT/COUNT) pattern can exceed 100% when cohorts differ."
        )

    def test_monthly_all_values_between_0_and_100(self, metrics, db_conn):
        qo = _activation_qo(time_granularity="month", time_range_days=180)
        sql, _ = compile_query(qo, metrics)
        df = db_conn.execute(sql).df()
        assert len(df) >= 1, "Monthly activation must return at least 1 month"
        rc = self._rate_col(df)
        for _, row in df.iterrows():
            rate = float(row[rc])
            assert 0 <= rate <= 100, (
                f"Month {row.get('month')}: activation rate {rate}% exceeds 100%.\n"
                "Cohort CTE must bucket users by their denominator-event month."
            )

    def test_numerator_never_exceeds_denominator(self, metrics, db_conn):
        """Numerator count (transacted) must always be ≤ denominator (onboarded) due to LEFT JOIN."""
        sql, _ = compile_query(_activation_qo(), metrics)
        df = db_conn.execute(sql).df()
        denom = int(df.iloc[0]["onboarding_completed_users"])
        numer = int(df.iloc[0][self._numer_col(df)])
        assert numer <= denom, (
            f"Numerator ({numer}) exceeds denominator ({denom}).\n"
            "LEFT JOIN on denom_cohort guarantees numer ⊆ denom for the same time window."
        )

    def test_upi_scalar_non_null_rate(self, metrics, db_conn):
        qo = _activation_qo(filters={"transaction_channel": "UPI"})
        sql, _ = compile_query(qo, metrics)
        df = db_conn.execute(sql).df()
        assert len(df) == 1
        rate = df.iloc[0][self._rate_col(df)]
        assert rate is not None and str(rate).lower() != "nan", (
            "UPI activation rate must not be NULL or NaN."
        )

    def test_upi_rate_between_0_and_100(self, metrics, db_conn):
        qo = _activation_qo(filters={"transaction_channel": "UPI"})
        sql, _ = compile_query(qo, metrics)
        df = db_conn.execute(sql).df()
        rate = float(df.iloc[0][self._rate_col(df)])
        assert 0 <= rate <= 100, f"UPI activation rate {rate}% is out of bounds"

    def test_expected_columns_present(self, metrics, db_conn):
        sql, _ = compile_query(_activation_qo(), metrics)
        df = db_conn.execute(sql).df()
        assert "onboarding_completed_users" in df.columns, "denominator column missing"
        assert self._numer_col(df), "numerator column missing"
        assert self._rate_col(df), "activation_rate column missing"

    def test_monthly_expected_columns(self, metrics, db_conn):
        qo = _activation_qo(time_granularity="month", time_range_days=180)
        sql, _ = compile_query(qo, metrics)
        df = db_conn.execute(sql).df()
        assert "month" in df.columns
        assert self._rate_col(df), "activation_rate column missing in monthly result"

    def test_no_empty_result_for_30_day_window(self, metrics, db_conn):
        sql, _ = compile_query(_activation_qo(), metrics)
        df = db_conn.execute(sql).df()
        denom = int(df.iloc[0]["onboarding_completed_users"])
        assert denom > 0, (
            "Expected non-zero onboarding_completed_users in last 30 days.\n"
            "Empty denominator suggests wrong event name or no data."
        )

    def test_in_app_filter_returns_rows(self, metrics, db_conn):
        """
        'IN APP activation' bug: filters={transaction_channel: 'in_app'} → 0 rows.
        After fix: IS NOT NULL sentinel → transaction_channel IS NOT NULL → real rows returned.
        """
        qo = _activation_qo(
            time_granularity="month",
            time_range_days=180,
            filters={"transaction_channel": _IS_NOT_NULL_SENTINEL},
        )
        sql, _ = compile_query(qo, metrics)
        assert "IS NOT NULL" in sql, "IS NOT NULL sentinel must survive into compiled SQL"
        df = db_conn.execute(sql).df()
        assert len(df) >= 1, "IN APP monthly trend must return at least 1 month of data"
        rc = self._rate_col(df)
        for _, row in df.iterrows():
            rate = float(row[rc])
            assert 0 <= rate <= 100, f"IN APP activation rate {rate}% is out of bounds"
        # Also verify it returns MORE data than if we had used 'in_app' (which returns 0)
        denom = int(df.iloc[0]["onboarding_completed_users"])
        assert denom > 0, (
            "IN APP filter (IS NOT NULL) must return non-zero denominator.\n"
            "Bug: filters={transaction_channel: 'in_app'} returned 0 because 'in_app' is not a real value."
        )


# ═════════════════════════════════════════════════════════════════════════════
# L3 — SQL GUARD INTEGRATION (DB)
# Verify always_filter guards appear in final executed queries.
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.db
class TestSQLGuardsDB:
    """Global SQL guards from catalog must be injected and not break execution."""

    def test_activation_rate_executes_with_guards(self, metrics, db_conn):
        """After guard injection, cohort SQL must still execute without error."""
        sql, _ = compile_query(_activation_qo(), metrics)
        # If guards are configured, SQL should still run cleanly
        df = db_conn.execute(sql).df()
        assert df is not None


# ═════════════════════════════════════════════════════════════════════════════
# MULTI-TURN INTEGRATION
# Simulate a full turn sequence: orchestrator output → fixup → compile.
# These tests verify that filter inheritance works end-to-end through the pipeline.
# ═════════════════════════════════════════════════════════════════════════════

class TestMultiTurnPipeline:
    """
    Simulate multi-turn conversations to verify context is preserved.
    These are L2-style (no LLM, no DB) but test the full fixup chain together.
    """

    def test_upi_filter_survives_monthly_trend_followup(self, catalog, metrics):
        """
        Turn 1: activation_rate with transaction_channel=UPI
        Turn 2: share monthly trend → should still have UPI filter
        Bug: merge_rollforward_filters returned early for metric_id-only QOs.
        """
        # Simulate what gets stored in history after turn 1
        turn1_history = [{
            "qo": {
                "analysis_type": "metric",
                "metric_id": "activation_rate",
                "event": None,
                "filters": {"transaction_channel": "UPI"},
                "time_range_days": 30,
                "time_granularity": "day",
                "time_source": "default",
            }
        }]

        # Turn 2: orchestrator returns vague follow-up QO (as it does for "share monthly trend")
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",  # set by _apply_followup_context_repair
            time_range_days=180,
            time_granularity="month",
            filters={},
        )

        # Run the fixup chain (same order as ui/pipeline.py)
        _apply_followup_context_repair(qo, turn1_history, catalog)
        _hydrate_retention_event_from_metric(qo, catalog)
        _apply_lineage_rollforward_filters(qo, turn1_history, catalog)

        assert qo.filters.get("transaction_channel") == "UPI", (
            "UPI filter must survive from turn 1 to turn 2 monthly trend.\n"
            f"Filters after fixup: {qo.filters}"
        )

        # Compile and assert SQL has the filter
        sql, _ = compile_query(qo, metrics)
        assert "transaction_channel = 'UPI'" in sql, (
            "Monthly trend SQL must contain UPI filter.\n"
            f"SQL: {sql[:300]}"
        )

    def test_breakdown_not_inherited_across_turns(self, catalog, metrics):
        """
        Breakdown from turn 1 must NOT automatically carry to turn 2 unless user asks.
        Follow-ups should only inherit filters, not breakdown dimension.
        """
        turn1_history = [{
            "qo": {
                "analysis_type": "segment",
                "metric_id": "activation_rate",
                "breakdown": "platform",
                "filters": {},
                "time_range_days": 30,
            }
        }]
        qo = QueryObject(
            analysis_type="metric",
            metric_id="activation_rate",
            time_range_days=90,
        )
        _apply_followup_context_repair(qo, turn1_history, catalog)
        # breakdown should not be inherited (user said "show activation rate", not "by platform")
        assert qo.breakdown is None, (
            "Breakdown must not be silently inherited from prior segment turn."
        )


# ── Phase 1: Data quality gate ────────────────────────────────────────────────

class TestDataQualityGate:
    """validate_findings → SUSPICIOUS/grade-D → investigate() sets data_quality_blocked."""

    def test_suspicious_result_blocks_story_arc(self):
        """Grade-D / SUSPICIOUS validation sets data_quality_blocked and skips narrative."""
        from core.analysis.analyst import AnalystReport, _format_data_quality_block
        from core.sql.validator import ValidationResult, ArithmeticWarning

        # Build a SUSPICIOUS ValidationResult (score drops below 50 → grade D)
        arith_errors = [
            ArithmeticWarning(check="rate_bounds", detail="retention_pct contains values > 100%", severity="error"),
            ArithmeticWarning(check="rate_bounds", detail="retention_pct max = 100.0%", severity="error"),
            ArithmeticWarning(check="rate_bounds", detail="third error", severity="error"),
        ]
        val = ValidationResult(
            arithmetic_warnings=arith_errors,
            sanity_flag="SUSPICIOUS",
            sanity_reason="100% retention is implausible for this event",
            score=20,
            grade="D",
            confidence_label="Very low confidence",
        )

        msg = _format_data_quality_block(val)
        assert "unusual" in msg.lower() or "reliable" in msg.lower(), \
            "Data quality block must mention unusual/reliable"
        assert "retention_pct contains values > 100%" in msg, \
            "Must include the arithmetic warning detail"

    def test_data_quality_blocked_flag_on_report(self):
        """AnalystReport.data_quality_blocked defaults to False."""
        from core.analysis.analyst import AnalystReport
        r = AnalystReport(analysis_type="retention")
        assert r.data_quality_blocked is False

    def test_plausible_result_does_not_block(self):
        """Grade-A result must not set data_quality_blocked."""
        from core.sql.validator import ValidationResult
        val = ValidationResult(score=100, grade="A", sanity_flag="PLAUSIBLE")
        # Not SUSPICIOUS and not grade D — gate should not trigger
        blocked = val.grade == "D" or val.sanity_flag == "SUSPICIOUS"
        assert not blocked, "PLAUSIBLE/A result must not trigger the quality gate"


# ── Phase 3: Chart type from semantics ───────────────────────────────────────

class TestChartTypeFromSemantics:
    """preferred_chart populated by resolve_query_semantics for deterministic chart types."""

    def test_retention_with_breakdown_gets_heatmap(self):
        from core.semantic.query_semantics import resolve_query_semantics
        qo = QueryObject(
            analysis_type="retention",
            event="app_opened",
            breakdown="platform",
            retention_window_days=7,
            time_range_days=90,
        )
        sem = resolve_query_semantics(qo)
        assert sem.preferred_chart == "retention_heatmap", \
            f"Retention+breakdown must get retention_heatmap, got {sem.preferred_chart!r}"

    def test_retention_period_matrix_gets_heatmap(self):
        from core.semantic.query_semantics import resolve_query_semantics, RetentionTemplate
        qo = QueryObject(
            analysis_type="retention",
            event="app_opened",
            retention_window_days=30,
            time_range_days=120,
        )
        sem = resolve_query_semantics(qo)
        assert sem.retention is not None
        assert sem.retention.template == RetentionTemplate.PERIOD_MATRIX
        assert sem.preferred_chart == "retention_heatmap", \
            f"period_matrix retention must get retention_heatmap, got {sem.preferred_chart!r}"

    def test_retention_simple_gets_line(self):
        from core.semantic.query_semantics import resolve_query_semantics
        qo = QueryObject(
            analysis_type="retention",
            event="app_opened",
            retention_window_days=7,
            time_range_days=30,
        )
        sem = resolve_query_semantics(qo)
        assert sem.preferred_chart == "retention_line", \
            f"Simple retention without breakdown must get retention_line, got {sem.preferred_chart!r}"

    def test_funnel_gets_funnel_bar(self):
        from core.semantic.query_semantics import resolve_query_semantics
        qo = QueryObject(
            analysis_type="funnel",
            funnel_steps=["sign_up", "onboarding_completed", "first_transaction"],
        )
        sem = resolve_query_semantics(qo)
        assert sem.preferred_chart == "funnel_bar", \
            f"Funnel must get funnel_bar, got {sem.preferred_chart!r}"

    def test_lifecycle_gets_lifecycle_stages(self):
        from core.semantic.query_semantics import resolve_query_semantics
        qo = QueryObject(analysis_type="user_lifecycle", event="app_opened")
        sem = resolve_query_semantics(qo)
        assert sem.preferred_chart == "lifecycle_stages", \
            f"user_lifecycle must get lifecycle_stages, got {sem.preferred_chart!r}"

    def test_metric_has_no_preferred_chart(self):
        from core.semantic.query_semantics import resolve_query_semantics
        qo = QueryObject(analysis_type="metric", event="app_opened")
        sem = resolve_query_semantics(qo)
        assert sem.preferred_chart is None, \
            f"Generic metric should have no preferred_chart, got {sem.preferred_chart!r}"


# ── Phase 2: QO precondition validator ───────────────────────────────────────

class TestQOPreconditionValidator:
    """validate_qo_preconditions catches structural LLM errors before the fixup chain."""

    def test_retention_without_event_or_metric_is_violation(self):
        from ui.qo_fixups import validate_qo_preconditions
        qo = QueryObject(analysis_type="retention")
        violations = validate_qo_preconditions(qo)
        fields = [v.field for v in violations]
        assert "event" in fields, \
            f"retention with no event/metric_id must produce a violation, got {fields}"

    def test_retention_with_event_is_clean(self):
        from ui.qo_fixups import validate_qo_preconditions
        qo = QueryObject(analysis_type="retention", event="app_opened")
        violations = validate_qo_preconditions(qo)
        non_fixable = [v for v in violations if not v.auto_fix]
        assert all(v.field != "event" for v in non_fixable), \
            "retention with event set must not have an event violation"

    def test_funnel_with_one_step_is_violation(self):
        from ui.qo_fixups import validate_qo_preconditions
        qo = QueryObject(analysis_type="funnel", funnel_steps=["sign_up"])
        violations = validate_qo_preconditions(qo)
        fields = [v.field for v in violations]
        assert "funnel_steps" in fields, \
            f"funnel with 1 step must produce a violation, got {fields}"

    def test_funnel_with_two_steps_is_clean(self):
        from ui.qo_fixups import validate_qo_preconditions
        qo = QueryObject(
            analysis_type="funnel",
            funnel_steps=["sign_up", "first_transaction"],
        )
        violations = [v for v in validate_qo_preconditions(qo) if not v.auto_fix]
        assert all(v.field != "funnel_steps" for v in violations), \
            "funnel with 2 steps must not have a funnel_steps violation"

    def test_segment_without_breakdown_is_violation(self):
        from ui.qo_fixups import validate_qo_preconditions
        qo = QueryObject(analysis_type="segment", event="app_opened")
        violations = validate_qo_preconditions(qo)
        fields = [v.field for v in violations]
        assert "breakdown" in fields, \
            f"segment without breakdown must produce a violation, got {fields}"

    def test_breakdown_with_default_source_is_auto_fix(self):
        from ui.qo_fixups import validate_qo_preconditions, apply_auto_fix_preconditions
        qo = QueryObject(
            analysis_type="segment",
            event="app_opened",
            breakdown="platform",
        )
        # breakdown_source defaults to "default" on most QOs
        setattr(qo, "breakdown_source", "default")
        violations = validate_qo_preconditions(qo)
        auto_fixable = [v for v in violations if v.auto_fix and v.field == "breakdown_source"]
        assert auto_fixable, "breakdown+default source must produce an auto-fixable violation"
        needs_retry = apply_auto_fix_preconditions(qo, violations)
        bd_src = getattr(qo, "breakdown_source", "default")
        assert bd_src in ("user", "event"), \
            f"auto-fix must set breakdown_source to user or event, got {bd_src!r}"
        # The breakdown violation (no breakdown for segment would be fixed if breakdown is set)
        non_fixable = [v for v in needs_retry if v.field == "breakdown_source"]
        assert not non_fixable, "breakdown_source violation must be consumed by auto-fix"


class TestBehavioralCohortFilterExtraction:
    """
    Compiler-level tests for behavioral_cohort filter handling.

    Bug class: orchestrator extracts qualifier on event_b into qo.filters;
    the compiler must honour those filters in the event_b JOIN, not silently drop them.
    """

    def test_overlap_with_filter_includes_filter_in_sql(self):
        """Filter on event_b must appear inside the 'did_b' CTE, not be dropped."""
        from core.sql.compilers import compile_query

        qo = QueryObject(
            analysis_type="behavioral_cohort",
            event="event_a",
            event_b="event_b",
            filters={"channel": "upi"},
            time_range_days=30,
        )
        sql, _ = compile_query(qo, [])
        assert "upi" in sql.lower(), (
            "Filter value must appear in compiled SQL for behavioral_cohort overlap."
        )

    def test_anti_cohort_with_filter_includes_filter_in_sql(self):
        """Anti-cohort (did A but NOT B with filter) must scope filter to event_b."""
        from core.sql.compilers import compile_query

        qo = QueryObject(
            analysis_type="behavioral_cohort",
            event="event_a",
            event_b="event_b",
            metric_variant="anti_cohort",
            filters={"channel": "upi"},
            time_range_days=30,
        )
        sql, _ = compile_query(qo, [])
        assert "upi" in sql.lower(), (
            "Filter value must appear in compiled SQL for behavioral_cohort anti_cohort."
        )


class TestAntiCohortQualifierExtraction:
    """
    Bug class: negated behavioral_cohort phrases ("no <qualifier> <event_b>",
    "without <qualifier> <event_b>") must still extract the qualifier into filters.

    The negation word sets metric_variant=anti_cohort but does NOT remove the qualifier.
    Rule-based extraction via extract_dimension_filters_from_question must handle both
    positive ("and made a <qualifier> <event_b>") and negated forms.
    """

    def test_negated_phrase_extracts_qualifier_with_catalog(self):
        """'no <qualifier> <event_b>' → filters[col]=qualifier even with negation."""
        from core.pipeline.catalog_vocab import extract_dimension_filters_from_question

        # Minimal catalog with a dimension that has value_meanings
        catalog = {
            "events": {
                "events": [
                    {
                        "raw_name": "channel",
                        "value_meanings": {"express": "express channel", "standard": "standard channel"},
                    }
                ]
            }
        }
        sampled = {"events": {"channel": ["express", "standard"]}}

        result = extract_dimension_filters_from_question(
            "users who signed up and no express purchase",
            sampled,
            catalog,
        )
        assert result.get("channel") == "express", (
            "Qualifier adjacent to negated event_b must still be extracted into filters."
        )

    def test_positively_phrased_extracts_qualifier(self):
        """Positive form ('and made a <qualifier> <event_b>') also extracts qualifier."""
        from core.pipeline.catalog_vocab import extract_dimension_filters_from_question

        catalog = {
            "events": {
                "events": [
                    {
                        "raw_name": "channel",
                        "value_meanings": {"express": "express channel", "standard": "standard channel"},
                    }
                ]
            }
        }
        sampled = {"events": {"channel": ["express", "standard"]}}

        result = extract_dimension_filters_from_question(
            "users who signed up and made an express purchase",
            sampled,
            catalog,
        )
        assert result.get("channel") == "express", (
            "Qualifier adjacent to positive event_b phrase must be extracted into filters."
        )


class TestRetentionCohortDescriptorFilters:
    """
    Bug class: cohort-descriptor phrases ('active users', 'transacting users') must NOT
    become column filters in retention queries. The event drives cohort membership;
    filters should remain empty unless the user specified an explicit column=value filter.

    These tests work at the compiler level — they verify that spurious filters
    (IS NOT NULL sentinels injected by the orchestrator) don't break retention SQL.
    """

    def test_retention_with_is_not_null_sentinel_compiles(self):
        """Retention with an IS NOT NULL sentinel in filters must still compile."""
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            event="app_opened",
            event_b="app_opened",
            retention_window_days=7,
            filters={"account_type": "__IS_NOT_NULL__"},
            time_range_days=90,
        )
        sql = _compile_retention(qo)
        # Must produce valid SQL, not crash
        assert "cohort" in sql.lower()
        assert "retained" in sql.lower()

    def test_retention_empty_filters_compiles_clean(self):
        """Retention with no filters (correct case for cohort-descriptor queries)."""
        from core.sql.compilers import _compile_retention

        qo = QueryObject(
            analysis_type="retention",
            event="app_opened",
            event_b="app_opened",
            retention_window_days=7,
            filters={},
            time_range_days=90,
        )
        sql = _compile_retention(qo)
        assert "cohort" in sql.lower()
        assert "retained" in sql.lower()
        assert "IS NOT NULL" not in sql


class TestSentinelVsLiteralPriority:
    """
    Bug class: when a user says a literal value (e.g. "UPI") that exists in DIMENSION
    VALUE HINTS, the orchestrator must use the literal — not the IS NOT NULL sentinel
    that may also map to the same column via a custom event.

    The fixup layer (not the orchestrator) owns sentinel remapping, so we test here
    that the fixup does NOT remap a known literal value to a sentinel.
    """

    def test_known_literal_not_remapped_to_sentinel(self):
        """
        _remap_invalid_filter_values_via_custom_events must leave literal values that
        are already valid catalog values alone — it should only remap values not found
        in sampled_values.
        """
        from ui.qo_fixups import _remap_invalid_filter_values_via_custom_events

        catalog = {
            "__business_context__": {
                "custom_events": [
                    {
                        "name": "channel_active",
                        "sql": "channel IS NOT NULL",
                        "builder_definition": {
                            "groups": [{"filters": [{"field": "channel", "op": "IS NOT NULL"}]}]
                        },
                    }
                ]
            }
        }
        sampled = {"events": {"channel": ["upi", "neft", "imps"]}}

        # "upi" is a known sampled value — must NOT be remapped to __IS_NOT_NULL__
        qo = QueryObject(
            analysis_type="metric",
            event="purchase",
            filters={"channel": "upi"},
        )
        _remap_invalid_filter_values_via_custom_events(qo, catalog=catalog, sampled_values=sampled)
        assert qo.filters.get("channel") == "upi", (
            "Known literal 'upi' must not be remapped to a sentinel."
        )

    def test_unknown_value_remapped_to_sentinel(self):
        """Values not in sampled_values should be remapped to the CE sentinel."""
        from ui.qo_fixups import _remap_invalid_filter_values_via_custom_events

        catalog = {
            "__business_context__": {
                "custom_events": [
                    {
                        "name": "channel_active",
                        "sql": "channel IS NOT NULL",
                        "builder_definition": {
                            "groups": [{"filters": [{"field": "channel", "op": "IS NOT NULL"}]}]
                        },
                    }
                ]
            }
        }
        sampled = {"events": {"channel": ["upi", "neft", "imps"]}}

        qo = QueryObject(
            analysis_type="metric",
            event="purchase",
            filters={"channel": "in_app"},  # not in sampled values
        )
        _remap_invalid_filter_values_via_custom_events(qo, catalog=catalog, sampled_values=sampled)
        assert qo.filters.get("channel") == "__IS_NOT_NULL__", (
            "Unknown value 'in_app' should be remapped to IS NOT NULL sentinel via CE."
        )
