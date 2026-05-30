"""Composable intelligence pipeline wrapper.

The current production path still uses ``PhaseTwoThreeEngine`` through
``OfflineClueBuilder``.  This module exposes the staged pipeline boundary from
``重构.md`` so new code can compose deterministic stages without making the
orchestrator own every processing detail.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


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
        clean_stage: Any,
        dedup_stage: Any,
        triage_stage: Any,
        classify_stage: Any,
        extract_stage: Any,
        correlate_stage: Any,
        score_stage: Any,
        model_router: Any,
    ) -> None:
        self.clean_stage = clean_stage
        self.dedup_stage = dedup_stage
        self.triage_stage = triage_stage
        self.classify_stage = classify_stage
        self.extract_stage = extract_stage
        self.correlate_stage = correlate_stage
        self.score_stage = score_stage
        self.model_router = model_router

    def run(self, raw_items: Iterable[Mapping[str, Any]], context: Mapping[str, Any] | None = None) -> PipelineResult:
        context = dict(context or {})
        materialized_raw = [dict(item) for item in raw_items]
        cleaned = self.clean_stage.run_batch(materialized_raw, context=context)
        deduped = self.dedup_stage.run_batch(cleaned, context=context)
        triaged = self.triage_stage.run_batch(deduped, context=context)
        classified = self.classify_stage.run_batch(triaged, context=context)
        extracted = self.extract_stage.run_batch(classified, context=context)
        routed = [self._route_item(item) for item in extracted]
        correlated = self.correlate_stage.run_batch(extracted, routed=routed, context=context)
        scored = self.score_stage.run_batch(correlated, context=context)
        return PipelineResult(
            cleaned=[dict(item) for item in cleaned],
            classified=[dict(item) for item in classified],
            entities=[dict(item) for item in extracted],
            routed=routed,
            clues=[dict(item) for item in scored],
            execution_summary={
                "status": "completed",
                "input_count": len(materialized_raw),
                "cleaned_count": len(cleaned),
                "classified_count": len(classified),
                "entity_count": len(extracted),
                "clue_count": len(scored),
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
