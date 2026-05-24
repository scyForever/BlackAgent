"""Dynamic slang lifecycle and prompt evaluation support."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import uuid4

from src.collector.base_collector import get_record_field


@dataclass(frozen=True)
class SlangLifecycleRecord:
    term: str
    normalized_term: str
    stage: str
    evidence_trace_ids: list[str]
    reviewer: str | None = None
    notes: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class DynamicSlangLifecycleManager:
    """Phase II/III add-review-gray-roll lifecycle for slang terms."""

    NEW_CANDIDATE = "NEW_CANDIDATE"
    REVIEWED = "REVIEWED"
    GRAY_ROLLOUT = "GRAY_ROLLOUT"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"

    def __init__(self) -> None:
        self._records: dict[str, SlangLifecycleRecord] = {}
        self.few_shot_examples: list[dict[str, Any]] = []
        self.negative_samples: list[dict[str, Any]] = []
        self.whitelist_candidates: list[dict[str, Any]] = []

    def nominate(self, term: str, normalized_term: str, evidence_trace_ids: Iterable[str]) -> SlangLifecycleRecord:
        record = SlangLifecycleRecord(term=term, normalized_term=normalized_term, stage=self.NEW_CANDIDATE, evidence_trace_ids=list(dict.fromkeys(evidence_trace_ids)))
        self._records[term] = record
        return record

    def review(self, term: str, *, approved: bool, reviewer: str = "system", notes: str | None = None) -> SlangLifecycleRecord:
        current = self._records[term]
        stage = self.REVIEWED if approved else self.REJECTED
        record = SlangLifecycleRecord(current.term, current.normalized_term, stage, current.evidence_trace_ids, reviewer, notes)
        self._records[term] = record
        return record

    def gray_rollout(self, term: str, *, reviewer: str = "system", notes: str | None = None) -> SlangLifecycleRecord:
        current = self._records[term]
        if current.stage != self.REVIEWED:
            raise ValueError("term must be REVIEWED before gray rollout")
        record = SlangLifecycleRecord(current.term, current.normalized_term, self.GRAY_ROLLOUT, current.evidence_trace_ids, reviewer, notes)
        self._records[term] = record
        return record

    def activate(self, term: str, *, reviewer: str = "system", notes: str | None = None) -> SlangLifecycleRecord:
        current = self._records[term]
        if current.stage != self.GRAY_ROLLOUT:
            raise ValueError("term must be in GRAY_ROLLOUT before activation")
        record = SlangLifecycleRecord(current.term, current.normalized_term, self.ACTIVE, current.evidence_trace_ids, reviewer, notes)
        self._records[term] = record
        return record

    def ingest_review_decision(self, event_or_payload: Mapping[str, Any] | Any) -> None:
        payload = get_record_field(event_or_payload, "payload") or event_or_payload
        decision = str(get_record_field(payload, "decision") or "").upper()
        source_trace_id = str(get_record_field(payload, "source_trace_id") or "unknown")
        edits = get_record_field(payload, "edits") or {}
        corrected_entities = get_record_field(edits, "corrected_entities") or []
        if decision == "MISREPORT":
            item = {"source_trace_id": source_trace_id, "reason": "review_marked_misreport"}
            self.whitelist_candidates.append(item)
            self.negative_samples.append(item)
        if decision == "APPROVED" and bool(get_record_field(edits, "add_to_wordlist", False)):
            for entity in corrected_entities:
                term = str(get_record_field(entity, "entity_value") or get_record_field(entity, "term") or "").strip()
                if term:
                    self.nominate(term, str(get_record_field(entity, "normalized_value") or term), [source_trace_id])
                    self.few_shot_examples.append({"term": term, "source_trace_id": source_trace_id, "label": get_record_field(edits, "edited_risk_type")})

    def list_records(self, stage: str | None = None) -> list[SlangLifecycleRecord]:
        records = list(self._records.values())
        if stage:
            records = [record for record in records if record.stage == stage]
        return records


@dataclass(frozen=True)
class PromptEvalResult:
    prompt_name: str
    score: float
    passed: bool
    missing_requirements: list[str]
    sample_count: int
    eval_id: str = field(default_factory=lambda: f"prompt_eval_{uuid4().hex[:12]}")

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class PromptEvaluator:
    """Phase III prompt-version evaluator with deterministic checks."""

    DEFAULT_REQUIREMENTS = ("JSON", "confidence", "evidence", "requires_human_review")

    def evaluate(self, prompt_name: str, prompt_text: str, samples: Iterable[Mapping[str, Any] | Any], requirements: Iterable[str] | None = None) -> PromptEvalResult:
        requirements = tuple(requirements or self.DEFAULT_REQUIREMENTS)
        missing = [requirement for requirement in requirements if requirement.lower() not in prompt_text.lower()]
        sample_count = sum(1 for _ in samples)
        score = round(max(0.0, 1.0 - len(missing) / max(1, len(requirements))), 4)
        return PromptEvalResult(prompt_name=prompt_name, score=score, passed=not missing and sample_count > 0, missing_requirements=missing, sample_count=sample_count)


__all__ = ["DynamicSlangLifecycleManager", "PromptEvaluator", "PromptEvalResult", "SlangLifecycleRecord"]
