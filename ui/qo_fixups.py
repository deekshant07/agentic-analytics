"""
qo_fixups.py — Post-orchestration QueryObject repairs.

Called after orchestrate() returns a QO but before compile_query() runs.
Each function mutates qo in-place and is safe to call when qo is None or
the relevant data is missing (it degrades gracefully).

Typical call order in pipeline.get_sql():
    _maybe_resolve_clarify_as_followup(qo, history, catalog, prompt)
    _apply_time_intent_overrides(qo, history)
    _hydrate_retention_event_from_metric(qo, catalog)
    _hydrate_primary_event_for_action_types(qo, catalog)
    _apply_metric_variant_overrides(qo, catalog)
    _sanitize_invalid_breakdown(qo, sampled_values)
    _apply_followup_context_repair(qo, history, catalog)
    _apply_same_month_anchor_composite_metric(qo, metrics, prompt)
    _apply_lineage_rollforward_filters(qo, history, catalog)
    _materialize_metric_status_into_filters_for_equality_cohorts(qo)
    _strip_redundant_calendar_day_filter(qo)
"""
from __future__ import annotations

import re
from typing import Optional

from core.pipeline.activation_window import (
    apply_activation_window_from_prompt,
    apply_retention_window_from_prompt,
)
from core.sql.query_object import QueryObject
from core.semantic.qo_lineage import lookup_custom_event, merge_rollforward_filters


# ── 1. Time intent overrides ─────────────────────────────────────────────────

def _apply_time_intent_overrides(
    qo: QueryObject,
    history: Optional[list[dict]],
) -> None:
    """
    When the user didn't specify a time window (time_source == 'default'),
    carry forward the window/granularity from the most recent turn that had
    an explicit one.  Prevents "last month" from resetting to 30d on follow-ups.
    """
    if not history or not qo:
        return
    if getattr(qo, "time_source", "default") != "default":
        return

    for turn in reversed(history):
        if not isinstance(turn, dict):
            continue
        src = turn.get("time_source", "default")
        if src not in ("explicit", "inherited"):
            continue
        # Only inherit when the analysis type is the same broad category
        if turn.get("time_range_days") and not qo.date_from:
            qo.time_range_days = int(turn["time_range_days"])
            qo.time_source     = "inherited"
        if turn.get("time_granularity") and qo.time_granularity == "day":
            qo.time_granularity = turn["time_granularity"]
        break


# ── 2. Retention event hydration ─────────────────────────────────────────────

_GENERIC_RETENTION_EVENTS = frozenset({
    "app_opened", "app_open", "session_start", "page_view", "screen_view",
})
# Preferred metric IDs when the question is ambiguous — industry standard order.
_RETENTION_METRIC_PRIORITY = ["d7_retention", "d30_retention", "d1_retention"]


def _apply_retention_metric_to_qo(qo: QueryObject, m: dict) -> None:
    """
    Hydrate a QueryObject from a catalog retention metric's builder_definition.
    Sets event, event_b, retention_window_days, and merges filters.
    This ensures _compile_retention() receives the correct event and filter
    clauses rather than falling back to the generic 'app_opened' default.
    """
    qo.metric_id = m.get("id")
    bd = m.get("builder_definition") or {}

    primary = str(bd.get("primary_event") or "").strip()
    if not primary:
        # Fall back to top-level fields or sql_hint first event_name
        primary = str(m.get("event") or m.get("primary_event") or "").strip()
    if not primary:
        sql = m.get("sql_hint") or m.get("sql") or ""
        found = re.findall(r"event_name[`\"']?\s*=\s*'([^']+)'", sql, re.IGNORECASE)
        primary = found[0] if found else ""

    if primary:
        qo.event   = primary
        # Retention: cohort event and return event are the same unless explicitly different.
        if not qo.event_b or qo.event_b in _GENERIC_RETENTION_EVENTS:
            qo.event_b = primary

    # Catalog default window — skip when prompt or prior turn already set N-day window.
    if not getattr(qo, "_retention_window_explicit", False) and not getattr(
        qo, "_retention_window_from_history", False
    ):
        win = bd.get("retention_days") or bd.get("retention_window_days")
        if win:
            qo.retention_window_days = int(win)

    # Merge structured filters (e.g. transaction_status = SUCCESS) into qo.filters
    # so _filters_clause() picks them up when building the cohort and return CTEs.
    structured = bd.get("filters_structured") or []
    cur_filters = dict(qo.filters or {})
    for f in structured:
        if not isinstance(f, dict):
            continue
        col = str(f.get("field") or "").strip()
        val = str(f.get("value") or "").strip()
        op  = str(f.get("op") or "=").strip()
        if col and val and op == "=":
            cur_filters.setdefault(col, val)
    if cur_filters:
        qo.filters = cur_filters


def _hydrate_retention_event_from_metric(
    qo: QueryObject,
    catalog: Optional[dict],
) -> None:
    """
    For retention queries, ensure a pre-built catalog retention metric drives
    event, event_b, window, and filters — rather than letting the compiler fall
    back to the generic 'app_opened' default.

    Two cases handled:
      A) metric_id already set → apply builder_definition from that specific metric.
      B) metric_id is null OR the LLM guessed a generic session event → pick the
         best approved retention metric from the catalog and apply it.
    """
    if not qo or qo.analysis_type != "retention" or not catalog:
        return

    mid = getattr(qo, "metric_id", None)

    # Case A: metric_id already set — apply its builder_definition.
    if mid:
        for tdata in catalog.values():
            if not isinstance(tdata, dict):
                continue
            for m in tdata.get("suggested_metrics", []):
                if m.get("id") != mid:
                    continue
                _apply_retention_metric_to_qo(qo, m)
                return
        return  # metric_id set but not found — leave as-is

    # Case B: no metric_id, or the LLM guessed a generic session event.
    guessed_generic = (not mid) or (
        str(qo.event or "").strip() in _GENERIC_RETENTION_EVENTS
        or str(qo.event_b or "").strip() in _GENERIC_RETENTION_EVENTS
    )
    if not guessed_generic:
        return

    retention_metrics: list[dict] = []
    for tdata in catalog.values():
        if not isinstance(tdata, dict):
            continue
        for m in tdata.get("suggested_metrics", []):
            if m.get("status") in ("rejected", "suppressed"):
                continue
            mtype = (m.get("type") or m.get("category") or "").lower()
            if "retention" in mtype or (m.get("id") or "").endswith("_retention"):
                retention_metrics.append(m)

    if not retention_metrics:
        return  # no catalog retention metrics — leave the LLM's choice as-is

    # Pick in priority order, then by confidence, then first found.
    chosen = None
    for preferred_id in _RETENTION_METRIC_PRIORITY:
        for m in retention_metrics:
            if m.get("id") == preferred_id:
                chosen = m
                break
        if chosen:
            break
    if not chosen:
        chosen = retention_metrics[0]

    _apply_retention_metric_to_qo(qo, chosen)


# ── 3. Stickiness / lifecycle primary-event hydration ────────────────────────

# Analysis types where a generic session event is meaningless for a fintech product.
# If the LLM provides one of these events, it likely guessed rather than reasoned.
_GENERIC_SESSION_EVENTS = frozenset({
    "app_opened", "app_open", "session_start", "page_view", "screen_view",
})
# Catalog event categories that represent the core product action (transact/purchase/pay).
# Deliberately excludes "onboarding" (KYC steps) and "engagement" (passive app events).
_PRODUCT_ACTION_CATEGORIES = frozenset({"payments", "transactions", "purchase", "checkout"})


def _hydrate_primary_event_for_action_types(
    qo: QueryObject,
    catalog: Optional[dict],
) -> None:
    """
    For stickiness and user_lifecycle: if the LLM left event=None or chose a
    generic session event, replace it with the most prominent product-action event
    from the catalog (non-session, highest-confidence event in a payments/activation
    category).  Mirrors the retention fixup logic but uses catalog event vocabulary
    rather than pre-built metrics.

    If no catalog action event is found, leaves event as-is so that is_valid()
    downstream raises a clarify request rather than silently using "app_opened".
    """
    if not qo or not catalog:
        return
    if qo.analysis_type not in ("stickiness", "user_lifecycle"):
        return

    # Only override when event is absent or is a known generic session placeholder.
    cur_ev = str(getattr(qo, "event", None) or "").strip()
    if cur_ev and cur_ev not in _GENERIC_SESSION_EVENTS:
        return  # LLM set a specific event — trust it

    # Scan catalog for approved non-session events in product-action categories.
    candidates: list[dict] = []
    for tdata in catalog.values():
        if not isinstance(tdata, dict):
            continue
        for ev in tdata.get("events", []):
            if not isinstance(ev, dict):
                continue
            raw = str(ev.get("raw_name") or "").strip()
            if not raw or raw in _GENERIC_SESSION_EVENTS:
                continue
            if ev.get("status") in ("rejected", "suppressed"):
                continue
            groups = ev.get("groups") or {}
            category = str(groups.get("event_category") or "").lower()
            if category in _PRODUCT_ACTION_CATEGORIES:
                conf = float(ev.get("confidence", 0) or 0)
                candidates.append((conf, raw, ev))

    if not candidates:
        # No catalog action events — clear any generic guess so is_valid() fires.
        if cur_ev in _GENERIC_SESSION_EVENTS:
            qo.event = None
        return

    # Pick highest-confidence action event; stable sort within equal confidence.
    candidates.sort(key=lambda x: (-x[0], x[1]))
    qo.event = candidates[0][1]


# ── 5. Metric variant overrides ───────────────────────────────────────────────

def _apply_metric_variant_overrides(
    qo: QueryObject,
    catalog: Optional[dict],
) -> None:
    """
    When the matched pre-built metric specifies a metric_variant, status_col,
    or value_col, copy those into the QueryObject so the compiler picks them up.
    """
    if not qo or not catalog:
        return
    mid = getattr(qo, "metric_id", None)
    if not mid:
        return

    for tdata in catalog.values():
        if not isinstance(tdata, dict):
            continue
        for m in tdata.get("suggested_metrics", []):
            if m.get("id") != mid:
                continue
            if m.get("metric_variant") and not qo.metric_variant:
                qo.metric_variant = m["metric_variant"]
            if m.get("metric_status_col") and not qo.metric_status_col:
                qo.metric_status_col = m["metric_status_col"]
            if m.get("metric_status_target") and not qo.metric_status_target:
                qo.metric_status_target = m["metric_status_target"]
            if m.get("metric_value_col") and not qo.metric_value_col:
                qo.metric_value_col = m["metric_value_col"]
            return


# ── 4. Invalid breakdown sanitization ────────────────────────────────────────

_TEMPORAL_BREAKDOWN_RE = re.compile(
    r"\b(month|week|day|year|date|cohort)\b", re.IGNORECASE
)


def _sanitize_invalid_breakdown(
    qo: QueryObject,
    sampled_values: Optional[dict],
) -> None:
    """
    Clear qo.breakdown when the requested column doesn't appear in the events
    table or has only one distinct value (not useful for slicing).
    Falls back to 'platform' which is always available.

    Temporal aliases like "month_name" are cleared to None — the compiler uses
    time_granularity to drive grouping and no dimensional breakdown is needed.
    """
    if not qo or qo.analysis_type != "segment" or not qo.breakdown:
        return

    bd = str(qo.breakdown).strip()

    # Temporal alias (e.g. "month_name", "cohort_month"): LLM used a time phrase as
    # breakdown. Clear it so the compiler emits a clean temporal trend, not a bogus
    # MAX(month_name) GROUP BY that errors or returns an unexpected shape.
    if _TEMPORAL_BREAKDOWN_RE.search(bd):
        qo.breakdown = None
        return

    if not sampled_values:
        return

    event_vals = sampled_values.get("events", {})

    if bd not in event_vals:
        qo.breakdown = "platform"
        return

    vals = event_vals.get(bd, [])
    if len(vals) < 2:
        qo.breakdown = "platform"


# ── 4b. Invalid filter value → IS NOT NULL via custom event semantic lookup ───

_IS_NOT_NULL_SENTINEL = "__IS_NOT_NULL__"
_IS_NULL_SENTINEL     = "__IS_NULL__"


def _remap_invalid_filter_values_via_custom_events(
    qo: QueryObject,
    sampled_values: Optional[dict],
    catalog: Optional[dict],
) -> None:
    """
    When a filter value doesn't exist in sampled data (e.g. transaction_channel='in_app'),
    look for a custom event that defines an IS NOT NULL condition on that column.
    If found, replace the invalid value with the IS_NOT_NULL sentinel so the compiler
    renders it as ``col IS NOT NULL`` rather than ``col = 'in_app'`` (which returns 0 rows).

    Example: "IN APP activation" → orchestrator sets filters={transaction_channel: "in_app"}
    → "in_app" not in sampled values → in_app_transacting_users CE has transaction_channel IS NOT NULL
    → replace with filters={transaction_channel: "__IS_NOT_NULL__"}
    """
    if not qo or not isinstance(getattr(qo, "filters", None), dict):
        return
    if not sampled_values or not catalog:
        return

    # Collect all sampled column values (case-insensitive) across all tables
    all_col_vals: dict[str, set[str]] = {}
    for tdata in sampled_values.values():
        if not isinstance(tdata, dict):
            continue
        for col, vals in tdata.items():
            lk = str(col).lower()
            if lk not in all_col_vals:
                all_col_vals[lk] = set()
            for v in (vals or []):
                all_col_vals[lk].add(str(v).lower())

    # Build lookup: col -> set of ops from custom events (IS NOT NULL / IS NULL)
    # ce_col_ops[col_lower] = {"IS NOT NULL", "IS NULL"}
    ce_col_ops: dict[str, set[str]] = {}
    biz = (catalog or {}).get("__business_context__", {}) or {}
    for ce in (biz.get("custom_events") or []):
        if not isinstance(ce, dict):
            continue
        for group in ((ce.get("builder_definition") or {}).get("groups") or []):
            if not isinstance(group, dict):
                continue
            for rule in (group.get("filters") or []):
                if not isinstance(rule, dict):
                    continue
                field = str(rule.get("field") or "").strip().lower()
                op    = str(rule.get("op") or "").strip().upper()
                if field and op in ("IS NOT NULL", "IS NULL"):
                    ce_col_ops.setdefault(field, set()).add(op)

    updated = dict(qo.filters)
    changed = False
    for col, val in list(updated.items()):
        if not isinstance(val, str):
            continue
        if val in (_IS_NOT_NULL_SENTINEL, _IS_NULL_SENTINEL):
            continue
        lk = str(col).lower()
        col_samples = all_col_vals.get(lk, set())
        if not col_samples:
            continue  # column not sampled — can't verify; leave as-is
        if str(val).lower() in col_samples:
            continue  # valid value; no remapping needed
        # Value not found in sampled data — check custom events
        ops = ce_col_ops.get(lk, set())
        if "IS NOT NULL" in ops:
            updated[col] = _IS_NOT_NULL_SENTINEL
            changed = True
        elif "IS NULL" in ops:
            updated[col] = _IS_NULL_SENTINEL
            changed = True

    if changed:
        qo.filters = updated


# ── 5. Follow-up context repair ───────────────────────────────────────────────

# Short phrases that indicate a *display modifier* follow-up (user wants the
# same query re-run with a different chart type, not a brand-new query).
_DISPLAY_MODIFIER_SIGNALS = frozenset({
    "percentage", "percent", "pct", "donut", "pie", "share",
    "as a donut", "as a pie", "as a percentage", "percentage split",
    "show percentage", "show as percentage", "show as donut",
    "bar chart", "bar graph", "show as bar", "show count", "just count",
    "stacked", "100%", "100 percent", "normalized", "normalised",
    "mom split", "mom", "month over month", "m.o.m",
})

_MOM_TREND_RE = re.compile(
    r"\b(mom|m\.o\.m|month\s+over\s+month|monthly\s+trend)\b",
    re.IGNORECASE,
)


def _empty_qo_slot(val) -> bool:
    return val is None or val == "" or val == [] or val == {}


def _usable_history_turn(turn: dict) -> bool:
    pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
    at = str(pq.get("analysis_type") or turn.get("analysis_type") or "").strip()
    return at not in ("clarify", "out_of_scope", "diagnose")


def _infer_custom_event_name_from_turn(turn: dict) -> Optional[str]:
    """Resolve catalog custom-event name from persisted turn metadata."""
    ce = (turn.get("custom_event_name") or "").strip()
    if ce:
        return ce
    pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
    mid = str(pq.get("metric_id") or "").strip()
    if mid.startswith("ce_"):
        return mid[3:]
    for key in ("_executed_custom_event_name", "_bound_custom_event_name"):
        raw = pq.get(key)
        if raw:
            return str(raw).strip()
    mn = re.sub(r"[^a-z0-9]+", "_", (turn.get("metric_name") or "").lower()).strip("_")
    if mn in ("transacting_user", "transacting_users"):
        return "transacting_user"
    if mn in ("active_user", "jupiter_active_user"):
        return mn
    q = (turn.get("question") or "").lower()
    if re.search(r"\btransact(?:ing)?\s+users?\b", q):
        return "transacting_user"
    if re.search(r"\bactive\s+users?\b", q):
        return "active_user"
    return None


def _bind_custom_event_on_qo(qo: QueryObject, ce_name: str) -> None:
    ce_name = str(ce_name).strip()
    if not ce_name:
        return
    setattr(qo, "_bound_custom_event_name", ce_name)
    qo.metric_id = f"ce_{ce_name}"


def _find_metric_anchor_turn(history: list[dict]) -> Optional[dict]:
    """Most recent turn with a identifiable metric / custom-event (not clarify)."""
    for turn in reversed(history):
        if not isinstance(turn, dict) or not _usable_history_turn(turn):
            continue
        pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
        if (
            _infer_custom_event_name_from_turn(turn)
            or turn.get("metric_name")
            or pq.get("metric_id")
        ):
            return turn
    return None


def _maybe_resolve_clarify_as_followup(
    qo: QueryObject,
    history: Optional[list[dict]],
    catalog: Optional[dict],
    prompt: Optional[str],
) -> None:
    """
    When the orchestrator returns ``clarify`` for a short phrase that is clearly
    a *display modifier* follow-up ("show as percentage", "show as donut",
    "can you show percentage split", etc.), inherit the prior turn's full query
    context so the pipeline can re-run the same query instead of asking the user
    to rephrase.

    Only acts when:
      - analysis_type is "clarify"
      - The prompt is short (≤ 14 words) and contains a display modifier signal
      - There is a usable prior turn in history
    """
    if not qo or qo.analysis_type != "clarify":
        return
    if not history or not prompt:
        return

    pl = prompt.lower().strip()
    if len(pl.split()) > 14:
        return
    if not any(sig in pl for sig in _DISPLAY_MODIFIER_SIGNALS):
        return

    # Find the most recent actionable turn
    prev_qo_dict: dict = {}
    prev_ce_name: Optional[str] = None
    for turn in reversed(history):
        if not isinstance(turn, dict):
            continue
        pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
        at = str(pq.get("analysis_type") or turn.get("analysis_type") or "")
        if at in ("clarify", "out_of_scope"):
            continue
        if at:
            prev_qo_dict = pq or {}
            prev_ce_name = turn.get("custom_event_name")
            break

    if not prev_qo_dict:
        return

    # Inherit all key QO fields from the prior turn
    _INHERIT_FIELDS = (
        "analysis_type", "event", "metric_id", "breakdown", "filters",
        "time_range_days", "date_from", "date_to", "time_granularity",
        "event_b", "funnel_steps", "segment",
    )
    for field in _INHERIT_FIELDS:
        val = prev_qo_dict.get(field)
        if val is not None:
            try:
                setattr(qo, field, val)
            except Exception:
                pass

    # If analysis_type is still clarify after inheritance, bail — we couldn't fix it
    if str(getattr(qo, "analysis_type", "") or "") in ("clarify", "out_of_scope", ""):
        return

    setattr(qo, "_clarify_resolved_as_followup", True)
    setattr(qo, "_resolved_from_ce_name", prev_ce_name)


def _apply_retention_context_from_history(
    qo: QueryObject,
    prompt: Optional[str],
    history: Optional[list[dict]],
) -> None:
    """
    Inherit retention contract from the latest prior retention turn (e.g. 14-day MOM
    then "broken down by platform") without requiring the word "same".
    """
    if not history or not qo or getattr(qo, "analysis_type", "") != "retention":
        return

    for turn in reversed(history):
        if not isinstance(turn, dict):
            continue
        pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
        if str(pq.get("analysis_type") or "").strip() != "retention":
            continue

        def _empty(val) -> bool:
            return val is None or val == "" or val == [] or val == {}

        if not getattr(qo, "_retention_window_explicit", False):
            prev_win = pq.get("retention_window_days")
            if prev_win is not None:
                qo.retention_window_days = int(prev_win)
                setattr(qo, "_retention_window_from_history", True)

        for slot in (
            "event",
            "event_b",
            "metric_id",
            "filters",
            "time_range_days",
            "time_granularity",
            "date_from",
            "date_to",
        ):
            if not _empty(getattr(qo, slot, None)):
                continue
            prev = pq.get(slot)
            if not _empty(prev):
                setattr(qo, slot, prev)

        if getattr(qo, "time_source", "default") == "default" and pq.get("time_source"):
            qo.time_source = pq.get("time_source")
        break


def _apply_same_query_followup(
    qo: QueryObject,
    prompt: Optional[str],
    history: Optional[list[dict]],
) -> None:
    """
    When the user says "same …" (e.g. same retention metric for 14 days), inherit
    the prior turn's query slots the LLM often drops.

    Metric identity comes from the **most recent** turn that named a metric / custom
    event (e.g. Q3 transacting users), not an older turn (e.g. Q1 active users).
    """
    if not history or not qo or not prompt:
        return
    if not re.search(r"\bsame\b", prompt.lower()):
        return
    if qo.analysis_type in ("clarify", "out_of_scope"):
        return

    context_turn: Optional[dict] = None
    for turn in reversed(history):
        if isinstance(turn, dict) and _usable_history_turn(turn):
            context_turn = turn
            break
    if not context_turn:
        return

    pq_ctx = context_turn.get("qo") if isinstance(context_turn.get("qo"), dict) else {}

    for slot in (
        "analysis_type",
        "event",
        "event_b",
        "time_range_days",
        "time_granularity",
        "retention_window_days",
        "date_from",
        "date_to",
    ):
        if not _empty_qo_slot(getattr(qo, slot, None)):
            continue
        prev = pq_ctx.get(slot)
        if not _empty_qo_slot(prev):
            setattr(qo, slot, prev)

    if getattr(qo, "time_source", "default") == "default" and pq_ctx.get("time_source"):
        qo.time_source = pq_ctx["time_source"]

    # Metric contract: latest anchor overrides orchestrator picking an older metric.
    anchor = _find_metric_anchor_turn(history)
    if anchor:
        pq_anchor = anchor.get("qo") if isinstance(anchor.get("qo"), dict) else {}
        ce_name = _infer_custom_event_name_from_turn(anchor)
        if ce_name:
            _bind_custom_event_on_qo(qo, ce_name)
            anchor_filters = pq_anchor.get("filters")
            if anchor_filters is not None:
                qo.filters = dict(anchor_filters) if anchor_filters else None
            elif ce_name == "transacting_user":
                qo.filters = None
        elif pq_anchor.get("metric_id"):
            qo.metric_id = pq_anchor["metric_id"]
        if not _empty_qo_slot(pq_anchor.get("event")) and _empty_qo_slot(getattr(qo, "event", None)):
            qo.event = pq_anchor["event"]

    # GROUP BY dimension must not also be filtered to IS NOT NULL only.
    bd = getattr(qo, "breakdown", None)
    if bd:
        flt = dict(getattr(qo, "filters", None) or {})
        key = str(bd).strip()
        if key in flt and flt[key] in (_IS_NOT_NULL_SENTINEL, _IS_NULL_SENTINEL):
            del flt[key]
            qo.filters = flt or None


def _apply_display_modifier_followup(
    qo: QueryObject,
    prompt: Optional[str],
    history: Optional[list[dict]],
) -> None:
    """
    Short display follow-ups ("share mom split", "show percentage") re-run the prior
    query with MoM monthly buckets instead of a single aggregated window.
    """
    if not history or not qo or not prompt:
        return
    if qo.analysis_type in ("clarify", "out_of_scope"):
        return

    pl = prompt.lower().strip()
    if len(pl.split()) > 16:
        return
    if not any(sig in pl for sig in _DISPLAY_MODIFIER_SIGNALS):
        return

    has_share = any(w in pl for w in ("share", "percentage", "percent", "pct", "split", "stacked"))
    has_mom = bool(_MOM_TREND_RE.search(pl))
    if not has_share and not has_mom:
        return

    target: Optional[dict] = None
    for turn in reversed(history):
        if not isinstance(turn, dict) or not _usable_history_turn(turn):
            continue
        pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
        if pq.get("analysis_type") == "metric" and pq.get("time_granularity") == "month":
            target = turn
            break
    if not target:
        for turn in reversed(history):
            if not isinstance(turn, dict) or not _usable_history_turn(turn):
                continue
            pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
            if has_mom or pq.get("time_granularity") == "month":
                target = turn
                break
    if not target:
        return

    pq = target.get("qo") if isinstance(target.get("qo"), dict) else {}
    ce_name = _infer_custom_event_name_from_turn(target)
    if ce_name:
        _bind_custom_event_on_qo(qo, ce_name)
    elif pq.get("metric_id"):
        qo.metric_id = pq["metric_id"]

    if has_mom or pq.get("time_granularity") == "month":
        qo.analysis_type = "metric"
        qo.time_granularity = "month"
        qo.breakdown = None
        if _empty_qo_slot(getattr(qo, "time_range_days", None)):
            qo.time_range_days = pq.get("time_range_days") or 180
        qo.time_source = "inherited"


def _sanitize_retention_dimension_status(qo: QueryObject) -> None:
    """Drop metric_status_* when it duplicates a dimension already in ``filters``."""
    if getattr(qo, "analysis_type", "") != "retention":
        return
    col = getattr(qo, "metric_status_col", None)
    if not col:
        return
    flt = dict(getattr(qo, "filters", None) or {})
    if col in flt or col.lower() in {k.lower() for k in flt}:
        qo.metric_status_col = None
        qo.metric_status_target = None


def _apply_followup_context_repair(
    qo: QueryObject,
    history: Optional[list[dict]],
    catalog: Optional[dict] = None,
) -> None:
    """
    When a follow-up drops event/metric slots, fill gaps from the latest usable
    history turn.  ``get_qo_history`` nests slots under ``turn["qo"]`` — read
    both nested and flat shapes for compatibility.

    Also: if the prior turn only persisted a ``custom_event_name`` (no base
    event), infer ``qo.event`` from that CE's builder_definition when needed.
    """
    if not history or not qo:
        return
    if qo.analysis_type in ("clarify", "out_of_scope"):
        return

    prev_event: Optional[str] = None
    prev_metric_id: Optional[str] = None
    prev_ce_name: Optional[str] = None

    for turn in reversed(history):
        if not isinstance(turn, dict):
            continue
        pq = turn.get("qo") if isinstance(turn.get("qo"), dict) else {}
        at = str(pq.get("analysis_type") or turn.get("analysis_type") or "")
        if at in ("clarify", "out_of_scope", "diagnose"):
            continue
        ev = pq.get("event") or turn.get("event")
        mid = pq.get("metric_id") or turn.get("metric_id")
        pce = turn.get("custom_event_name")
        ev = str(ev).strip() if ev else None
        mid = str(mid).strip() if mid else None
        pce = str(pce).strip() if pce else None
        if ev or mid or pce:
            prev_event = ev
            prev_metric_id = mid
            prev_ce_name = pce
            break

    cur_ev = str(getattr(qo, "event", None) or "").strip()
    cur_mid = str(getattr(qo, "metric_id", None) or "").strip()
    if not cur_ev and prev_event:
        qo.event = prev_event
    if not cur_mid and prev_metric_id:
        qo.metric_id = prev_metric_id

    if (
        catalog
        and not str(getattr(qo, "event", None) or "").strip()
        and not str(getattr(qo, "metric_id", None) or "").strip()
        and prev_ce_name
    ):
        ce = lookup_custom_event(catalog, prev_ce_name)
        if ce:
            bd = ce.get("builder_definition") or {}
            for g in bd.get("groups") or []:
                if isinstance(g, dict):
                    ev0 = str(g.get("event") or "").strip()
                    if ev0:
                        qo.event = ev0
                        break


def _activity_event_for_same_month_validation(sql: str, anchor_event: Optional[str]) -> Optional[str]:
    """First event_name in metric sql that is not the lifecycle anchor (for QO slots)."""
    if not sql:
        return None
    found = re.findall(
        r"event_name[`\"']?\s*=\s*'([^']+)'",
        sql,
        flags=re.IGNORECASE,
    )
    anc = (anchor_event or "").strip()
    for ev in found:
        if ev != anc:
            return ev
    return found[0] if found else None


def _apply_same_month_anchor_composite_metric(
    qo: QueryObject,
    metrics: Optional[list[dict]],
    prompt: Optional[str],
) -> None:
    """
    If the user names a composite catalog cohort (metric SQL with OR / multiple
    ``event_name``) while the model emitted only a primitive ``event``, set
    ``metric_id`` so ``compile_query`` can use the catalog predicate.

    Matching is vocabulary-driven: every significant word in the metric *display name*
    must appear in the question; the longest matching name wins. No product-specific
    keywords.
    """
    if not qo or not prompt:
        return
    if getattr(qo, "analysis_type", None) != "same_month_anchor":
        return
    if getattr(qo, "metric_id", None):
        return

    pl = re.sub(r"[^a-z0-9\s]", " ", prompt.lower())
    pl = re.sub(r"\s+", " ", pl).strip()
    if not pl:
        return

    best: Optional[tuple[int, str, str, str]] = None  # len(name), id, display_name, sql

    for m in metrics or []:
        mid = (m.get("id") or "").strip()
        sq = (m.get("sql") or "").strip()
        if not mid or not sq:
            continue
        name_raw = (m.get("name") or "").strip()
        if not name_raw:
            continue
        tokens = [t for t in re.split(r"\s+", name_raw.lower()) if len(t) > 1]
        if not tokens:
            continue
        if not all(t in pl for t in tokens):
            continue
        sq_l = sq.lower()
        is_composite = " or " in sq_l or sq_l.count("event_name") >= 2
        if not is_composite:
            continue
        key = (len(name_raw), mid, name_raw, sq)
        if best is None or key[0] > best[0]:
            best = key

    if not best:
        return

    _, mid, name_raw, sq = best
    qo.metric_id = mid
    setattr(qo, "_same_month_activity_label", name_raw)
    ev_slot = _activity_event_for_same_month_validation(sq, getattr(qo, "event_b", None))
    if ev_slot:
        qo.event = ev_slot


_SPEND_WORDS = frozenset({"spend", "amount", "value", "revenue", "cost", "price"})


def _inherit_metric_status_and_filters(
    qo: QueryObject,
    history: Optional[list[dict]],
    question: str = "",
) -> None:
    """
    Deterministic follow-up inheritance — runs AFTER _apply_followup_context_repair
    so qo.event is already populated.

    When the new QO shares the same event as the most recent valid history turn,
    inherit three things the LLM routinely drops on follow-up questions:
      1. metric_status_col / metric_status_target (e.g. transaction_status=SUCCESS)
      2. Dimension filters the new QO doesn't explicitly override (e.g. channel=UPI)
      3. per_user_value → per_user_count when prior context was count-based and
         the question contains no spend-related words
    """
    if not history or not qo:
        return
    if getattr(qo, "analysis_type", "") in ("clarify", "out_of_scope"):
        return

    cur_event = str(getattr(qo, "event", None) or "").strip()
    if not cur_event:
        return

    for turn in reversed(history):
        if not isinstance(turn, dict):
            continue
        prev_qo = turn.get("qo") or {}
        prev_at = str(prev_qo.get("analysis_type") or "").strip()
        if prev_at in ("clarify", "out_of_scope"):
            continue
        prev_event = str(prev_qo.get("event") or "").strip()
        if prev_event != cur_event:
            continue

        # 1. Inherit metric_status_col / metric_status_target if absent
        if not qo.metric_status_col and prev_qo.get("metric_status_col"):
            qo.metric_status_col    = prev_qo["metric_status_col"]
            qo.metric_status_target = prev_qo.get("metric_status_target")

        # 2. Roll forward filters the new QO doesn't explicitly set
        prev_filters   = dict(prev_qo.get("filters") or {})
        cur_filters    = dict(getattr(qo, "filters", None) or {})
        cur_keys_lower = {k.lower() for k in cur_filters}
        for k, v in prev_filters.items():
            if k.lower() not in cur_keys_lower and v is not None:
                cur_filters[k] = v
        qo.filters = cur_filters or None

        # 3. Correct per_user_value → per_user_count when prior context was count-based
        prev_variant = (prev_qo.get("metric_variant") or "").lower()
        if (
            getattr(qo, "metric_variant", None) == "per_user_value"
            and not any(w in question.lower() for w in _SPEND_WORDS)
            and prev_variant != "per_user_value"
        ):
            qo.metric_variant   = "per_user_count"
            qo.metric_value_col = None

        break  # only inherit from the most recent matching turn


def _apply_activation_window_from_prompt(
    qo: QueryObject,
    prompt: Optional[str],
    metrics: Optional[list[dict]] = None,
) -> None:
    """Delegate to ``core.pipeline.activation_window`` (single source of truth)."""
    apply_activation_window_from_prompt(qo, prompt, metrics=metrics)


def _apply_retention_window_from_prompt(
    qo: QueryObject,
    prompt: Optional[str],
    catalog: Optional[dict] = None,
) -> None:
    """Delegate to ``core.pipeline.activation_window`` (after metric hydration)."""
    apply_retention_window_from_prompt(qo, prompt, catalog=catalog)


def _apply_lineage_rollforward_filters(
    qo: QueryObject,
    history: Optional[list[dict]],
    catalog: Optional[dict],
) -> None:
    """Delegate to ``core.qo_lineage`` (catalog-driven rollups, not ad-hoc rules)."""
    merge_rollforward_filters(qo, history, catalog)


def _materialize_metric_status_into_filters_for_equality_cohorts(qo: QueryObject) -> None:
    """
    The orchestrator sometimes places an equality slice in ``metric_status_col`` /
    ``metric_status_target`` (e.g. SUCCESS) instead of ``filters``.  Compilers that
    only read ``filters`` for the activity predicate (e.g. ``same_month_anchor``) would
    otherwise drop it.  Fold into ``filters`` when this is not a true ``status_rate``
    ratio query (those need numerator/denominator semantics on the metric compiler path).
    """
    if not qo:
        return
    if str(getattr(qo, "metric_variant", "") or "").strip() == "status_rate":
        return
    if getattr(qo, "analysis_type", "") != "same_month_anchor":
        return
    sc = getattr(qo, "metric_status_col", None)
    tgt = getattr(qo, "metric_status_target", None)
    if not sc or not tgt:
        return
    lk = str(sc).strip().lower()
    filt = dict(qo.filters or {})
    if any(str(k).strip().lower() == lk for k in filt):
        return
    filt[str(sc).strip()] = str(tgt).strip()
    qo.filters = filt


# ── 6. Clarify context extraction ────────────────────────────────────────────

def _strip_redundant_calendar_day_filter(qo: QueryObject) -> None:
    """
    If the orchestrator set both date_from/date_to (full month) and filters.date = date_from,
    the day filter collapses the cohort to a single calendar day. Drop filters.date (and
    similar keys) when they duplicate the window start.
    """
    if not qo or not isinstance(qo.filters, dict):
        return
    df = getattr(qo, "date_from", None)
    if not df:
        return
    d0 = str(df)[:10]
    for key in list(qo.filters.keys()):
        lk = str(key).lower()
        if lk not in ("date", "event_date", "dt", "day"):
            continue
        v = str(qo.filters.get(key, ""))[:10]
        if v == d0:
            qo.filters.pop(key, None)


def _extract_clarify_context(history: Optional[list[dict]]) -> Optional[str]:
    """
    If the last assistant turn was a clarification request, return its message
    so the orchestrator can inject it as follow-up context.
    """
    if not history:
        return None
    for turn in reversed(history):
        if not isinstance(turn, dict):
            continue
        if turn.get("analysis_type") == "clarify" and turn.get("answer"):
            return str(turn["answer"])[:400]
    return None
