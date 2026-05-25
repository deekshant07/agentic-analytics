"""
playbooks.py — Domain playbook registry and matcher.

Playbooks are YAML-defined investigation templates that guide the deep analysis
LLM toward proven, structured investigation plans for well-understood problem types.

Design principles:
  - Industry-agnostic: playbooks describe problem angles and narrative requirements,
    NOT specific event names. The generate_deep_plan() LLM maps angles → real events.
  - Pure-Python matcher: no LLM call, no regex — just keyword scoring.
    Deterministic, fast, zero cost.
  - Graceful degradation: if no playbook matches (score below threshold), the LLM
    generates a plan from scratch. Playbooks are guidance, not hard constraints.
  - Additive only: playbooks inject context into generate_deep_plan() prompts.
    They never replace or override the orchestrator's query object.

File layout:
  playbooks/
    funnel/       — conversion and onboarding funnel problems
    quality/      — technical failures, errors, verification friction
    retention/    — churn, engagement, re-engagement
    growth/       — acquisition, activation, cohort analysis
    product/      — feature adoption, power users, A/B testing
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PlaybookInvestigation:
    sub_question: str
    analysis_hint: str       # metric|segment|funnel|retention|behavioral_cohort|time_between|journey
    rationale: str
    priority: int
    breakdown_hint: Optional[str] = None   # suggested breakdown dimension (hint to LLM)
    event_hint: Optional[str] = None       # suggested event concept (hint to LLM, not exact name)


@dataclass
class Playbook:
    id: str
    name: str
    description: str
    match_signals: dict                                      # {keywords, anti_keywords, intent_phrases}
    investigations: list[PlaybookInvestigation] = field(default_factory=list)
    narrative_requirements: list[str] = field(default_factory=list)
    cookbook_id: str = "default"
    recipe_id: str = ""
    tool_controls: dict = field(default_factory=dict)
    validation_rules: dict = field(default_factory=dict)
    ingredients: list[dict] = field(default_factory=list)
    mandatory_filters: list[str] = field(default_factory=list)
    # ── Pre-orchestration fields (new) ───────────────────────────────────────
    intent_class: str = ""          # maps directly to analysis_type (e.g. "retention")
    intent_confidence: str = "high" # "high" → strong suggestion; "low" → soft hint
    orchestrator_hint: dict = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        """
        Format this playbook as a structured guidance block for the deep-plan LLM.
        The LLM sees this as proven investigation angles to follow (not a hard constraint).
        """
        lines = [
            f"PLAYBOOK MATCH: {self.name}",
            f"Problem type: {self.description.strip().split(chr(10))[0]}",
            "",
            "Proven investigation angles for this problem type:",
        ]
        for inv in sorted(self.investigations, key=lambda i: i.priority):
            lines.append(f"  [{inv.priority}] {inv.sub_question}")
            lines.append(f"      Analysis type: {inv.analysis_hint}")
            if inv.breakdown_hint:
                lines.append(f"      Suggested breakdown dimension: {inv.breakdown_hint}")
            lines.append(f"      Why: {inv.rationale}")
        if self.tool_controls:
            allowed = self.tool_controls.get("allowed_analysis_types") or []
            if allowed:
                lines.append("")
                lines.append("Recipe tool controls:")
                lines.append(f"  • Allowed analysis types: {', '.join(str(x) for x in allowed)}")
        if self.validation_rules:
            lines.append("")
            lines.append("Recipe validation rules:")
            for k, v in self.validation_rules.items():
                lines.append(f"  • {k}: {v}")
        if self.ingredients:
            lines.append("")
            lines.append("Recipe ingredients:")
            for ing in self.ingredients[:8]:
                if isinstance(ing, dict):
                    nm = str(ing.get("name", ing.get("id", "ingredient")))
                    it = str(ing.get("type", "context"))
                    lines.append(f"  • {nm} ({it})")
        if self.narrative_requirements:
            lines.append("")
            lines.append("The final narrative MUST address:")
            for req in self.narrative_requirements:
                lines.append(f"  • {req}")
        lines.append("")
        lines.append(
            "INSTRUCTION: Use these investigation angles as a starting template. "
            "Adapt sub-questions to the actual events and dimensions in the catalog. "
            "Keep the same analytical structure but use real event names from the catalog."
        )
        return "\n".join(lines)

    def to_orchestrator_hint_block(self) -> str:
        """
        Compact hint block injected into the orchestrator system prompt BEFORE
        the LLM fills QueryObject slots.  Provides strong but non-binding guidance
        on analysis_type and slot defaults for well-understood question patterns.
        """
        if not self.intent_class and not self.orchestrator_hint:
            return ""

        strength = "Strongly prefer" if self.intent_confidence == "high" else "Consider"
        lines = [
            f"INTENT SIGNAL — matched pattern: {self.name}",
            f"This question matches a known analytics pattern.",
        ]
        if self.intent_class:
            lines.append(f'  {strength} analysis_type="{self.intent_class}"')

        hint = self.orchestrator_hint or {}
        if hint.get("event_role"):
            lines.append(f'  Primary event role: {hint["event_role"]} (the core product action)')
        if hint.get("suggested_time_range_days"):
            lines.append(f'  Suggested time_range_days: {hint["suggested_time_range_days"]} (override if user specified)')
        if hint.get("suggested_time_granularity"):
            lines.append(f'  Suggested time_granularity: "{hint["suggested_time_granularity"]}" (override if user specified)')
        if hint.get("suggested_breakdown"):
            lines.append(f'  Suggested breakdown: "{hint["suggested_breakdown"]}" (only if user did not specify)')

        if self.mandatory_filters:
            lines.append(f'  Mandatory filters: {", ".join(self.mandatory_filters)}')

        lines.append("(These are hints — if the question context clearly overrides them, follow the question.)")
        return "\n".join(lines)


# ── YAML loader ───────────────────────────────────────────────────────────────

def _load_playbook(path: Path) -> Optional[Playbook]:
    """Load and validate a single YAML playbook file."""
    if not _YAML_AVAILABLE:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(data, dict) or not data.get("id"):
        return None

    invs = []
    for raw in data.get("investigations", []):
        if not isinstance(raw, dict) or not raw.get("sub_question"):
            continue
        invs.append(PlaybookInvestigation(
            sub_question=str(raw["sub_question"]).strip(),
            analysis_hint=str(raw.get("analysis_hint", "metric")).strip(),
            rationale=str(raw.get("rationale", "")).strip(),
            priority=int(raw.get("priority", 99)),
            breakdown_hint=raw.get("breakdown_hint"),
            event_hint=raw.get("event_hint"),
        ))

    signals = data.get("match_signals", {}) or {}
    # Merge top-level intent_phrases into match_signals for backward compat
    top_phrases = [str(p) for p in (data.get("intent_phrases") or [])]
    if top_phrases and "intent_phrases" not in signals:
        signals = dict(signals)
        signals["intent_phrases"] = top_phrases

    return Playbook(
        id=str(data["id"]),
        name=str(data.get("name", data["id"])),
        description=str(data.get("description", "")),
        match_signals=signals,
        investigations=sorted(invs, key=lambda i: i.priority),
        narrative_requirements=[str(r) for r in data.get("narrative_requirements", [])],
        cookbook_id=str(data.get("cookbook_id", "default")),
        recipe_id=str(data.get("recipe_id", data.get("id", ""))),
        tool_controls=(data.get("tool_controls") or {}),
        validation_rules=(data.get("validation_rules") or {}),
        ingredients=[x for x in (data.get("ingredients") or []) if isinstance(x, dict)],
        mandatory_filters=[str(x) for x in (data.get("mandatory_filters") or [])],
        intent_class=str(data.get("intent_class", "")),
        intent_confidence=str(data.get("intent_confidence", "high")),
        orchestrator_hint=(data.get("orchestrator_hint") or {}),
    )


# ── Scorer ────────────────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]")

def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return cleaned.split()


def _score_playbook(playbook: Playbook, question_tokens: list[str], question_lower: str) -> float:
    """
    Score a playbook against a user question.

    Scoring:
      +4.0  per intent_phrase match (structural question patterns — highest signal)
      +2.0  per keyword that appears as a substring in the lowered question
      -1.5  per anti_keyword that appears as a substring (reduces false positives)
      Minimum score: 0.0

    intent_phrases beat keywords so structural patterns ("why are users not returning")
    always win over incidental keyword hits ("retention is fine, show me growth").
    """
    signals = playbook.match_signals
    intent_phrases = [str(p).lower() for p in signals.get("intent_phrases", [])]
    keywords       = [str(k).lower() for k in signals.get("keywords", [])]
    anti_keywords  = [str(k).lower() for k in signals.get("anti_keywords", [])]

    score = 0.0

    for phrase in intent_phrases:
        if phrase in question_lower:
            score += 4.0

    for kw in keywords:
        if kw in question_lower:
            score += 2.0

    for akw in anti_keywords:
        if akw in question_lower:
            score -= 1.5

    return max(0.0, score)


# ── Registry ──────────────────────────────────────────────────────────────────

class PlaybookRegistry:
    """
    Loads all YAML playbooks from a directory tree and provides
    a deterministic (no LLM) find() method.
    """

    # Minimum score to activate a playbook.
    # 2.0 = one keyword hit (e.g. "onboarding", "churn", "a/b test").
    # Only used when qo.depth=="deep", so the LLM already decided this is a deep
    # multi-angle question — any playbook match provides useful guidance.
    _THRESHOLD = 2.0

    def __init__(self, playbooks_dir: Path):
        self._playbooks: list[Playbook] = []
        if _YAML_AVAILABLE and playbooks_dir.exists():
            for yaml_path in sorted(playbooks_dir.rglob("*.yaml")):
                pb = _load_playbook(yaml_path)
                if pb:
                    self._playbooks.append(pb)

    def __len__(self) -> int:
        return len(self._playbooks)

    def all(self) -> list[Playbook]:
        return list(self._playbooks)

    def find(self, question: str) -> Optional[Playbook]:
        """
        Return the best-matching playbook for a question, or None if no
        playbook scores above the threshold.

        Deterministic — no LLM call. Safe to call on every deep-analysis request.
        """
        if not self._playbooks:
            return None

        q_lower = question.lower()
        q_tokens = _tokenize(question)

        best_pb    = None
        best_score = 0.0

        for pb in self._playbooks:
            score = _score_playbook(pb, q_tokens, q_lower)
            if score > best_score:
                best_score = score
                best_pb    = pb

        if best_score >= self._THRESHOLD:
            return best_pb
        return None

    def confirm(self, question: str, analysis_type: str) -> Optional[Playbook]:
        """
        Post-orchestration playbook selection.

        Called after the orchestrator has resolved analysis_type.  Uses that type
        as the primary routing signal; keyword scoring is a tiebreaker within the
        type-matched set, not a gatekeeper.

        Lookup order:
          1. Primary — intent_class == analysis_type (exact type match).
             Returns the highest-scoring candidate with no threshold: the type
             already confirms relevance, so keyword score only breaks ties when
             multiple playbooks share the same intent_class.
          2. Secondary — analysis_type appears in allowed_analysis_types but
             intent_class differs (e.g. segment query matching a metric playbook).
             Threshold applies here because structural fit is weaker.

        Returns None if no candidate exists for the type (LLM plans from scratch).
        """
        if not self._playbooks or not analysis_type:
            return None

        q_lower  = question.lower()
        q_tokens = _tokenize(question)
        at       = analysis_type.strip().lower()

        primary   = [p for p in self._playbooks if p.intent_class.strip().lower() == at]
        secondary = [
            p for p in self._playbooks
            if p not in primary
            and at in {str(x).strip().lower()
                       for x in (p.tool_controls.get("allowed_analysis_types") or [])}
        ]

        if primary:
            scored = [(p, _score_playbook(p, q_tokens, q_lower)) for p in primary]
            return max(scored, key=lambda x: x[1])[0]

        if secondary:
            scored = [(p, _score_playbook(p, q_tokens, q_lower)) for p in secondary]
            best_pb, best_score = max(scored, key=lambda x: x[1])
            return best_pb if best_score >= self._THRESHOLD else None

        return None

    def find_all(self, question: str, top_n: int = 3) -> list[tuple[Playbook, float]]:
        """
        Return the top N playbooks with their scores (for debugging / inspection).
        All playbooks returned, not just those above threshold.
        """
        q_lower  = question.lower()
        q_tokens = _tokenize(question)
        scored = [
            (pb, _score_playbook(pb, q_tokens, q_lower))
            for pb in self._playbooks
        ]
        return sorted(scored, key=lambda x: -x[1])[:top_n]
