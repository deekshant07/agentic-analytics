"""
QueryObject — the typed intermediate representation that sits between
the LLM (orchestrator) and the SQL compilers.

The LLM fills slots from catalog vocabulary. It never writes SQL.
Python compilers turn QueryObject → deterministic SQL.
"""

from dataclasses import dataclass, field
from typing import Optional

ANALYSIS_TYPES = frozenset({
    "metric",                # time-series count/distinct for one event
    "segment",               # metric broken down by a single dimension column
    "demographic_breakdown", # user profile across ALL available demographic dimensions
    "funnel",                # ordered conversion steps
    "funnel_compare",        # same steps: per-step users current vs prior period
    "funnel_property_drilldown",  # at the worst drop-off step, break down by a property
    "journey",               # v0: top next-event transitions after an anchor event
    "retention",             # cohort return rate (event_a → event_b after N days)
    "behavioral_cohort",     # users who did event_a but NOT event_b (anti-join)
    "time_between",          # median/avg time from event_a to event_b per user
    "user_lifecycle",        # classify users into lifecycle stages (new/active/at-risk/churned)
    "stickiness",            # DAU/WAU/MAU ratios and L7/L28 stickiness
    "xyz_matrix",            # 3-axis cohort table: cohort × dimension × metric value
    # Users who did `event` in the window: split by whether calendar month of
    # first-ever `event_b` equals calendar month of first qualifying `event` in window.
    "same_month_anchor",
    "diagnose",              # why did metric X drop/spike — dimension slicing
    "forecast",              # linear trend projection for next N periods
    "clarify",               # question is ambiguous — ask the user
    "out_of_scope",          # not answerable (export, ML, send messages, untracked UI features, etc.)
    "multi_intent",          # user asked for two+ distinct named metrics simultaneously
})

_NULLISH_STRINGS = {"none", "null", "nil", "n/a", "na", ""}


def _norm_opt_str(v):
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in _NULLISH_STRINGS:
            return None
        return s
    return v


@dataclass
class QueryObject:
    analysis_type: str                          # must be in ANALYSIS_TYPES

    # --- shared ---
    metric_id: Optional[str] = None             # pre-built metric id (skips compiler)
    event: Optional[str] = None                 # primary event name from catalog
    # Optional metric behavior modifier for deterministic compiler variants.
    # Examples:
    # - per_user_count: COUNT(*) / COUNT(DISTINCT user_id)
    # - per_user_value: SUM(metric_value_col) / COUNT(DISTINCT user_id)
    metric_variant: Optional[str] = None
    metric_value_col: Optional[str] = None
    metric_status_col: Optional[str] = None
    metric_status_target: Optional[str] = None
    filters: dict = field(default_factory=dict)         # {col: value} — include filter
    filter_excludes: dict = field(default_factory=dict) # {col: value|[values]} — exclude filter (NOT IN / !=)
    time_range_days: int = 30                   # lookback window (ignored when date_from is set)

    # Absolute date range — takes priority over time_range_days when set.
    # Set by the orchestrator when the user names a specific period ("in January", "Q1 2026").
    date_from: Optional[str] = None             # YYYY-MM-DD inclusive start
    date_to: Optional[str] = None               # YYYY-MM-DD exclusive end (first day after period)

    # Second absolute window for behavioral_cohort when the same event is checked in
    # another calendar period (e.g. March transactors who also transacted in February).
    secondary_date_from: Optional[str] = None   # YYYY-MM-DD inclusive
    secondary_date_to: Optional[str] = None     # YYYY-MM-DD exclusive

    # --- segment ---
    breakdown: Optional[str] = None             # column to GROUP BY

    # --- funnel ---
    funnel_steps: list = field(default_factory=list)  # ordered list of event names

    # --- retention ---
    event_b: Optional[str] = None               # return event (defaults to event if None)
    retention_window_days: int = 7              # D1=1, D7=7, D30=30
    # Source tag: "explicit" when user said "D7", "14-day retention", "24hr retention".
    # "default" when no window stated — compiler uses catalog default or scale rule.
    retention_window_days_source: str = "default"

    # --- activation window ---
    # Days after the cohort event within which the conversion must happen.
    # None = lifetime (any time) — used for activation_rate and similar '% of users' metrics.
    # Set by orchestrator when user says "7-day activation", "D30 activation", etc.
    activation_window_days: Optional[int] = None
    # Source tag: was this value explicitly stated by the user, or is it a default/inferred?
    # "explicit" → user said "D7 activation", "24hr conversion", "within 30 days"
    # "default"  → no window mentioned; compiler uses its own default_windows
    activation_window_days_source: str = "default"

    # --- time granularity (shared) ---
    # Controls how the time axis is bucketed for trend/retention queries.
    # "day" → daily, "week" → weekly, "month" → monthly (MOM)
    # When set to "week" or "month", pre-built scalar sql_hints are bypassed
    # so the compiler generates the properly bucketed query instead.
    time_granularity: str = "day"               # "day" | "week" | "month"
    # Source tag: "explicit" when user said "by month", "weekly", "MOM" etc.
    # "default" means the LLM filled in the default — do NOT use as a scalar/trend signal.
    time_granularity_source: str = "default"
    # Source of time window selection:
    # - "explicit": user explicitly asked a period/trend window
    # - "inherited": copied from previous turn context
    # - "default": system default (e.g., last 30 days)
    time_source: str = "default"

    # --- breakdown source ---
    # "explicit" when user said "by channel", "split by region", etc.
    # "default"  when LLM inferred or no breakdown was requested.
    breakdown_source: str = "default"

    # --- diagnose ---
    # Optional end date for the diagnose comparison window (YYYY-MM-DD).
    # When set, the diagnose agent treats this date as "today" for the purpose
    # of splitting the window — enabling month-specific analysis like "in March".
    # If None, defaults to CURRENT_DATE (rolling window from today).
    diagnose_period_end: Optional[str] = None

    # --- user_lifecycle ---
    # lifecycle_activation_event: Optional[str] = None  # future: explicit activation override

    # --- xyz_matrix ---
    # First groupby dimension (beyond cohort) for the 3-axis matrix.
    # e.g. "platform", "acquisition_cohort", "city"
    xyz_axis1: Optional[str] = None

    # --- threshold_user_count variant ---
    # Integer N for "users who did event more than N times". Set by orchestrator
    # when the user says "more than N times" / "at least N times" / "N+ transactions".
    threshold: Optional[int] = None

    # --- clarify ---
    clarify_message: Optional[str] = None       # question to show the user

    # --- routing ---
    # Set by the orchestrator, not inferred by keyword matching.
    # "deep"  → run parallel multi-query investigation (Deep Analysis)
    # "quick" → single-query path (default)
    depth: str = "quick"

    @classmethod
    def from_dict(cls, d: dict) -> "QueryObject":
        return cls(
            analysis_type=_norm_opt_str(d.get("analysis_type")) or "clarify",
            metric_id=_norm_opt_str(d.get("metric_id")),
            event=_norm_opt_str(d.get("event")),
            metric_variant=_norm_opt_str(d.get("metric_variant")),
            metric_value_col=_norm_opt_str(d.get("metric_value_col")),
            metric_status_col=_norm_opt_str(d.get("metric_status_col")),
            metric_status_target=_norm_opt_str(d.get("metric_status_target")),
            filters=d.get("filters") or {},
            filter_excludes=d.get("filter_excludes") or {},
            time_range_days=int(d.get("time_range_days") or 30),
            date_from=_norm_opt_str(d.get("date_from")),
            date_to=_norm_opt_str(d.get("date_to")),
            secondary_date_from=_norm_opt_str(d.get("secondary_date_from")),
            secondary_date_to=_norm_opt_str(d.get("secondary_date_to")),
            breakdown=_norm_opt_str(d.get("breakdown")),
            funnel_steps=list(d.get("funnel_steps") or []),
            event_b=_norm_opt_str(d.get("event_b")),
            retention_window_days=int(d["retention_window_days"]) if d.get("retention_window_days") is not None and str(d["retention_window_days"]).lstrip("-").isdigit() else 7,
            retention_window_days_source=_norm_opt_str(d.get("retention_window_days_source")) or "default",
            activation_window_days=int(d["activation_window_days"]) if d.get("activation_window_days") is not None and str(d["activation_window_days"]).lstrip("-").isdigit() else None,
            activation_window_days_source=_norm_opt_str(d.get("activation_window_days_source")) or "default",
            time_granularity=_norm_opt_str(d.get("time_granularity")) or "day",
            time_granularity_source=_norm_opt_str(d.get("time_granularity_source")) or "default",
            time_source=_norm_opt_str(d.get("time_source")) or "default",
            breakdown_source=_norm_opt_str(d.get("breakdown_source")) or "default",
            diagnose_period_end=_norm_opt_str(d.get("diagnose_period_end")),
            xyz_axis1=_norm_opt_str(d.get("xyz_axis1")),
            clarify_message=_norm_opt_str(d.get("clarify_message")),
            depth="deep" if str(d.get("depth", "quick")).lower() == "deep" else "quick",
            threshold=int(d["threshold"]) if d.get("threshold") and str(d["threshold"]).isdigit() else None,
        )

    def to_dict(self) -> dict:
        """
        Canonical serialization of all QueryObject fields.

        Used by _format_history so every new field automatically appears in
        the orchestrator's conversation context without a manual update.
        """
        return {
            "analysis_type":        self.analysis_type,
            "metric_id":            self.metric_id,
            "event":                self.event,
            "metric_variant":       self.metric_variant,
            "metric_value_col":     self.metric_value_col,
            "metric_status_col":    self.metric_status_col,
            "metric_status_target": self.metric_status_target,
            "filters":              self.filters,
            "filter_excludes":      self.filter_excludes,
            "time_range_days":      self.time_range_days,
            "date_from":            self.date_from,
            "date_to":              self.date_to,
            "secondary_date_from":  self.secondary_date_from,
            "secondary_date_to":    self.secondary_date_to,
            "breakdown":            self.breakdown,
            "funnel_steps":         self.funnel_steps,
            "event_b":               self.event_b,
            "retention_window_days":        self.retention_window_days,
            "retention_window_days_source": self.retention_window_days_source,
            "activation_window_days":        self.activation_window_days,
            "activation_window_days_source": self.activation_window_days_source,
            "time_granularity":              self.time_granularity,
            "time_granularity_source":       self.time_granularity_source,
            "time_source":                   self.time_source,
            "breakdown_source":              self.breakdown_source,
            "diagnose_period_end":  self.diagnose_period_end,
            "xyz_axis1":            self.xyz_axis1,
            "threshold":            self.threshold,
            "clarify_message":      self.clarify_message,
            "depth":                self.depth,
        }

    def is_valid(self) -> tuple[bool, str]:
        """Basic structural validation. Returns (ok, error_message)."""
        if self.analysis_type not in ANALYSIS_TYPES:
            return False, f"Unknown analysis_type '{self.analysis_type}'"
        if self.analysis_type == "metric" and not self.event and not self.metric_id:
            return False, "metric requires 'event' or 'metric_id'"
        if self.analysis_type == "segment" and not self.breakdown:
            return False, "segment requires 'breakdown'"
        if self.analysis_type == "funnel" and len(self.funnel_steps) < 2:
            return False, "funnel requires at least 2 steps"
        if self.analysis_type == "funnel_compare" and len(self.funnel_steps) < 2:
            return False, "funnel_compare requires at least 2 steps"
        if self.analysis_type == "journey" and not self.event:
            return False, "journey requires 'event' (anchor / starting event)"
        if self.analysis_type == "retention" and not self.event and not self.metric_id:
            return False, "retention requires 'event' or 'metric_id'"
        if self.analysis_type == "behavioral_cohort" and not self.event:
            return False, "behavioral_cohort requires 'event' (the set users DID do)"
        if self.analysis_type == "time_between" and not self.event:
            return False, "time_between requires 'event' (start event)"
        if self.analysis_type == "same_month_anchor":
            if not self.event_b:
                return False, "same_month_anchor requires 'event_b' (anchor / lifecycle event)"
            if not self.event and not self.metric_id:
                return False, "same_month_anchor requires 'event' or 'metric_id' (activity cohort)"
            if self.event and self.event_b and self.event == self.event_b:
                return False, "same_month_anchor requires distinct activity and anchor when both are events"
        if self.analysis_type == "diagnose" and not self.event and not self.metric_id:
            return False, "diagnose requires 'event' or 'metric_id'"
        if self.analysis_type == "user_lifecycle" and not self.event:
            return False, "user_lifecycle requires 'event' (the core action event)"
        if self.analysis_type == "stickiness" and not self.event:
            return False, "stickiness requires 'event'"
        if self.analysis_type == "funnel_property_drilldown" and len(self.funnel_steps) < 2:
            return False, "funnel_property_drilldown requires at least 2 funnel_steps"
        if self.analysis_type == "xyz_matrix" and not self.event:
            return False, "xyz_matrix requires 'event'"
        return True, ""
