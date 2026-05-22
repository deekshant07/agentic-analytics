"""
semantic_index.py — BM25-based semantic retrieval over the catalog.

Addresses the vocabulary mismatch problem: users say "engagement" but the
event is called `session_start`, or they ask about "payments" and the catalog
has `transaction_initiated`. Classic keyword match fails silently; BM25 over
event/metric descriptions finds the right entities.

Usage:
    from core.semantic.semantic_index import CatalogSemanticIndex
    index = CatalogSemanticIndex(catalog)
    result = index.retrieve("why did daily active users drop?")
    # result.top_events   → ranked list of event dicts with descriptions
    # result.top_metrics  → ranked list of metric dicts
    # result.all_event_names → complete list for hard-constraint enforcement
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import log
from typing import Optional

# BM25 weight in the blended score — exact-match signal gets the remainder.
# Tuned so a strong exact hit (e.g. user types "DAU" matching "daily_active_users")
# always wins over a weak BM25 match on a longer description.
_BM25_WEIGHT = 0.65
_EXACT_WEIGHT = 0.35


# ── Tokenizer ─────────────────────────────────────────────────────────────────

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "in", "of", "to", "for", "is", "are",
    "was", "were", "be", "been", "by", "with", "at", "from", "as", "on",
    "how", "what", "why", "did", "do", "does", "has", "have", "had",
    "show", "me", "get", "give", "tell", "this", "that", "it", "its",
    "user", "users", "event", "events", "metric", "metrics",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on word boundaries, remove stopwords and short tokens."""
    tokens = re.findall(r'\b[a-z][a-z0-9]*\b', text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]


# ── Exact-match / metadata signal ────────────────────────────────────────────

# Acronym expansions: query terms that map to known event/metric name fragments.
_ACRONYMS: dict[str, list[str]] = {
    "dau":  ["daily_active_users", "daily active"],
    "wau":  ["weekly_active_users", "weekly active"],
    "mau":  ["monthly_active_users", "monthly active"],
    "d1":   ["day1", "d1_retention", "retention"],
    "d7":   ["d7_retention", "retention"],
    "d30":  ["d30_retention", "retention"],
    "ltv":  ["lifetime_value", "lifetime value"],
    "arpu": ["average_revenue", "revenue per user"],
    "cac":  ["customer_acquisition", "acquisition cost"],
    "nps":  ["net_promoter", "promoter score"],
    "cvr":  ["conversion", "conversion rate"],
}


def _exact_signal(query: str, doc: "_Doc") -> float:
    """
    0–1 signal that captures what BM25 misses:
      - Acronym expansion  (query "DAU" → doc "daily_active_users")
      - Exact token-in-name substring match
      - Tag overlap with query tokens
      - Display name prefix match

    Returns a normalised score in [0, 1].
    """
    q_lower = query.lower()
    q_tokens = _tokenize(query)
    name_lower = doc.name.lower()
    display_lower = doc.display_name.lower()
    score = 0.0

    # 1. Acronym expansion: if any query word expands to the doc name
    for tok in q_tokens:
        expansions = _ACRONYMS.get(tok, [])
        for exp in expansions:
            if exp in name_lower or exp in display_lower:
                score += 1.0
                break

    # 2. Query token appears literally in the doc name (handles partial matches)
    name_parts = set(re.split(r"[_\s\-]", name_lower))
    for tok in q_tokens:
        if tok in name_parts:
            score += 0.6

    # 3. Tag exact match (each matching tag adds a boost)
    tag_set = {t.lower() for t in doc.tags}
    for tok in q_tokens:
        if tok in tag_set:
            score += 0.4

    # 4. Display name starts with a query token (prefix signal)
    for tok in q_tokens:
        if display_lower.startswith(tok):
            score += 0.3

    # Normalize: cap at 1.0, scale by number of query tokens so longer queries
    # don't artificially inflate the score.
    if q_tokens:
        score = min(1.0, score / max(1, len(q_tokens)))
    return score


# ── BM25 corpus document ──────────────────────────────────────────────────────

@dataclass
class _Doc:
    doc_type: str    # "event" | "metric" | "glossary"
    id: str
    name: str
    display_name: str
    description: str
    journey: str
    stage: str
    tags: list[str]
    tokens: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "type":         self.doc_type,
            "name":         self.name,
            "display_name": self.display_name,
            "description":  self.description,
            "journey":      self.journey,
            "stage":        self.stage,
            "tags":         self.tags,
        }


# ── BM25 scorer ───────────────────────────────────────────────────────────────

class _BM25:
    """
    Standard BM25 (Robertson et al.) over a fixed corpus of _Doc objects.
    k1=1.5, b=0.75 are standard defaults that work well on short descriptions.
    """
    def __init__(self, docs: list[_Doc], k1: float = 1.5, b: float = 0.75):
        self._docs = docs
        self._k1 = k1
        self._b = b
        self._N = len(docs)
        self._avg_dl = (
            sum(len(d.tokens) for d in docs) / max(1, len(docs))
        )
        # document-frequency per token
        self._df: dict[str, int] = {}
        for doc in docs:
            for tok in set(doc.tokens):
                self._df[tok] = self._df.get(tok, 0) + 1
        # IDF per token (Robertson-Sparck Jones variant, always positive)
        self._idf: dict[str, float] = {
            tok: log((self._N - df + 0.5) / (df + 0.5) + 1.0)
            for tok, df in self._df.items()
        }

    def score(self, query: str, doc: _Doc) -> float:
        q_tokens = _tokenize(query)
        if not q_tokens:
            return 0.0
        dl = len(doc.tokens)
        tf: dict[str, int] = {}
        for t in doc.tokens:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for t in q_tokens:
            idf = self._idf.get(t, 0.0)
            f = tf.get(t, 0)
            denom = f + self._k1 * (1 - self._b + self._b * dl / self._avg_dl)
            score += idf * (f * (self._k1 + 1)) / (denom + 1e-9)
        return score

    def rank(self, query: str, doc_subset: list[_Doc]) -> list[tuple[float, _Doc]]:
        """Blend BM25 with the exact-match signal and return sorted (score, doc) pairs."""
        results = []
        for d in doc_subset:
            bm25  = self.score(query, d)
            exact = _exact_signal(query, d)
            # Normalise BM25 to [0,1] using a soft-max of 10 (covers typical scores)
            bm25_norm = min(1.0, bm25 / 10.0)
            blended = _BM25_WEIGHT * bm25_norm + _EXACT_WEIGHT * exact
            results.append((blended, d))
        results.sort(key=lambda x: -x[0])
        return results


# ── Retrieval result ──────────────────────────────────────────────────────────

@dataclass
class SemanticRetrievalResult:
    top_events:      list[dict]       # ranked, with descriptions — inject into prompt
    top_metrics:     list[dict]       # ranked, with descriptions
    all_event_names: list[str]        # complete list for hard-constraint enforcement


# ── Main index ────────────────────────────────────────────────────────────────

class CatalogSemanticIndex:
    """
    BM25 index over catalog events and metrics.

    Build once per session (or per catalog object) and reuse across questions.
    Building is O(n_events × avg_description_length) — negligible for typical
    catalogs (20–100 events).
    """

    def __init__(self, catalog: dict):
        self._event_docs:  list[_Doc] = []
        self._metric_docs: list[_Doc] = []
        self._build(catalog)
        all_docs = self._event_docs + self._metric_docs
        self._bm25 = _BM25(all_docs) if all_docs else None

    # ── Catalog traversal ─────────────────────────────────────────────────────

    def _build(self, catalog: dict) -> None:
        for tname, tdata in catalog.items():
            if tname.startswith("__") or not isinstance(tdata, dict):
                continue

            # Rich event descriptions from catalog["events"] array (display_name, description, tags)
            events_array: dict[str, dict] = {
                e["raw_name"]: e
                for e in tdata.get("events", [])
                if isinstance(e, dict) and e.get("raw_name")
            }

            # Event semantics: journey + stage + optional description
            for ename, emeta in tdata.get("event_semantics", {}).items():
                if not isinstance(emeta, dict):
                    continue
                ev_doc = events_array.get(ename, {})
                display = ev_doc.get("display_name", "")
                description = (
                    ev_doc.get("description", "")
                    or emeta.get("description", "")
                )
                journey = emeta.get("journey", "")
                stage   = emeta.get("stage", "")
                tags    = ev_doc.get("analysis_tags", [])

                # Build rich text for BM25: name + display + description + journey + tags
                raw_text = " ".join(filter(None, [
                    ename.replace("_", " "),
                    display,
                    description,
                    journey,
                    stage,
                    " ".join(tags),
                ]))

                doc = _Doc(
                    doc_type="event",
                    id=ename,
                    name=ename,
                    display_name=display or ename.replace("_", " ").title(),
                    description=description,
                    journey=journey,
                    stage=stage,
                    tags=tags,
                    tokens=_tokenize(raw_text),
                )
                self._event_docs.append(doc)

            # Metrics
            for m in tdata.get("suggested_metrics", []):
                if not isinstance(m, dict):
                    continue
                mid  = m.get("id", "") or m.get("name", "")
                name = m.get("name", "")
                desc = m.get("description", "")
                aarrr = m.get("aarrr", "") or m.get("category", "")

                raw_text = " ".join(filter(None, [
                    mid.replace("_", " "),
                    name,
                    desc,
                    aarrr,
                ]))

                doc = _Doc(
                    doc_type="metric",
                    id=mid,
                    name=name,
                    display_name=name,
                    description=desc,
                    journey=aarrr,
                    stage="",
                    tags=[aarrr] if aarrr else [],
                    tokens=_tokenize(raw_text),
                )
                self._metric_docs.append(doc)

    # ── Retrieve ──────────────────────────────────────────────────────────────

    def retrieve(
        self,
        question: str,
        top_k_events: int = 18,
        top_k_metrics: int = 8,
    ) -> SemanticRetrievalResult:
        """
        Score all catalog events and metrics against the question.
        Returns:
          - top_events:  up to top_k_events most relevant events (with descriptions)
          - top_metrics: up to top_k_metrics most relevant metrics
          - all_event_names: complete sorted list of all event names (for prompt constraint)

        When the corpus is empty (freshly scanned catalog with no semantics),
        returns all events/metrics unranked so the orchestrator still works.
        """
        all_event_names = sorted(d.id for d in self._event_docs)

        if self._bm25 is None or not self._event_docs:
            return SemanticRetrievalResult(
                top_events=[d.to_dict() for d in self._event_docs],
                top_metrics=[d.to_dict() for d in self._metric_docs],
                all_event_names=all_event_names,
            )

        ranked_events  = self._bm25.rank(question, self._event_docs)
        ranked_metrics = self._bm25.rank(question, self._metric_docs)

        # Always include all events but rank them; caller picks top_k for descriptions
        # and receives complete list for the constraint section
        top_events  = [d.to_dict() for _, d in ranked_events[:top_k_events]]
        top_metrics = [d.to_dict() for _, d in ranked_metrics[:top_k_metrics]]

        return SemanticRetrievalResult(
            top_events=top_events,
            top_metrics=top_metrics,
            all_event_names=all_event_names,
        )

    def __len__(self) -> int:
        return len(self._event_docs) + len(self._metric_docs)


# ── Module-level index cache (keyed by catalog object id) ────────────────────
# Avoids re-building BM25 IDF vectors on every orchestrator call within a session.

_INDEX_CACHE: dict[int, CatalogSemanticIndex] = {}


def get_or_build_index(catalog: dict) -> CatalogSemanticIndex:
    """Return a cached index for this catalog object, building it on first use."""
    key = id(catalog)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = CatalogSemanticIndex(catalog)
    return _INDEX_CACHE[key]


def invalidate_index(catalog: dict) -> None:
    """Call after catalog is reloaded to force a rebuild on the next question."""
    _INDEX_CACHE.pop(id(catalog), None)
