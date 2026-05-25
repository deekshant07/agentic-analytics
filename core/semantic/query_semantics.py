"""
Resolved query semantics — single source of truth after QO normalization.

Compilers, charts, and narration should read ResolvedQuerySemantics (attached on
the QueryObject as ``_query_semantics``) instead of re-deriving intent from
heuristics on raw slots.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from core.sql.query_object import QueryObject


class RetentionTemplate(str, Enum):
    """SQL shape for retention analysis."""

    MOM_NDAY = "mom_nday"              # monthly cohort + single retention_pct (first N days)
    WEEKLY_NDAY = "weekly_nday"        # weekly cohort + retention_pct
    PERIOD_MATRIX = "period_matrix"    # monthly cohort + m1..mN (30-day buckets, D30+)


class CohortAnchorPolicy(str, Enum):
    FIRST_EVENT_GLOBAL_THEN_LOOKBACK = "first_event_global_then_lookback"


class ValueKind(str, Enum):
    PERCENT_0_100 = "percent_0_100"
    COUNT = "count"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetentionSemantics:
    template: RetentionTemplate
    return_window_days: int
    cohort_anchor_policy: CohortAnchorPolicy = CohortAnchorPolicy.FIRST_EVENT_GLOBAL_THEN_LOOKBACK
    primary_metric_column: str = "retention_pct"
    cohort_time_column: str = "cohort_month"
    value_kind: ValueKind = ValueKind.PERCENT_0_100
    maturity_window_days: int = 7

    @property
    def narration_frame(self) -> str:
        if self.template == RetentionTemplate.PERIOD_MATRIX:
            return (
                f"Month-over-month retention with {self.return_window_days}-day period buckets "
                f"(m1, m2, … after cohort month)"
            )
        grain = "month" if self.template == RetentionTemplate.MOM_NDAY else "week"
        return (
            f"Month-over-month {self.return_window_days}-day retention"
            if grain == "month"
            else f"{self.return_window_days}-day retention by {grain}"
        )


@dataclass(frozen=True)
class ResolvedQuerySemantics:
    """Frozen semantics for the current query (extensible beyond retention)."""

    analysis_type: str
    retention: Optional[RetentionSemantics] = None
    primary_metric_column: Optional[str] = None
    value_kind: ValueKind = ValueKind.UNKNOWN
    maturity_window_days: Optional[int] = None
    narration_frame: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    preferred_chart: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.retention:
            d["retention"] = {
                **asdict(self.retention),
                "template": self.retention.template.value,
                "cohort_anchor_policy": self.retention.cohort_anchor_policy.value,
                "value_kind": self.retention.value_kind.value,
            }
        d["value_kind"] = self.value_kind.value
        return d


def resolve_retention_semantics(qo: QueryObject) -> RetentionSemantics:
    """
    Choose retention SQL template from resolved QO slots (after normalization).

    Rules (explicit, testable):
      - return_window_days >= 30 → period_matrix (D30 survival buckets)
      - else time_granularity month OR time_range_days > 60 → mom_nday
      - else → weekly_nday
    """
    win = int(getattr(qo, "retention_window_days", None) or 7)
    days = int(getattr(qo, "time_range_days", None) or 30)
    gran = (getattr(qo, "time_granularity", None) or "day").lower()

    if win >= 30:
        template = RetentionTemplate.PERIOD_MATRIX
        cohort_col = "cohort_month"
    elif gran == "month" or days > 60:
        template = RetentionTemplate.MOM_NDAY
        cohort_col = "cohort_month"
    else:
        template = RetentionTemplate.WEEKLY_NDAY
        cohort_col = "cohort_week"

    return RetentionSemantics(
        template=template,
        return_window_days=win,
        cohort_time_column=cohort_col,
        primary_metric_column="retention_pct",
        value_kind=ValueKind.PERCENT_0_100,
        maturity_window_days=win,
    )


def resolve_query_semantics(qo: QueryObject) -> ResolvedQuerySemantics:
    """Build semantics contract for any analysis type."""
    at = str(getattr(qo, "analysis_type", "") or "").strip()

    if at == "retention":
        ret = resolve_retention_semantics(qo)
        frame = ret.narration_frame
        bd = str(getattr(qo, "breakdown", None) or "").strip()
        if bd:
            grain = "month" if ret.template.value in ("mom_nday",) else "week"
            period_label = "cohort month" if grain == "month" else "cohort week"
            frame = (
                f"D{ret.return_window_days} retention by {bd.replace('_', ' ')} "
                f"(retention_pct per {period_label} × {bd})"
            )
        if bd:
            preferred_chart = "retention_heatmap"
        elif ret.template == RetentionTemplate.PERIOD_MATRIX:
            preferred_chart = "retention_heatmap"
        else:
            preferred_chart = "retention_line"
        return ResolvedQuerySemantics(
            analysis_type=at,
            retention=ret,
            primary_metric_column=ret.primary_metric_column,
            value_kind=ret.value_kind,
            maturity_window_days=ret.maturity_window_days,
            narration_frame=frame,
            extra={"breakdown": bd} if bd else {},
            preferred_chart=preferred_chart,
        )

    mid = (getattr(qo, "metric_id", None) or "").lower()
    if at == "metric" and "activation" in mid:
        win = getattr(qo, "activation_window_days", None)
        return ResolvedQuerySemantics(
            analysis_type=at,
            primary_metric_column="pct",
            value_kind=ValueKind.PERCENT_0_100,
            maturity_window_days=int(win) if win else 30,
            narration_frame=(
                f"{int(win)}-day activation rate by cohort month"
                if win
                else "Activation rate by cohort month"
            ),
        )

    if at == "funnel":
        return ResolvedQuerySemantics(analysis_type=at, preferred_chart="funnel_bar")

    if at == "user_lifecycle":
        return ResolvedQuerySemantics(analysis_type=at, preferred_chart="lifecycle_stages")

    return ResolvedQuerySemantics(analysis_type=at)


def attach_query_semantics(qo: QueryObject) -> ResolvedQuerySemantics:
    """Resolve and store semantics on the QO for downstream compile/viz/narration."""
    sem = resolve_query_semantics(qo)
    setattr(qo, "_query_semantics", sem)
    return sem
