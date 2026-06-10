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
    lifecycle_version: str | None = None
    batch_id: str | None = None
    target_risk_category: str | None = None
    reviewed_at: str | None = None
    gray_rollout_at: str | None = None
    activated_at: str | None = None
    baseline_eval_version: str | None = None
    post_eval_version: str | None = None
    evaluation_gain: dict[str, Any] | None = None
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
        return self._store_record(
            term=term,
            normalized_term=normalized_term,
            stage=self.NEW_CANDIDATE,
            evidence_trace_ids=evidence_trace_ids,
            allow_downgrade=False,
        )

    def review(
        self,
        term: str,
        *,
        approved: bool,
        reviewer: str = "system",
        notes: str | None = None,
        lifecycle_version: str | None = None,
        batch_id: str | None = None,
        target_risk_category: str | None = None,
        reviewed_at: str | None = None,
        baseline_eval_version: str | None = None,
        post_eval_version: str | None = None,
        evaluation_gain: Mapping[str, Any] | None = None,
    ) -> SlangLifecycleRecord:
        current = self._records[term]
        stage = self.REVIEWED if approved else self.REJECTED
        return self._store_record(
            term=current.term,
            normalized_term=current.normalized_term,
            stage=stage,
            evidence_trace_ids=current.evidence_trace_ids,
            reviewer=reviewer,
            notes=notes,
            lifecycle_version=lifecycle_version,
            batch_id=batch_id,
            target_risk_category=target_risk_category,
            reviewed_at=reviewed_at or datetime.now(timezone.utc).isoformat(),
            baseline_eval_version=baseline_eval_version,
            post_eval_version=post_eval_version,
            evaluation_gain=evaluation_gain,
            allow_downgrade=not approved,
        )

    def gray_rollout(
        self,
        term: str,
        *,
        reviewer: str = "system",
        notes: str | None = None,
        lifecycle_version: str | None = None,
        batch_id: str | None = None,
        target_risk_category: str | None = None,
        gray_rollout_at: str | None = None,
        baseline_eval_version: str | None = None,
        post_eval_version: str | None = None,
        evaluation_gain: Mapping[str, Any] | None = None,
    ) -> SlangLifecycleRecord:
        current = self._records[term]
        if current.stage == self.GRAY_ROLLOUT:
            return current
        if current.stage != self.REVIEWED:
            raise ValueError("term must be REVIEWED before gray rollout")
        return self._store_record(
            term=current.term,
            normalized_term=current.normalized_term,
            stage=self.GRAY_ROLLOUT,
            evidence_trace_ids=current.evidence_trace_ids,
            reviewer=reviewer,
            notes=notes,
            lifecycle_version=lifecycle_version,
            batch_id=batch_id,
            target_risk_category=target_risk_category,
            gray_rollout_at=gray_rollout_at or datetime.now(timezone.utc).isoformat(),
            baseline_eval_version=baseline_eval_version,
            post_eval_version=post_eval_version,
            evaluation_gain=evaluation_gain,
            allow_downgrade=True,
        )

    def activate(
        self,
        term: str,
        *,
        reviewer: str = "system",
        notes: str | None = None,
        lifecycle_version: str | None = None,
        batch_id: str | None = None,
        target_risk_category: str | None = None,
        activated_at: str | None = None,
        baseline_eval_version: str | None = None,
        post_eval_version: str | None = None,
        evaluation_gain: Mapping[str, Any] | None = None,
    ) -> SlangLifecycleRecord:
        current = self._records[term]
        if current.stage != self.GRAY_ROLLOUT:
            raise ValueError("term must be in GRAY_ROLLOUT before activation")
        return self._store_record(
            term=current.term,
            normalized_term=current.normalized_term,
            stage=self.ACTIVE,
            evidence_trace_ids=current.evidence_trace_ids,
            reviewer=reviewer,
            notes=notes,
            lifecycle_version=lifecycle_version,
            batch_id=batch_id,
            target_risk_category=target_risk_category,
            activated_at=activated_at or datetime.now(timezone.utc).isoformat(),
            baseline_eval_version=baseline_eval_version,
            post_eval_version=post_eval_version,
            evaluation_gain=evaluation_gain,
            allow_downgrade=True,
        )

    def promote_approved_candidate(
        self,
        term: str,
        normalized_term: str,
        evidence_trace_ids: Iterable[str],
        *,
        reviewer: str = "system",
        notes: str | None = None,
        lifecycle_version: str | None = None,
        batch_id: str | None = None,
        target_risk_category: str | None = None,
        reviewed_at: str | None = None,
        gray_rollout_at: str | None = None,
        baseline_eval_version: str | None = None,
        post_eval_version: str | None = None,
        evaluation_gain: Mapping[str, Any] | None = None,
    ) -> SlangLifecycleRecord:
        """Promote a human-approved candidate through review and gray rollout.

        Activation is a separate operator action after gray rollout evaluation.
        """

        timestamp = datetime.now(timezone.utc).isoformat()
        self.nominate(term, normalized_term, evidence_trace_ids)
        self.review(
            term,
            approved=True,
            reviewer=reviewer,
            notes=notes,
            lifecycle_version=lifecycle_version,
            batch_id=batch_id,
            target_risk_category=target_risk_category,
            reviewed_at=reviewed_at or timestamp,
            baseline_eval_version=baseline_eval_version,
            post_eval_version=post_eval_version,
            evaluation_gain=evaluation_gain,
        )
        return self.gray_rollout(
            term,
            reviewer=reviewer,
            notes=notes,
            lifecycle_version=lifecycle_version,
            batch_id=batch_id,
            target_risk_category=target_risk_category,
            gray_rollout_at=gray_rollout_at or timestamp,
            baseline_eval_version=baseline_eval_version,
            post_eval_version=post_eval_version,
            evaluation_gain=evaluation_gain,
        )

    def ingest_review_decision(self, event_or_payload: Mapping[str, Any] | Any) -> None:
        payload = get_record_field(event_or_payload, "payload") or event_or_payload
        decision = str(get_record_field(payload, "decision") or "").upper()
        source_trace_id = str(get_record_field(payload, "source_trace_id") or "unknown")
        reviewer = str(get_record_field(payload, "reviewer") or "human_review").strip() or "human_review"
        notes = str(get_record_field(payload, "notes") or "").strip() or None
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
                    normalized_term = str(get_record_field(entity, "normalized_value") or term).strip() or term
                    record = self._store_record(
                        term=term,
                        normalized_term=normalized_term,
                        stage=self.GRAY_ROLLOUT,
                        evidence_trace_ids=[source_trace_id],
                        reviewer=reviewer,
                        notes=notes or "review_approved_wordlist",
                        allow_downgrade=False,
                    )
                    self.few_shot_examples.append(
                        {
                            "term": term,
                            "normalized_term": record.normalized_term,
                            "source_trace_id": source_trace_id,
                            "label": get_record_field(edits, "edited_risk_type"),
                            "stage": record.stage,
                        }
                    )

    def list_records(self, stage: str | None = None) -> list[SlangLifecycleRecord]:
        records = list(self._records.values())
        if stage:
            records = [record for record in records if record.stage == stage]
        return records

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any] | Any]) -> "DynamicSlangLifecycleManager":
        manager = cls()
        for item in records:
            term = str(get_record_field(item, "term") or "").strip()
            if not term:
                continue
            normalized_term = str(get_record_field(item, "normalized_term") or term).strip() or term
            stage = str(get_record_field(item, "stage") or cls.NEW_CANDIDATE).strip().upper() or cls.NEW_CANDIDATE
            evidence_trace_ids = get_record_field(item, "evidence_trace_ids") or []
            if isinstance(evidence_trace_ids, str):
                evidence_trace_ids = [evidence_trace_ids]
            evaluation_gain = get_record_field(item, "evaluation_gain")
            manager._store_record(
                term=term,
                normalized_term=normalized_term,
                stage=stage,
                evidence_trace_ids=evidence_trace_ids,
                reviewer=get_record_field(item, "reviewer"),
                notes=get_record_field(item, "notes"),
                lifecycle_version=get_record_field(item, "lifecycle_version"),
                batch_id=get_record_field(item, "batch_id"),
                target_risk_category=get_record_field(item, "target_risk_category"),
                reviewed_at=get_record_field(item, "reviewed_at"),
                gray_rollout_at=get_record_field(item, "gray_rollout_at"),
                activated_at=get_record_field(item, "activated_at"),
                baseline_eval_version=get_record_field(item, "baseline_eval_version"),
                post_eval_version=get_record_field(item, "post_eval_version"),
                evaluation_gain=evaluation_gain if isinstance(evaluation_gain, Mapping) else None,
                allow_downgrade=True,
            )
        return manager

    def runtime_records(
        self,
        *,
        include_candidates: bool = False,
        include_gray: bool = False,
    ) -> list[SlangLifecycleRecord]:
        stages = set(self.runtime_stages(include_candidates=include_candidates, include_gray=include_gray))
        records = [record for record in self._records.values() if record.stage in stages]
        return sorted(records, key=lambda item: (-self._stage_priority(item.stage), item.term.lower()))

    def runtime_terms_mapping(
        self,
        *,
        include_candidates: bool = False,
        include_gray: bool = False,
    ) -> dict[str, str]:
        return {
            record.term: record.normalized_term
            for record in self.runtime_records(include_candidates=include_candidates, include_gray=include_gray)
        }

    def runtime_slang_entries(
        self,
        *,
        include_candidates: bool = False,
        include_gray: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            {
                "term": record.term,
                "normalized_term": record.normalized_term,
                "stage": record.stage,
                "evidence_trace_ids": list(record.evidence_trace_ids),
            }
            for record in self.runtime_records(include_candidates=include_candidates, include_gray=include_gray)
        ]

    def few_shot_examples_for_label(self, label: str | None = None) -> list[dict[str, Any]]:
        if label is None:
            return [dict(item) for item in self.few_shot_examples]
        normalized_label = str(label).strip().lower()
        return [
            dict(item)
            for item in self.few_shot_examples
            if str(item.get("label") or "").strip().lower() == normalized_label
        ]

    def prompt_context(
        self,
        *,
        label: str | None = None,
        include_candidates: bool = False,
        include_gray: bool = False,
    ) -> dict[str, Any]:
        return {
            "slang_terms": self.runtime_slang_entries(include_candidates=include_candidates, include_gray=include_gray),
            "few_shot_examples": self.few_shot_examples_for_label(label),
            "negative_samples": [dict(item) for item in self.negative_samples],
            "whitelist_candidates": [dict(item) for item in self.whitelist_candidates],
        }

    @classmethod
    def runtime_stages(cls, *, include_candidates: bool = False, include_gray: bool = False) -> tuple[str, ...]:
        stages = [cls.ACTIVE]
        if include_gray:
            stages.insert(0, cls.GRAY_ROLLOUT)
        if include_candidates:
            stages.insert(0, cls.NEW_CANDIDATE)
        return tuple(stages)

    @classmethod
    def _stage_priority(cls, stage: str) -> int:
        priorities = {
            cls.REJECTED: 0,
            cls.NEW_CANDIDATE: 1,
            cls.REVIEWED: 2,
            cls.GRAY_ROLLOUT: 3,
            cls.ACTIVE: 4,
        }
        return priorities.get(str(stage or "").strip().upper(), 0)

    def _store_record(
        self,
        *,
        term: str,
        normalized_term: str,
        stage: str,
        evidence_trace_ids: Iterable[str],
        reviewer: str | None = None,
        notes: str | None = None,
        lifecycle_version: str | None = None,
        batch_id: str | None = None,
        target_risk_category: str | None = None,
        reviewed_at: str | None = None,
        gray_rollout_at: str | None = None,
        activated_at: str | None = None,
        baseline_eval_version: str | None = None,
        post_eval_version: str | None = None,
        evaluation_gain: Mapping[str, Any] | None = None,
        allow_downgrade: bool,
    ) -> SlangLifecycleRecord:
        normalized_key = str(term).strip()
        if not normalized_key:
            raise ValueError("term must not be empty")
        current = self._records.get(normalized_key)
        requested_stage = str(stage).strip().upper() or self.NEW_CANDIDATE
        final_stage = requested_stage
        final_normalized = str(normalized_term).strip() or normalized_key
        final_reviewer = reviewer
        final_notes = notes
        merged_evidence = self._merge_evidence(current.evidence_trace_ids if current else (), evidence_trace_ids)
        if current is not None and not allow_downgrade and self._stage_priority(current.stage) > self._stage_priority(requested_stage):
            final_stage = current.stage
            final_normalized = current.normalized_term or final_normalized
            final_reviewer = current.reviewer if reviewer is None else reviewer
            final_notes = current.notes if notes is None else notes
        elif current is not None:
            if reviewer is None:
                final_reviewer = current.reviewer
            if notes is None:
                final_notes = current.notes
        metadata = self._record_metadata(
            current,
            lifecycle_version=lifecycle_version,
            batch_id=batch_id,
            target_risk_category=target_risk_category,
            reviewed_at=reviewed_at,
            gray_rollout_at=gray_rollout_at,
            activated_at=activated_at,
            baseline_eval_version=baseline_eval_version,
            post_eval_version=post_eval_version,
            evaluation_gain=evaluation_gain,
        )
        record = SlangLifecycleRecord(
            term=normalized_key,
            normalized_term=final_normalized,
            stage=final_stage,
            evidence_trace_ids=merged_evidence,
            reviewer=final_reviewer,
            notes=final_notes,
            **metadata,
        )
        self._records[normalized_key] = record
        return record

    @staticmethod
    def _record_metadata(
        current: SlangLifecycleRecord | None,
        *,
        lifecycle_version: str | None,
        batch_id: str | None,
        target_risk_category: str | None,
        reviewed_at: str | None,
        gray_rollout_at: str | None,
        activated_at: str | None,
        baseline_eval_version: str | None,
        post_eval_version: str | None,
        evaluation_gain: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        def choose(field_name: str, incoming: Any) -> Any:
            if incoming is not None:
                return incoming
            return getattr(current, field_name) if current is not None else None

        selected_gain = choose("evaluation_gain", dict(evaluation_gain) if evaluation_gain is not None else None)
        return {
            "lifecycle_version": choose("lifecycle_version", lifecycle_version),
            "batch_id": choose("batch_id", batch_id),
            "target_risk_category": choose("target_risk_category", target_risk_category),
            "reviewed_at": choose("reviewed_at", reviewed_at),
            "gray_rollout_at": choose("gray_rollout_at", gray_rollout_at),
            "activated_at": choose("activated_at", activated_at),
            "baseline_eval_version": choose("baseline_eval_version", baseline_eval_version),
            "post_eval_version": choose("post_eval_version", post_eval_version),
            "evaluation_gain": dict(selected_gain) if isinstance(selected_gain, Mapping) else selected_gain,
        }

    @staticmethod
    def _merge_evidence(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for value in [*existing, *incoming]:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
        return merged


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
