"""
pre_check.py — Zero-LLM gate that runs before any API call.

Three checks:
1. Out-of-scope regex: operational requests (export, ML, etc.) → instant rejection
2. Underspecified detection: question too vague to produce a useful answer
3. Entity validation: event/column names the user mentioned must exist in catalog
"""

import re
from typing import Optional

# Patterns that are definitely out of scope for a descriptive analytics agent
_OUT_OF_SCOPE = [
    (r"\bml\b|\bmachine learning\b", "ML model training is not supported."),
    (r"\bexport\b",         "Data export is not supported here."),
    (r"\bdownload\b",       "Data download is not supported here."),
    (r"\bsend.*(email|slack|teams)\b", "Sending messages or notifications is not supported."),
    (r"\bdelete\b|\btruncate\b|\binsert\b|\bupdate\b", "Write operations are not permitted."),
]

# Questions that are too vague to answer: fewer than ~4 meaningful words and no event/metric signal.
# Matched against stripped, lowercased question.
_UNDERSPECIFIED_EXACT = {
    "show metrics", "show me metrics", "show all metrics",
    "what happened", "what happened last month", "what happened this month",
    "show data", "show me data", "give me data",
    "show dashboard", "show overview",
}


def check_out_of_scope(question: str) -> Optional[str]:
    """Return a user-facing error message if question is out of scope, else None."""
    q = question.lower()
    for pattern, message in _OUT_OF_SCOPE:
        if re.search(pattern, q):
            return message
    return None


def check_underspecified(question: str) -> Optional[str]:
    """
    Return a user-facing message when the question is too vague to answer.
    Only fires on a small set of well-known ambiguous phrases — never blocks
    anything that names a specific metric, event, or time window.
    """
    q = question.strip().lower().rstrip("?. ")
    # Remove filler words to normalise phrasing variants
    q_stripped = re.sub(r"\b(please|can you|could you|i want|i need|i'd like)\b", "", q).strip()
    if q_stripped in _UNDERSPECIFIED_EXACT:
        return (
            "That question is too broad for me to answer directly. "
            "Try asking about a specific metric or event, for example: "
            "“Show DAU for last 30 days” or “What is the activation rate MOM?”"
        )
    return None


def check_entities(question: str, valid_events: set[str], valid_columns: set[str]) -> list[str]:
    """
    Warn (don't block) if the user mentions a token that looks like an event
    or column name but isn't in the catalog. Returns list of warning strings.
    """
    warnings = []
    q_tokens = set(re.findall(r"[a-z_]{4,}", question.lower()))

    # Check for event-like tokens (contain underscore, not in catalog)
    for tok in q_tokens:
        if "_" in tok and tok not in valid_events and tok not in valid_columns:
            # Only warn if it looks like a technical name (all lowercase + underscores)
            if re.match(r"^[a-z][a-z_]+[a-z]$", tok):
                warnings.append(f"'{tok}' not found in catalog — closest match may differ")

    return warnings  # currently unused in the main flow but available for future UI hints
