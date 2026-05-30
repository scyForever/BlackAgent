"""Composable intelligence pipeline wrapper over real deterministic stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.agent.model_router import ModelRouter
from src.pipeline.stages import ClassifyStage, CleanStage, CorrelateStage, DedupStage, ExtractStage, PassThroughStage, ScoreStage


@dataclass
class PipelineResult:
    cleaned: list[dict[str, Any]] = field(default_factory=list)
    classified: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    routed: list[dict[str, Any]] = field(default_factory=list)
    clues: list[dict[str, Any]] = field(default_factory=list)
    execution_summary: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class IntelligencePipeline:
    """Run clean, dedup, classify, extract, correlate, and score stages."""

    def __init__(
        self,
        *,
        clean_stage: Any | None = None,
        dedup_stage: Any | None = None,
        triage_stage: Any | None = None,
        classify_stage: Any | None = None,
        extract_stage: Any | None = None,
        correlate_stage: Any | None = None,
        score_stage: Any | None = None,
        model_router: Any | None = None,
    ) -> None:
        self.clean_stage = clean_stage or CleanStage()
        self.dedup_stage = dedup_stage or DedupStage()
        self.triage_stage = triage_stage or PassThroughStage()
        self.classify_stage = classify_stage or ClassifyStage()
        self.extract_stage = extract_stage or ExtractStage()
        self.correlate_stage = correlate_stage or CorrelateStage()
        self.score_stage = score_stage or ScoreStage()
        self.model_router = model_router or ModelRouter()

    def run(self, raw_items: Iterable[Mapping[str, Any]], context: Mapping[str, Any] | None = None) -> PipelineResult:
        context = dict(context or {})
        materialized_raw = [dict(item) for item in raw_items]
        cleaned = self.clean_stage.run_batch(materialized_raw, context=context)
        deduped = self.dedup_stage.run_batch(cleaned, context=context)
        triaged = self.triage_stage.run_batch(deduped, context=context)
        classified = self.classify_stage.run_batch(triaged, context=context)
        extracted = self.extract_stage.run_batch(classified, context=context)
        routed = [self._route_item(item) for item in extracted]
        classifications = [dict(item.get("classification") or {}) for item in extracted if isinstance(item.get("classification"), Mapping)]
        entities = [dict(entity) for item in extracted for entity in (item.get("entities") or []) if isinstance(entity, Mapping)]
        stage_context = {**context, "classifications": classifications, "entities": entities}
        correlated = self.correlate_stage.run_batch(extracted, routed=routed, context=stage_context)
        scored = self.score_stage.run_batch(correlated, context=stage_context)
        return PipelineResult(
            cleaned=[dict(item) for item in cleaned],
            classified=classifications,
            entities=entities,
            routed=routed,
            clues=[dict(item) for item in scored],
            execution_summary={
                "status": "completed",
                "input_count": len(materialized_raw),
                "cleaned_count": len(cleaned),
                "classified_count": len(classifications),
                "entity_count": len(entities),
                "clue_count": len(scored),
                "stage_mode": "real_components",
            },
        )

    def _route_item(self, item: Mapping[str, Any]) -> dict[str, Any]:
        decision = self.model_router.decide_record(
            rule_confidence=float(item.get("confidence") or item.get("rule_confidence") or 0.0),
            risk_score=float(item.get("risk_score") or 0.0),
            entity_count=int(item.get("entity_count") or len(item.get("entities") or [])),
            has_contact=bool(item.get("has_contact")),
            has_url=bool(item.get("has_url")),
            has_tool=bool(item.get("has_tool")),
            has_conflict=bool(item.get("has_conflict")),
            is_duplicate=bool(item.get("is_duplicate") or item.get("duplicate_of")),
            quality_score=float(item.get("quality_score") or 0.0),
        )
        return decision.model_dump()


__all__ = ["IntelligencePipeline", "PipelineResult"]
