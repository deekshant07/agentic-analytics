"""
Unit tests for resolved query semantics (structural retention / presentation layer).

Run:
  pytest qa/test_query_semantics.py -v
  pytest qa/test_query_semantics.py qa/regression.py -m "not db" -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pipeline.activation_window import parse_retention_window_days_from_prompt
from core.pipeline.normalize_query import normalize_query_object
from core.semantic.presentation import (
    format_rate_columns_for_display,
    preferred_display_column,
)
from core.semantic.query_semantics import (
    CohortAnchorPolicy,
    RetentionTemplate,
    attach_query_semantics,
    resolve_query_semantics,
    resolve_retention_semantics,
)
from core.sql.compilers import _compile_retention
from core.sql.query_object import QueryObject
from ui.qo_fixups import (
    _apply_retention_metric_to_qo,
    _apply_retention_window_from_prompt,
    _hydrate_retention_event_from_metric,
)


_MINIMAL_RETENTION_CATALOG = {
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
        ],
    },
}


class TestResolveRetentionSemantics:
    def test_mom_14_day_uses_mom_nday_not_matrix(self):
        qo = QueryObject(
            analysis_type="retention",
            retention_window_days=14,
            time_range_days=180,
            time_granularity="month",
        )
        sem = resolve_retention_semantics(qo)
        assert sem.template == RetentionTemplate.MOM_NDAY
        assert sem.return_window_days == 14
        assert sem.primary_metric_column == "retention_pct"
        assert sem.cohort_time_column == "cohort_month"
        assert sem.cohort_anchor_policy == CohortAnchorPolicy.FIRST_EVENT_GLOBAL_THEN_LOOKBACK

    def test_d30_uses_period_matrix(self):
        qo = QueryObject(
            analysis_type="retention",
            retention_window_days=30,
            time_granularity="month",
            time_range_days=180,
        )
        sem = resolve_retention_semantics(qo)
        assert sem.template == RetentionTemplate.PERIOD_MATRIX

    def test_short_weekly_uses_weekly_nday(self):
        qo = QueryObject(
            analysis_type="retention",
            retention_window_days=7,
            time_range_days=30,
            time_granularity="day",
        )
        sem = resolve_retention_semantics(qo)
        assert sem.template == RetentionTemplate.WEEKLY_NDAY
        assert sem.cohort_time_column == "cohort_week"

    def test_attach_stores_on_qo(self):
        qo = QueryObject(analysis_type="retention", retention_window_days=14)
        sem = attach_query_semantics(qo)
        assert getattr(qo, "_query_semantics") is sem
        assert sem.retention is not None
        assert "14-day" in sem.narration_frame or "14" in sem.narration_frame


class TestRetentionSqlInvariants:
    """SQL must satisfy cohort-anchor and template invariants."""

    def _mom_14_qo(self) -> QueryObject:
        qo = QueryObject(
            analysis_type="retention",
            event="transaction_reconciled",
            event_b="transaction_reconciled",
            retention_window_days=14,
            time_range_days=180,
            time_granularity="month",
            filters={"transaction_status": "SUCCESS"},
        )
        attach_query_semantics(qo)
        return qo

    def test_mom_sql_has_retention_pct_not_m1_matrix(self):
        sql = _compile_retention(self._mom_14_qo())
        assert "retention_pct" in sql
        assert "m1_pct" not in sql
        assert "INTERVAL '14' DAY" in sql

    def test_cohort_anchor_before_lookback(self):
        sql = _compile_retention(self._mom_14_qo())
        assert "user_first AS" in sql
        assert "first_ts >= CURRENT_DATE - INTERVAL '180' DAY" in sql
        m = re.search(r"user_first AS \((.*?)\),\s*cohort AS", sql, re.DOTALL)
        assert m
        assert "INTERVAL '180' DAY" not in m.group(1)

    def test_return_window_starts_at_cohort_not_n_to_2n(self):
        sql = _compile_retention(self._mom_14_qo())
        retained = sql.split("retained AS", 1)[1].split(")\nSELECT", 1)[0]
        assert ">= c.cohort_month::TIMESTAMP" in retained
        assert "INTERVAL '14' DAY" in retained
        assert "+ INTERVAL '7' DAY" not in retained

    def test_compiler_reads_fresh_semantics_after_re_attach(self):
        qo = self._mom_14_qo()
        attach_query_semantics(qo)
        assert qo._query_semantics.retention.template == RetentionTemplate.MOM_NDAY
        qo.retention_window_days = 30
        attach_query_semantics(qo)
        assert qo._query_semantics.retention.template == RetentionTemplate.PERIOD_MATRIX
        sql_matrix = _compile_retention(qo)
        assert "m1_pct" in sql_matrix
        qo.retention_window_days = 14
        attach_query_semantics(qo)
        sql_mom = _compile_retention(qo)
        assert "retention_pct" in sql_mom
        assert "m1_pct" not in sql_mom


class TestNormalizeQueryObject:
    def test_prompt_14_overrides_catalog_d7_hydration(self):
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            retention_window_days=7,
            time_granularity="month",
            time_range_days=180,
        )
        normalize_query_object(
            qo,
            prompt="share MOM 14 day retention number",
            history=None,
            catalog=_MINIMAL_RETENTION_CATALOG,
            sampled_values={},
            metrics=[],
        )
        assert qo.retention_window_days == 14
        sem = getattr(qo, "_query_semantics")
        assert sem.retention.template == RetentionTemplate.MOM_NDAY
        assert sem.retention.return_window_days == 14

    def test_explicit_window_blocks_hydrate_overwrite(self):
        qo = QueryObject(analysis_type="retention", metric_id="d7_retention")
        metric = _MINIMAL_RETENTION_CATALOG["events"]["suggested_metrics"][0]
        _apply_retention_metric_to_qo(qo, metric)
        assert qo.retention_window_days == 7
        setattr(qo, "_retention_window_explicit", True)
        qo.retention_window_days = 14
        _apply_retention_metric_to_qo(qo, metric)
        assert qo.retention_window_days == 14


class TestRetentionBreakdownFollowup:
    def test_inherits_14_day_window_from_prior_retention_turn(self):
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            breakdown="platform",
            time_granularity="month",
            time_range_days=180,
            retention_window_days=7,
        )
        history = [
            {
                "qo": {
                    "analysis_type": "retention",
                    "event": "transaction_reconciled",
                    "event_b": "transaction_reconciled",
                    "retention_window_days": 14,
                    "time_range_days": 180,
                    "time_granularity": "month",
                    "filters": {"transaction_status": "SUCCESS"},
                    "time_source": "explicit",
                },
            }
        ]
        from ui.qo_fixups import _apply_retention_context_from_history

        _apply_retention_context_from_history(
            qo, "Show retention broken down by platform", history,
        )
        assert qo.retention_window_days == 14

    def test_breakdown_sql_groups_by_platform(self):
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
        attach_query_semantics(qo)
        sql = _compile_retention(qo)
        assert "GROUP BY 1, 2" in sql
        assert "platform" in sql.lower()
        assert "INTERVAL '14' DAY" in sql
        assert "retention_pct" in sql

    def test_normalize_retention_breakdown_followup(self):
        qo = QueryObject(
            analysis_type="retention",
            metric_id="d7_retention",
            breakdown="platform",
            time_granularity="month",
            time_range_days=180,
        )
        history = [
            {
                "qo": {
                    "analysis_type": "retention",
                    "retention_window_days": 14,
                    "time_range_days": 180,
                    "time_granularity": "month",
                    "event": "transaction_reconciled",
                    "event_b": "transaction_reconciled",
                },
            }
        ]
        normalize_query_object(
            qo,
            prompt="Show retention broken down by platform",
            history=history,
            catalog=_MINIMAL_RETENTION_CATALOG,
            sampled_values={},
            metrics=[],
        )
        assert qo.retention_window_days == 14
        assert qo._query_semantics.retention.return_window_days == 14
        sql = _compile_retention(qo)
        assert "platform" in sql


class TestPresentation:
    def test_prefer_retention_pct_over_cohort_size(self):
        import pandas as pd
        from core.semantic.query_semantics import resolve_query_semantics

        df = pd.DataFrame(
            {
                "cohort_month": ["2026-01-01"],
                "cohort_size": [1000],
                "retained_users": [200],
                "retention_pct": [20.0],
            }
        )
        qo = QueryObject(analysis_type="retention", retention_window_days=14)
        sem = resolve_query_semantics(qo)
        assert preferred_display_column(df, sem) == "retention_pct"

    def test_format_retention_pct_with_percent_sign(self):
        import pandas as pd

        df = pd.DataFrame({"retention_pct": [77.2, 19.6]})
        out = format_rate_columns_for_display(df)
        assert out["retention_pct"].iloc[0] == "77.2%"
        assert out["retention_pct"].iloc[1] == "19.6%"
