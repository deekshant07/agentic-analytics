"""
doc_ingestor.py — Upload business documents and extract structured BusinessEvent
entries for the semantic layer.

Three problems solved vs. a naive single-pass approach:

  1. Type-specific schemas — document type is detected first (one fast LLM call),
     then a tailored prompt+schema is used for extraction. A PRD extracts rollout %,
     owner, risk flags, success criteria. A campaign extracts channels, budget,
     target audience. An incident extracts root cause, affected tables, recoverability.

  2. Per-metric impacts — each affected metric gets its own direction + magnitude +
     note rather than a single document-level expected_direction that loses nuance.
     "onboarding_completed up 15-20%, fraud_rate unknown (monitoring)" is far more
     useful to hypothesis_agent than "direction: up".

  3. Chunked extraction for long documents — docs > _CHUNK_SIZE chars are split into
     overlapping chunks, each extracted independently, then merged via one synthesis
     LLM call that deduplicates and reconciles conflicts.

  4. Document update / version tracking — find_similar_event() detects if an upload
     is a revision of an existing entry (fuzzy title match). merge_update_event()
     merges old and new via one LLM call and records a changelog of specific changes.
     Hypothesis_agent can then surface "this feature was modified mid-rollout" as a
     signal when investigating metric inflections.

LLM call budget per document:
  short doc, first upload:  2 calls (detect type + extract)
  long doc, first upload:   N+2 calls (detect + N extractions + 1 chunk-merge)
  update to existing doc:   +1 call (merge old+new + produce changelog)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.infra.llm import LLM_FAST, LLM_STRONG, call_llm, make_llm_client


# ── Chunking constants ────────────────────────────────────────────────────────

_CHUNK_SIZE    = 4000   # chars per chunk
_CHUNK_OVERLAP = 200    # overlap so sentences at boundaries aren't lost
_MAX_CHUNKS    = 5      # cap LLM calls for very long docs


# ── Data structure ────────────────────────────────────────────────────────────

@dataclass
class BusinessEvent:
    id: str
    type: str                      # feature_launch|campaign|strategy|incident|data_dictionary|general
    title: str
    summary: str
    date_from: Optional[str]       # YYYY-MM-DD
    date_to: Optional[str]         # YYYY-MM-DD
    metric_impacts: list[dict]     # [{metric, direction, magnitude, note}]
    affected_metrics: list[str]    # derived from metric_impacts for backward compat
    affected_segments: list[str]
    expected_direction: str        # dominant direction across metric_impacts
    key_facts: list[str]
    type_metadata: dict            # type-specific extra fields
    source_doc_name: str
    ingested_at: str
    version: int = 1               # incremented on each update
    changelog: list[str] = field(default_factory=list)  # specific changes per version


# ── Type-specific extraction schemas ─────────────────────────────────────────

# Each entry is appended to the base extraction prompt to request type-specific fields.
_TYPE_SCHEMAS: dict[str, str] = {
    "feature_launch": """\
  "type_metadata": {
    "rollout_pct": <0-100 integer, or null if unknown>,
    "is_experiment": <true if A/B test/experiment, false if full launch, null if unclear>,
    "owner": "<team or person name, or null>",
    "success_criteria": ["<measurable success criterion>"],
    "risk_flags": ["<risk or concern explicitly mentioned>"],
    "rollback_plan": "<rollback plan description, 'none planned', or null>"
  }""",

    "campaign": """\
  "type_metadata": {
    "channels": ["<marketing channel e.g. facebook_ads, push_notification>"],
    "budget": "<budget amount with currency, or null>",
    "target_audience": "<description of who is targeted>",
    "expected_lift": "<expected uplift description or null>",
    "campaign_id": "<campaign identifier if mentioned, or null>"
  }""",

    "incident": """\
  "type_metadata": {
    "root_cause": "<root cause of the incident, or null if unknown>",
    "affected_tables": ["<database table name>"],
    "affected_events": ["<event name>"],
    "is_data_recoverable": <true|false|null>,
    "workaround": "<workaround or remediation status, or null>"
  }""",

    "strategy": """\
  "type_metadata": {
    "okr_period": "<quarter or time period e.g. Q2 2026, or null>",
    "key_results": ["<specific measurable key result>"],
    "priority_rank": <integer priority rank, or null>,
    "owner": "<team or person, or null>",
    "dependencies": ["<dependency on another team or system>"],
    "review_date": "<YYYY-MM-DD or null>"
  }""",

    "data_dictionary": """\
  "type_metadata": {
    "table_name": "<primary table this doc describes, or null>",
    "columns_defined": ["<column name>"],
    "events_defined": ["<event name>"]
  }""",

    "general": """\
  "type_metadata": {}""",
}


# ── Prompts ───────────────────────────────────────────────────────────────────

_TYPE_DETECTION_SYSTEM = """\
Classify the following business document into exactly one category.
Return ONLY the category name — no punctuation, no explanation.

Categories:
  feature_launch   — PRDs, release notes, feature announcements, product specs
  campaign         — Marketing campaigns, growth experiments, A/B tests, promotions
  strategy         — OKR docs, quarterly reviews, business goals, roadmaps
  incident         — Outages, tracking bugs, data quality issues, pipeline failures
  data_dictionary  — Schema docs, column definitions, event definitions, data models
  general          — Anything that doesn't fit the above"""


_BASE_EXTRACTION_SYSTEM = """\
You are a business analyst preparing documents for an analytics AI system.
Extract structured facts so the analytics system can reason about what changed,
when, and why metrics may have moved.

{catalog_vocab_block}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "title": "<concise 3-8 word document title>",
  "summary": "<2-3 sentences: what this document describes and why it matters to analytics>",
  "date_from": "<YYYY-MM-DD or null>",
  "date_to": "<YYYY-MM-DD or null>",
  "metric_impacts": [
    {{
      "metric": "<metric name — prefer names from the known list above>",
      "direction": "up|down|neutral|unknown",
      "magnitude": "<e.g. '+15-20%' or '2x' or null>",
      "note": "<brief context e.g. 'primary success metric' or null>"
    }}
  ],
  "affected_segments": ["<user segment, platform, account type, or geography>"],
  "key_facts": ["<fact>", "<fact>", "<fact>"],
{type_schema}
}}

Rules:
- metric_impacts: one entry per affected metric. Capture EVERY metric mentioned,
  even risks and secondary effects. direction = expected effect on that specific metric.
- key_facts: 3-6 bullet points capturing what an analyst needs when investigating
  a metric anomaly — include rollout %, experiment vs. full launch, known risks,
  data quality concerns, cohort restrictions.
- date_from: when this event/launch/campaign STARTS.
  date_to: when it ENDS — null if ongoing or not mentioned.
  For incidents: date_from = issue start, date_to = resolution date (null if unresolved).
- affected_segments: be specific — prefer dimension values from the catalog vocab above.\
"""


_MERGE_SYSTEM = """\
You are merging multiple partial JSON extractions from different chunks of the same
document into one comprehensive, deduplicated result. Each chunk may have partial or
overlapping information.

Merge rules:
- title: use the most descriptive title seen across chunks
- summary: write a fresh 2-3 sentence summary covering the full document scope
- date_from: earliest non-null date across chunks
- date_to: latest non-null date across chunks
- metric_impacts: union all entries; for the same metric, merge notes and keep the
  most specific direction and magnitude
- affected_segments: union all, deduplicate
- key_facts: union all, remove near-duplicates, keep the most informative phrasing
- type_metadata: merge all dicts; for list values take the union; for scalar values
  keep the most specific non-null value

Return ONLY valid JSON matching the same schema as the input extractions.\
"""


_UPDATE_MERGE_SYSTEM = """\
You are merging an ORIGINAL business event entry with NEW information from a revised
version of the same document. The user confirmed these are related documents.

Produce a single merged entry that represents the current state of knowledge, plus
a changelog of what specifically changed between the original and the new version.

Merge rules:
- title: use the most up-to-date title (prefer new version if more specific)
- summary: rewrite to reflect the combined/latest understanding
- date_from: keep original (the event start date hasn't changed)
- date_to: update if the new doc provides a clearer end date
- metric_impacts: union all; for the same metric keep the most recent direction and
  magnitude, combine notes
- affected_segments: union, deduplicate
- key_facts: union both, remove near-duplicates, keep the most specific phrasing
- type_metadata: merge dicts — prefer new values for scalar fields, union lists
- changelog: list SPECIFIC changes in plain English (not generic "summary updated").
  Examples: "Rollout reduced from 100% to 50%", "Added risk flag: fraud rate spike",
  "Success criteria revised from +15% to +8% onboarding completion"

Return ONLY valid JSON:
{
  "merged": { <full BusinessEvent fields — same schema as input> },
  "changelog": ["<specific change>", "<specific change>"]
}\
"""


# ── Catalog vocabulary ────────────────────────────────────────────────────────

def _catalog_vocab(catalog: dict) -> str:
    lines: list[str] = []
    bctx = catalog.get("__business_context__", {}) or {}
    glossary = (bctx.get("exclusions") or {}).get("glossary") or []
    if glossary:
        terms = [g.get("term", "") for g in glossary if g.get("term")]
        lines.append("Known metrics: " + ", ".join(terms))
    events_table = catalog.get("events", {}) or {}
    event_sem = events_table.get("event_semantics") or {}
    if event_sem:
        lines.append("Known events: " + ", ".join(list(event_sem.keys())[:20]))
    custom = bctx.get("custom_events") or []
    if custom:
        names = [c.get("name", "") for c in custom if c.get("name")]
        lines.append("Custom segments: " + ", ".join(names))
    return "\n".join(lines) if lines else ""


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks. Returns [text] unchanged if short enough."""
    if len(text) <= _CHUNK_SIZE:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < _MAX_CHUNKS:
        end = min(start + _CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - _CHUNK_OVERLAP
    return chunks


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    s = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            return json.loads(s[i: j + 1])
        raise


def _detect_type(text_sample: str, client) -> str:
    """One fast LLM call to classify document type."""
    resp = call_llm(
        client,
        call_site="doc_ingestor.detect_type",
        model=LLM_FAST,
        temperature=0,
        messages=[
            {"role": "system", "content": _TYPE_DETECTION_SYSTEM},
            {"role": "user",   "content": text_sample[:2000]},
        ],
    )
    detected = resp.choices[0].message.content.strip().lower()
    valid = {"feature_launch", "campaign", "strategy", "incident", "data_dictionary", "general"}
    return detected if detected in valid else "general"


def _extract_chunk(
    chunk: str,
    doc_type: str,
    vocab_block: str,
    filename: str,
    client,
    chunk_num: int = 1,
    total_chunks: int = 1,
) -> dict:
    """Extract structured data from one chunk using the type-specific prompt."""
    type_schema = _TYPE_SCHEMAS.get(doc_type, _TYPE_SCHEMAS["general"])
    system = _BASE_EXTRACTION_SYSTEM.format(
        catalog_vocab_block=vocab_block,
        type_schema=type_schema,
    )
    context = (
        f"Document filename: {filename}"
        + (f" [Chunk {chunk_num} of {total_chunks}]" if total_chunks > 1 else "")
        + f"\n\n{chunk}"
    )
    resp = call_llm(
        client,
        call_site="doc_ingestor.extract_chunk",
        model=LLM_STRONG,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": context},
        ],
    )
    return _parse_json(resp.choices[0].message.content)


def _merge_chunks(extractions: list[dict], client) -> dict:
    """Synthesise multiple chunk extractions into one coherent result."""
    payload = json.dumps(extractions, ensure_ascii=False)
    resp = call_llm(
        client,
        call_site="doc_ingestor.merge_chunks",
        model=LLM_FAST,
        temperature=0,
        messages=[
            {"role": "system", "content": _MERGE_SYSTEM},
            {"role": "user",   "content": payload},
        ],
    )
    return _parse_json(resp.choices[0].message.content)


# ── Dominant direction helper ─────────────────────────────────────────────────

def _dominant_direction(metric_impacts: list[dict]) -> str:
    """
    Compute a single summary direction from per-metric impacts.
    Used to populate the backward-compat expected_direction field.
    """
    counts: dict[str, int] = {"up": 0, "down": 0, "neutral": 0, "unknown": 0}
    for m in metric_impacts:
        d = m.get("direction", "unknown")
        counts[d] = counts.get(d, 0) + 1
    # Incident: any 'down' dominates. Otherwise: majority wins.
    if counts["down"] > 0:
        return "down"
    if counts["up"] > counts["neutral"] + counts["unknown"]:
        return "up"
    if counts["neutral"] > 0:
        return "neutral"
    return "unknown"


# ── Conflict detection ────────────────────────────────────────────────────────

def find_metric_conflicts(raw_events: list[dict]) -> list[dict]:
    """
    Detect pairs of business events with contradictory metric_impacts in overlapping
    time windows.

    A conflict = two events that (a) affect the same metric, (b) disagree on direction
    (one "up", one "down"), and (c) have overlapping date ranges.

    Accepts the raw event dicts stored in catalog["__business_context__"]["business_events"]
    so callers do not need to construct BusinessEvent objects.

    Returns a list of conflict dicts (empty when no conflicts found):
      {metric, event_a_id, event_a_title, direction_a,
       event_b_id, event_b_title, direction_b, overlap_start, overlap_end}
    """
    from datetime import date as _date

    _FAR_FUTURE = _date(2099, 12, 31)
    _FAR_PAST   = _date(2000,  1,  1)

    def _pd(s: Optional[str], default: _date) -> _date:
        if not s:
            return default
        try:
            return _date.fromisoformat(s)
        except (ValueError, TypeError):
            return default

    def _overlaps(af: _date, at: _date, bf: _date, bt: _date) -> bool:
        return af <= bt and bf <= at

    # Index: metric → [(direction, event_id, event_title, date_from, date_to)]
    index: dict[str, list] = {}
    for evt in raw_events:
        ef = _pd(evt.get("date_from"), _FAR_PAST)
        et = _pd(evt.get("date_to"),   _FAR_FUTURE)
        for impact in (evt.get("metric_impacts") or []):
            metric    = (impact.get("metric") or "").strip()
            direction = (impact.get("direction") or "unknown").lower()
            if not metric or direction in ("unknown", "neutral"):
                continue
            index.setdefault(metric, []).append(
                (direction, evt.get("id", ""), evt.get("title", ""), ef, et)
            )

    conflicts: list[dict] = []
    seen: set = set()

    for metric, entries in index.items():
        for i, (dir_a, id_a, title_a, from_a, to_a) in enumerate(entries):
            for dir_b, id_b, title_b, from_b, to_b in entries[i + 1:]:
                key = frozenset({id_a, id_b, metric})
                if key in seen:
                    continue
                if dir_a != dir_b and _overlaps(from_a, to_a, from_b, to_b):
                    seen.add(key)
                    overlap_start = max(from_a, from_b)
                    overlap_end   = min(to_a,   to_b)
                    conflicts.append({
                        "metric":        metric,
                        "event_a_id":    id_a,
                        "event_a_title": title_a,
                        "direction_a":   dir_a,
                        "event_b_id":    id_b,
                        "event_b_title": title_b,
                        "direction_b":   dir_b,
                        "overlap_start": str(overlap_start) if overlap_start != _FAR_PAST   else None,
                        "overlap_end":   str(overlap_end)   if overlap_end   != _FAR_FUTURE else None,
                    })

    return conflicts


# ── Similarity detection ──────────────────────────────────────────────────────

def find_similar_event(
    new_title: str,
    existing_events: list[BusinessEvent],
    threshold: float = 0.45,
) -> Optional[tuple[BusinessEvent, float]]:
    """
    Find an existing BusinessEvent whose title is similar to new_title.
    Uses difflib ratio on lowercased titles — no LLM, no new deps.

    Returns (event, similarity_score) if a match above threshold is found,
    else None. Threshold of 0.45 catches "Onboarding v2" matching
    "Redesigned Onboarding Flow v2" without false-positiving on unrelated docs.
    """
    import difflib

    best_ratio = 0.0
    best_event: Optional[BusinessEvent] = None

    a = new_title.lower().strip()
    for evt in existing_events:
        b = evt.title.lower().strip()
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_event = evt

    if best_event and best_ratio >= threshold:
        return best_event, best_ratio
    return None


# ── Update / merge existing entry ─────────────────────────────────────────────

def merge_update_event(
    new_event: BusinessEvent,
    existing_event: BusinessEvent,
    catalog_path: str,
    api_key: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[BusinessEvent, list[str]]:
    """
    Merge new_event into existing_event via one LLM call.
    The original id is preserved so agents have a stable reference.

    Set dry_run=True to compute the merge and changelog without writing to
    catalog.json — used by the UI to show a preview before the user confirms.

    Returns (updated_event, changelog) where changelog lists specific changes.
    """
    client = make_llm_client(api_key)

    payload = json.dumps({
        "original": asdict(existing_event),
        "new":      asdict(new_event),
    }, ensure_ascii=False)

    resp = call_llm(
        client,
        call_site="doc_ingestor.merge_update_event",
        model=LLM_STRONG,
        temperature=0,
        messages=[
            {"role": "system", "content": _UPDATE_MERGE_SYSTEM},
            {"role": "user",   "content": payload},
        ],
    )

    result = _parse_json(resp.choices[0].message.content)
    merged_data = result.get("merged", {})
    changelog   = [c for c in (result.get("changelog") or []) if c]

    metric_impacts = [
        m for m in (merged_data.get("metric_impacts") or [])
        if isinstance(m, dict) and m.get("metric")
    ]

    updated = BusinessEvent(
        id=existing_event.id,                        # preserve original id
        type=merged_data.get("type", existing_event.type),
        title=merged_data.get("title", existing_event.title),
        summary=merged_data.get("summary", existing_event.summary),
        date_from=merged_data.get("date_from") or existing_event.date_from,
        date_to=merged_data.get("date_to") or existing_event.date_to,
        metric_impacts=metric_impacts,
        affected_metrics=[m["metric"] for m in metric_impacts],
        affected_segments=[s for s in (merged_data.get("affected_segments") or []) if s],
        expected_direction=_dominant_direction(metric_impacts),
        key_facts=[f for f in (merged_data.get("key_facts") or []) if f],
        type_metadata=merged_data.get("type_metadata") or {},
        source_doc_name=new_event.source_doc_name,
        ingested_at=datetime.now(timezone.utc).isoformat(),
        version=existing_event.version + 1,
        changelog=existing_event.changelog + changelog,  # accumulate all versions
    )

    if not dry_run:
        _replace_in_catalog(updated, catalog_path)
    return updated, changelog


def _replace_in_catalog(event: BusinessEvent, catalog_path: str) -> None:
    """Replace an existing entry (matched by id) in catalog.json."""
    catalog = json.loads(Path(catalog_path).read_text())
    bctx    = catalog.setdefault("__business_context__", {})
    events  = bctx.get("business_events") or []
    bctx["business_events"] = [
        asdict(event) if e.get("id") == event.id else e
        for e in events
    ]
    Path(catalog_path).write_text(json.dumps(catalog, indent=2, ensure_ascii=False))


# ── Main entry point ──────────────────────────────────────────────────────────

def ingest_document(
    text: str,
    filename: str,
    catalog_path: str,
    api_key: Optional[str] = None,
    save: bool = True,
) -> BusinessEvent:
    """
    Extract a structured BusinessEvent from document text.

    Pipeline:
      1. Detect document type (1 fast LLM call)
      2. Split into overlapping chunks if doc is long
      3. Extract from each chunk using type-specific prompt (1 strong call each)
      4. Merge chunks if more than one (1 fast LLM call)
      5. Build BusinessEvent — persist to catalog.json only if save=True

    Set save=False to extract without committing, so the caller can show a
    conflict resolution UI before deciding whether to save or merge.
    """
    catalog = json.loads(Path(catalog_path).read_text())
    vocab = _catalog_vocab(catalog)
    vocab_block = f"\nThe analytics system uses these known names:\n{vocab}\n" if vocab else ""

    client = make_llm_client(api_key)

    # Step 1: detect type
    doc_type = _detect_type(text, client)

    # Step 2: chunk
    chunks = _chunk_text(text)
    total  = len(chunks)

    # Step 3: extract per chunk
    extractions = [
        _extract_chunk(chunk, doc_type, vocab_block, filename, client, i + 1, total)
        for i, chunk in enumerate(chunks)
    ]

    # Step 4: merge if needed
    data = extractions[0] if len(extractions) == 1 else _merge_chunks(extractions, client)

    # Step 5: build BusinessEvent
    metric_impacts = [
        m for m in (data.get("metric_impacts") or [])
        if isinstance(m, dict) and m.get("metric")
    ]
    affected_metrics = [m["metric"] for m in metric_impacts]

    event = BusinessEvent(
        id=uuid.uuid4().hex[:12],
        type=doc_type,
        title=data.get("title", filename),
        summary=data.get("summary", ""),
        date_from=data.get("date_from") or None,
        date_to=data.get("date_to") or None,
        metric_impacts=metric_impacts,
        affected_metrics=affected_metrics,
        affected_segments=[s for s in (data.get("affected_segments") or []) if s],
        expected_direction=_dominant_direction(metric_impacts),
        key_facts=[f for f in (data.get("key_facts") or []) if f],
        type_metadata=data.get("type_metadata") or {},
        source_doc_name=filename,
        ingested_at=datetime.now(timezone.utc).isoformat(),
    )

    if save:
        _append_to_catalog(event, catalog_path, catalog)
    return event


# ── Catalog persistence ───────────────────────────────────────────────────────

def save_business_event(event: BusinessEvent, catalog_path: str) -> None:
    """Persist a BusinessEvent that was extracted with save=False."""
    catalog = json.loads(Path(catalog_path).read_text())
    _append_to_catalog(event, catalog_path, catalog)


def _append_to_catalog(event: BusinessEvent, catalog_path: str, catalog: dict) -> None:
    bctx = catalog.setdefault("__business_context__", {})
    events: list = bctx.setdefault("business_events", [])
    events.append(asdict(event))
    Path(catalog_path).write_text(json.dumps(catalog, indent=2, ensure_ascii=False))


def list_business_events(catalog_path: str) -> list[BusinessEvent]:
    """Return all stored BusinessEvents from catalog.json, newest first."""
    try:
        catalog = json.loads(Path(catalog_path).read_text())
        raw = catalog.get("__business_context__", {}).get("business_events") or []
        events = []
        for e in raw:
            # backward compat: older entries may lack these fields
            e.setdefault("metric_impacts", [])
            e.setdefault("type_metadata", {})
            e.setdefault("version", 1)
            e.setdefault("changelog", [])
            e.setdefault("affected_metrics", [m.get("metric", "") for m in e["metric_impacts"]])
            e.setdefault("expected_direction", _dominant_direction(e["metric_impacts"]))
            events.append(BusinessEvent(**e))
        return list(reversed(events))
    except Exception:
        return []


def delete_business_event(event_id: str, catalog_path: str) -> bool:
    """Remove a BusinessEvent by id. Returns True if found and deleted."""
    try:
        catalog = json.loads(Path(catalog_path).read_text())
        bctx = catalog.get("__business_context__", {})
        before = bctx.get("business_events") or []
        after  = [e for e in before if e.get("id") != event_id]
        if len(after) == len(before):
            return False
        bctx["business_events"] = after
        Path(catalog_path).write_text(json.dumps(catalog, indent=2, ensure_ascii=False))
        return True
    except Exception:
        return False
