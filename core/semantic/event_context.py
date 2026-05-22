"""
event_context.py — Derives quality signals for any event from catalog + schema.

When a completion metric drops, a smart analyst asks three questions before
reaching for dimensional slicing:

  1. DEMAND:   Did *total attempts* also fall?  (fewer users tried)
  2. QUALITY:  Did *success rate* fall?          (more attempts failed)
  3. MODE:     Which *failure mode* grew most?   (what specifically broke)

This module detects which columns carry these signals by naming convention
against the sampled schema — so analyst.py and diagnose.py can build
hypothesis-driven investigation plans without any hardcoded event logic.

Works for any event that has a status column (transaction_status, pull_status,
binding_status …) — not just transactions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── QualityContext ────────────────────────────────────────────────────────────

@dataclass
class QualityContext:
    """Quality-signal columns for a given event, derived from schema."""
    event:   str
    stage:   str   # started | intermediate | completed | failed | observed
    journey: str   # transaction | onboarding | verification | engagement …

    # Primary status column — splits events into success vs failure
    status_col:  Optional[str] = None   # e.g. "transaction_status"
    success_val: Optional[str] = None   # e.g. "SUCCESS"
    fail_val:    Optional[str] = None   # e.g. "FAILED"

    # Failure mode dimensions — explain WHY something failed
    failure_dims: list[str] = field(default_factory=list)
    # e.g. ["failure_reason", "error_code", "rejection_reason"]

    # Journey-relevant quality dimensions — better than generic platform/city
    journey_dims: list[str] = field(default_factory=list)
    # e.g. ["payment_instrument", "transaction_channel"] for transaction journey

    # What unit to count attempts/successes by:
    #   "event" — transaction/payment events: each row = one attempt (COUNT(*))
    #             a user can have many transactions, so user_id would give wrong rate
    #   "user"  — user-journey events (onboarding, verification): one attempt per user
    count_unit: str = "user"

    @property
    def has_quality_signal(self) -> bool:
        """True when we can split this event into success vs failure attempts."""
        return self.status_col is not None and self.success_val is not None


# ── Journey → contextually meaningful breakdown dimensions ───────────────────
# Common column name patterns that are more informative than generic platform/city
# when we know the journey type. These are checked against actual sampled schema
# values so unrecognised columns are silently ignored — safe across any industry.
_JOURNEY_DIMS: dict[str, list[str]] = {
    "transaction":    ["payment_instrument", "payment_method", "transaction_channel",
                       "transaction_type", "amount_band", "channel"],
    "verification":   ["verification_method", "otp_channel", "auth_channel"],
    "onboarding":     ["install_source", "acquisition_source", "acquisition_cohort",
                       "referral_source"],
    "authentication": ["auth_method", "otp_channel"],
    "engagement":     ["notification_type", "open_source", "content_type"],
    "general":        [],
}

# Recognized success / failure value tokens (upper-cased for matching)
_SUCCESS_VALS: frozenset[str] = frozenset({
    "SUCCESS", "COMPLETED", "PASSED", "OK", "APPROVED", "VERIFIED", "RECONCILED",
})
_FAIL_VALS: frozenset[str] = frozenset({
    "FAILED", "FAILURE", "ERROR", "FAIL", "REJECTED", "DECLINED",
    "EXPIRED", "TIMEOUT", "PENDING_TIMEOUT",
})


# ── Main detection function ───────────────────────────────────────────────────

def detect_quality_context(
    event: str,
    catalog: dict,
    event_sampled_values: dict[str, list],
) -> QualityContext:
    """
    Derive quality signals for the given event from catalog metadata + schema.

    Parameters
    ----------
    event                : event_name string (e.g. "purchase_completed")
    catalog              : the full catalog dict (catalog.json loaded)
    event_sampled_values : sampled_values for the events table only
                           e.g. sampled_values.get("events", {})
                           Format: {col_name: [val1, val2, ...]}

    Returns
    -------
    QualityContext — may have status_col=None if no quality signal is detected.
    """
    # ── Pull event semantics from catalog ─────────────────────────────────────
    event_meta = (
        catalog.get("events", {})
               .get("event_semantics", {})
               .get(event, {})
    )
    stage   = event_meta.get("stage",   "")
    journey = event_meta.get("journey", "")
    obj     = event_meta.get("object",  "")  # e.g. "transaction", "kyc", "otp"

    # Extract the primary token from the event name (first underscore-separated word)
    primary_token = event.split("_")[0]
    obj_token     = obj.split("_")[0] if obj else ""

    # ── Status column detection ───────────────────────────────────────────────
    # Heuristic: find a column named <token>_status whose sampled values include
    # a recognisable success or failure indicator.
    # The token must match the event's primary word, object, or journey type
    # to avoid picking up unrelated status columns.
    status_col  = None
    success_val = None
    fail_val    = None

    for col, vals in event_sampled_values.items():
        if not col.endswith("_status") or not vals:
            continue
        col_prefix = col[: -len("_status")]   # strip "_status"
        if col_prefix not in {primary_token, obj_token, journey}:
            continue
        # Build upper-case → original-case lookup
        upper_map = {str(v).upper(): v for v in vals}
        sv = {k: v for k, v in upper_map.items() if k in _SUCCESS_VALS}
        fv = {k: v for k, v in upper_map.items() if k in _FAIL_VALS}
        if sv or fv:
            status_col  = col
            success_val = next(iter(sv.values())) if sv else None
            fail_val    = next(iter(fv.values())) if fv else None
            break

    # ── Failure mode dimensions ───────────────────────────────────────────────
    # Columns that name a reason, party, or code responsible for a failure.
    # Suffix "_code" alone is too broad (matches merchant_category_code, ifsc_code, etc.)
    # so we require the full column name to be more specific.
    # Columns that describe WHY something failed.
    # Matched by naming convention — works across any industry without hardcoding.
    fail_prefixes = ("failure_", "error_", "rejection_")
    fail_suffixes = ("_reason", "_error_code", "_error", "_failure_code",
                     "_rejection_reason", "_decline_reason")

    failure_dims = sorted([
        col for col in event_sampled_values
        if (
            any(col.startswith(p) for p in fail_prefixes)
            or any(col.endswith(s) for s in fail_suffixes)
        )
        and bool(event_sampled_values[col])  # column has sampled values
    ])

    # ── Journey-relevant quality dimensions ───────────────────────────────────
    journey_dims = [
        dim for dim in _JOURNEY_DIMS.get(journey, [])
        if event_sampled_values.get(dim)   # dimension is populated in this dataset
    ]

    # Transaction/payment events are counted per-event (each row = one attempt).
    # All other journeys (onboarding, verification, engagement) are counted per-user.
    count_unit = "event" if journey == "transaction" else "user"

    return QualityContext(
        event=event,
        stage=stage,
        journey=journey,
        status_col=status_col,
        success_val=success_val,
        fail_val=fail_val,
        failure_dims=failure_dims,
        journey_dims=journey_dims,
        count_unit=count_unit,
    )
