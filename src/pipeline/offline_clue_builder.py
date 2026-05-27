"""Offline candidate clue builder over the phase2/3 engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.enhancement.clue_quality import ClueQualityEvaluator
from src.enhancement.engine import PhaseTwoThreeEngine
from storage import ClueRepo, InMemoryClueRepo


@dataclass
class OfflineClueBuildResult:
    status: str
    input_count: int
    saved_clue_count: int
    high_quality_count: int
    candidate_count: int
    execution_summary: dict[str, Any] = field(default_factory=dict)
    clues: list[dict[str, Any]] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class OfflineClueBuilder:
    """Build and persist candidate clues from raw records for later retrieval."""

    def __init__(
        self,
        *,
        phase_engine: PhaseTwoThreeEngine | None = None,
        quality_evaluator: ClueQualityEvaluator | None = None,
        clue_repo: ClueRepo | None = None,
    ) -> None:
        self.phase_engine = phase_engine or PhaseTwoThreeEngine()
        self.quality_evaluator = quality_evaluator or ClueQualityEvaluator()
        self.clue_repo = clue_repo if clue_repo is not None else InMemoryClueRepo()

    def build(
        self,
        records: Iterable[Mapping[str, Any] | Any],
        *,
        prompt_text: str | None = None,
        source_candidates: Iterable[Mapping[str, Any] | Any] = (),
        quality_profile: str = "balanced",
        require_cross_source: bool = False,
        require_evidence_chain: bool = True,
    ) -> OfflineClueBuildResult:
        materialized_records = [dict(record) if isinstance(record, Mapping) else record for record in records]
        record_by_trace = {
            str(record.get("source_trace_id") or record.get("trace_id") or record.get("hash_id") or ""): record
            for record in materialized_records
            if isinstance(record, Mapping)
        }
        result = self.phase_engine.run(materialized_records, prompt_text=prompt_text, source_candidates=source_candidates)
        payload = result.model_dump()
        assessments = self.quality_evaluator.evaluate_many(
            payload.get("risk_clues", []),
            classifications=payload.get("classifications", []),
            entities=payload.get("entities", []),
            quality_profile=quality_profile,
            require_cross_source=require_cross_source,
            require_evidence_chain=require_evidence_chain,
        )
        by_id = {item.clue_id: item for item in assessments}
        saved: list[dict[str, Any]] = []
        high_quality_count = 0
        for clue in payload.get("risk_clues", []):
            enriched = dict(clue)
            assessment = by_id.get(str(clue.get("clue_id") or ""))
            if assessment is not None:
                enriched["quality"] = assessment.model_dump()
                enriched["quality_score"] = assessment.quality_score
                enriched["quality_level"] = assessment.quality_level
            evidence_trace_ids = [str(item) for item in (clue.get("evidence_trace_ids") or [])]
            source_types = sorted(
                {
                    str((record_by_trace.get(trace_id) or {}).get("source_type") or "").strip()
                    for trace_id in evidence_trace_ids
                    if str((record_by_trace.get(trace_id) or {}).get("source_type") or "").strip()
                }
            )
            publish_times = [
                str((record_by_trace.get(trace_id) or {}).get("publish_time") or "")
                for trace_id in evidence_trace_ids
                if str((record_by_trace.get(trace_id) or {}).get("publish_time") or "")
            ]
            if source_types:
                enriched["source_types"] = source_types
            if publish_times:
                enriched["first_seen"] = min(publish_times)
                enriched["last_seen"] = max(publish_times)
            self.clue_repo.save(enriched)
            saved.append(enriched)
            if assessment is not None and assessment.pass_threshold:
                high_quality_count += 1
        return OfflineClueBuildResult(
            status="completed",
            input_count=len(materialized_records),
            saved_clue_count=len(saved),
            high_quality_count=high_quality_count,
            candidate_count=len(saved) - high_quality_count,
            execution_summary={
                key: value
                for key, value in payload.items()
                if key
                in {
                    "status",
                    "mode",
                    "input_count",
                    "accepted_count",
                    "dropped_count",
                    "classification_count",
                    "entity_count",
                    "cluster_count",
                    "risk_clue_count",
                    "playbook_count",
                    "strategy_count",
                }
            },
            clues=saved,
        )


__all__ = ["OfflineClueBuildResult", "OfflineClueBuilder"]
