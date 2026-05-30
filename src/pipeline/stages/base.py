"""Base stage primitives for the composable intelligence pipeline."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.cleaner.pipeline import CleanerPipeline
from src.enhancement.clue_quality import ClueQualityEvaluator
from src.enhancement.source_intake import MultimodalTextExtractor
from src.enhancement.strategy import RiskClueAggregator
from src.enhancement.text_intelligence import AdaptiveEntropyFilter, AdvancedEntityExtractor, FineGrainedIntentClassifier


class PassThroughStage:
    """Default stage used while legacy processors are wrapped incrementally."""

    def run_batch(self, items: Iterable[Mapping[str, Any]], **_: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in items]


class CleanStage:
    """Materialize multimodal text and apply existing cleaner rules."""

    def __init__(self, *, extractor: MultimodalTextExtractor | None = None, cleaner: CleanerPipeline | None = None) -> None:
        self.extractor = extractor or MultimodalTextExtractor()
        self.cleaner = cleaner or CleanerPipeline(keep_duplicates=True)

    def run_batch(self, items: Iterable[Mapping[str, Any]], **_: Any) -> list[dict[str, Any]]:
        materialized = [self.extractor.materialize(item) for item in items]
        cleaned = self.cleaner.clean(materialized)
        raw_by_trace = {
            str(item.get("source_trace_id") or item.get("trace_id") or item.get("hash_id") or ""): item
            for item in materialized
        }
        output: list[dict[str, Any]] = []
        for item in cleaned.cleaned:
            payload = _dump(item)
            trace_id = str(payload.get("source_trace_id") or "")
            merged = dict(raw_by_trace.get(trace_id, {}))
            merged.update(payload)
            merged.setdefault("trace_id", trace_id)
            merged.setdefault("content_text", payload.get("clean_text") or merged.get("content_text"))
            output.append(merged)
        return output


class DedupStage:
    """Mark near duplicates while preserving all records for downstream clue aggregation."""

    def run_batch(self, items: Iterable[Mapping[str, Any]], **_: Any) -> list[dict[str, Any]]:
        seen: dict[str, str] = {}
        output: list[dict[str, Any]] = []
        for item in items:
            payload = dict(item)
            group_id = str(payload.get("dedup_group_id") or payload.get("clean_text") or payload.get("content_text") or "")
            if group_id in seen:
                payload["is_duplicate"] = True
                payload["duplicate_of"] = seen[group_id]
            elif group_id:
                seen[group_id] = str(payload.get("source_trace_id") or payload.get("trace_id") or "")
                payload["is_duplicate"] = False
            output.append(payload)
        return output


class ClassifyStage:
    """Attach fine-grained deterministic classification results."""

    def __init__(self, classifier: FineGrainedIntentClassifier | None = None) -> None:
        self.classifier = classifier or FineGrainedIntentClassifier()

    def run_batch(self, items: Iterable[Mapping[str, Any]], **_: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for item in items:
            payload = dict(item)
            classification = self.classifier.classify(payload).model_dump()
            payload["classification"] = classification
            payload["confidence"] = classification.get("confidence", payload.get("confidence"))
            payload["risk_category"] = classification.get("risk_category")
            payload["has_conflict"] = classification.get("conflict_status") == "CONFLICT_REVIEW"
            output.append(payload)
        return output


class ExtractStage:
    """Attach deterministic entities and record-level routing features."""

    def __init__(self, extractor: AdvancedEntityExtractor | None = None) -> None:
        self.extractor = extractor or AdvancedEntityExtractor()

    def run_batch(self, items: Iterable[Mapping[str, Any]], **_: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for item in items:
            payload = dict(item)
            entities = [entity.model_dump() for entity in self.extractor.extract(payload)]
            entity_types = {str(entity.get("entity_type") or "").lower() for entity in entities}
            payload["entities"] = entities
            payload["entity_count"] = len(entities)
            payload["has_contact"] = bool(entity_types.intersection({"contact", "account"}))
            payload["has_url"] = bool(entity_types.intersection({"url", "domain"}))
            payload["has_tool"] = "tool_name" in entity_types
            output.append(payload)
        return output


class CorrelateStage:
    """Use RiskClueAggregator over classified/extracted stage records."""

    def __init__(self, aggregator: RiskClueAggregator | None = None) -> None:
        self.aggregator = aggregator or RiskClueAggregator()

    def run_batch(self, items: Iterable[Mapping[str, Any]], **_: Any) -> list[dict[str, Any]]:
        records = [dict(item) for item in items]
        classifications = [dict(item.get("classification") or {}) for item in records if isinstance(item.get("classification"), Mapping)]
        entities = [dict(entity) for item in records for entity in (item.get("entities") or []) if isinstance(entity, Mapping)]
        clues = [
            clue.model_dump() if hasattr(clue, "model_dump") else dict(clue)
            for clue in self.aggregator.aggregate(records=records, classifications=classifications, entities=entities)
        ]
        return clues


class ScoreStage:
    """Attach clue quality metadata using the existing evaluator."""

    def __init__(self, evaluator: ClueQualityEvaluator | None = None) -> None:
        self.evaluator = evaluator or ClueQualityEvaluator()

    def run_batch(self, items: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        clues = [dict(item) for item in items]
        context = dict(kwargs.get("context") or {})
        assessments = self.evaluator.evaluate_many(
            clues,
            classifications=context.get("classifications") or [],
            entities=context.get("entities") or [],
            quality_profile=str(context.get("quality_profile") or "balanced"),
            require_cross_source=bool(context.get("require_cross_source", False)),
            require_evidence_chain=bool(context.get("require_evidence_chain", True)),
        )
        by_id = {item.clue_id: item for item in assessments}
        output: list[dict[str, Any]] = []
        for clue in clues:
            payload = dict(clue)
            assessment = by_id.get(str(payload.get("clue_id") or ""))
            if assessment is not None:
                payload["quality"] = assessment.model_dump()
                payload["quality_score"] = assessment.quality_score
                payload["quality_level"] = assessment.quality_level
            output.append(payload)
        return output


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


__all__ = ["ClassifyStage", "CleanStage", "CorrelateStage", "DedupStage", "ExtractStage", "PassThroughStage", "ScoreStage"]
