"""
QueryObject lineage — carry cohort-defining filters across follow-up turns.

Custom-event SQL can encode constraints that never appear in ``qo.filters``,
so a later ``segment`` / ``metric`` compile loses them.  Instead of
hard-coding product rules in ``ui/qo_fixups.py``:

1. **Catalog** — each ``custom_events[]`` entry may define equality cohort
   slices explicitly or via ``builder_definition`` (``op: "="`` only).
2. **Optional** ``__business_context__.lineage`` — tune analysis types,
   metric variants to skip, and extra keys to copy from the prior turn's
   saved filters.

See ``merge_rollforward_filters`` (before compile) and
``materialize_rollups_on_qo`` (before persisting a turn).
"""
from __future__ import annotations

from typing import Any, Optional


def _business_context(catalog: Optional[dict]) -> dict:
    return ((catalog or {}).get("__business_context__") or {}) if catalog else {}


def lineage_config(catalog: Optional[dict]) -> dict:
    raw = _business_context(catalog).get("lineage")
    return raw if isinstance(raw, dict) else {}


def _merged_runtime_config(catalog: Optional[dict]) -> dict[str, Any]:
    lc = lineage_config(catalog)
    rat = lc.get("rollup_analysis_types")
    smv = lc.get("skip_metric_variants")
    rfk = lc.get("rollforward_keys")
    return {
        "rollup_analysis_types": frozenset(
            str(x).strip() for x in (rat or ["metric", "segment"]) if str(x).strip()
        ),
        "skip_metric_variants": frozenset(
            str(x).strip() for x in (smv or ["status_rate", "event_count"]) if str(x).strip()
        ),
        "rollforward_keys": [
            str(x).strip() for x in (rfk or []) if str(x).strip()
        ],
    }


def iter_custom_events(catalog: Optional[dict]) -> list[dict]:
    raw = _business_context(catalog).get("custom_events")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def lookup_custom_event(catalog: Optional[dict], name: Optional[str]) -> Optional[dict]:
    if not name or not catalog:
        return None
    target = str(name).strip().lower().replace(" ", "_")
    for ce in iter_custom_events(catalog):
        n = str(ce.get("name") or "").strip().lower().replace(" ", "_")
        if n == target:
            return ce
    return None


def _equality_filters_from_group(group: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for rule in group.get("filters") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("op") or "").strip() != "=":
            continue
        field = str(rule.get("field") or "").strip()
        val = rule.get("value")
        if not field or val is None:
            continue
        vs = str(val).strip()
        if not vs:
            continue
        out[field] = vs
    return out


def rollforward_slice_from_custom_event(ce: dict, anchor_event: str) -> dict[str, str]:
    """
    Equality-style filters that define the custom cohort for ``anchor_event``.

    Precedence:
    1. ``rollup_filters`` on the CE (explicit dict), if non-empty.
    2. ``builder_definition.groups[]`` — for each group whose ``event`` matches
       ``anchor_event``, merge all ``op: "="`` rules.
    """
    explicit = ce.get("rollup_filters")
    if isinstance(explicit, dict) and explicit:
        merged: dict[str, str] = {}
        for k, v in explicit.items():
            if v is None:
                continue
            vs = str(v).strip()
            if not vs:
                continue
            merged[str(k)] = vs
        return merged

    anchor = (anchor_event or "").strip()
    bd = ce.get("builder_definition") or {}
    groups = bd.get("groups") or []
    merged2: dict[str, str] = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        ev = str(g.get("event") or "").strip()
        if anchor and ev != anchor:
            continue
        merged2.update(_equality_filters_from_group(g))
    return merged2


def _filter_keys_lower(f: dict) -> set[str]:
    return {str(k).lower() for k in f}


def merge_rollforward_filters(
    qo: Any,
    history: Optional[list[dict]],
    catalog: Optional[dict],
) -> None:
    """
    For the most recent history turn with the same ``event`` as ``qo``,
    merge cohort-defining filters that are missing on the current QO.

    Sources (in order, unioned before applying):
    - Custom event on that turn: ``rollforward_slice_from_custom_event``
    - Catalog ``lineage.rollforward_keys``: copy those keys from the prior
      turn's saved ``filters`` if still absent.
    """
    cfg = _merged_runtime_config(catalog)
    if not qo or not history:
        return

    at = getattr(qo, "analysis_type", None)
    if at not in cfg["rollup_analysis_types"]:
        return

    mv = str(getattr(qo, "metric_variant", "") or "").strip()
    if mv in cfg["skip_metric_variants"]:
        return

    cur_event = str(getattr(qo, "event", None) or "").strip()
    cur_metric_id = str(getattr(qo, "metric_id", None) or "").strip()

    if not cur_event:
        # Pre-built metric follow-ups have event=None but metric_id set.
        # Carry forward all filters from the most recent turn with the same metric_id
        # so user-supplied dimension slices (e.g. transaction_channel=UPI) survive
        # granularity/time changes like "share monthly trend".
        if not cur_metric_id:
            return
        filt = dict(getattr(qo, "filters", None) or {})
        have = _filter_keys_lower(filt)
        for turn in reversed(history):
            if not isinstance(turn, dict):
                continue
            prev_qo = turn.get("qo") or {}
            if str(prev_qo.get("metric_id") or "").strip() != cur_metric_id:
                continue
            prev_f = prev_qo.get("filters") or {}
            if not isinstance(prev_f, dict):
                continue
            changed = False
            for k, v in prev_f.items():
                lk = str(k).lower()
                if lk in have or v is None:
                    continue
                filt[k] = v
                have.add(lk)
                changed = True
            if changed:
                qo.filters = filt
            return
        return

    filt = dict(getattr(qo, "filters", None) or {})
    have = _filter_keys_lower(filt)

    for turn in reversed(history):
        if not isinstance(turn, dict):
            continue
        prev_qo = turn.get("qo") or {}
        if str(prev_qo.get("event") or "").strip() != cur_event:
            continue

        to_merge: dict[str, str] = {}

        ce = lookup_custom_event(catalog, turn.get("custom_event_name"))
        if ce:
            to_merge.update(rollforward_slice_from_custom_event(ce, cur_event))

        prev_f = prev_qo.get("filters") or {}
        if isinstance(prev_f, dict) and cfg["rollforward_keys"]:
            for key in cfg["rollforward_keys"]:
                lk = key.lower()
                if lk in have or lk in {str(x).lower() for x in to_merge}:
                    continue
                for pk, pv in prev_f.items():
                    if str(pk).lower() != lk:
                        continue
                    if pv is None:
                        continue
                    to_merge[str(pk)] = str(pv)
                    break

        if not to_merge:
            return

        changed = False
        for k, v in to_merge.items():
            lk = str(k).lower()
            if lk in have:
                continue
            filt[k] = v
            have.add(lk)
            changed = True
        if changed:
            qo.filters = filt
        return


def materialize_rollups_on_qo(qo: Any, catalog: Optional[dict]) -> None:
    """
    After a custom-event execution, copy catalog-defined equality rollups
    into ``qo.filters`` so history persistence matches what SQL enforced.
    """
    if not qo or not catalog:
        return
    cname = getattr(qo, "_executed_custom_event_name", None)
    if not cname:
        return
    ce = lookup_custom_event(catalog, cname)
    if not ce:
        return
    anchor = str(getattr(qo, "event", None) or "").strip()
    if not anchor:
        return
    slice_ = rollforward_slice_from_custom_event(ce, anchor)
    if not slice_:
        return
    filt = dict(getattr(qo, "filters", None) or {})
    have = _filter_keys_lower(filt)
    for k, v in slice_.items():
        lk = str(k).lower()
        if lk in have:
            continue
        filt[k] = v
        have.add(lk)
    qo.filters = filt
