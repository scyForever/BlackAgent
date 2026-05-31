"""Base stage primitives for the composable intelligence pipeline."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol

from src.cleaner.pipeline import CleanerPipeline
from src.domain import CleanedRecord, ExtractedEntity, IntelRecord, PipelineItem, RiskClassification
from src.enhancement.clue_quality import ClueQualityEvaluator
from src.enhancement.source_intake import MultimodalTextExtractor
from src.enhancement.strategy import RiskClueAggregator
from src.enhancement.text_intelligence import AdaptiveEntropyFilter, AdvancedEntityExtractor, FineGrainedIntentClassifier
from storage.entity_graph import EntityGraphStore


class Stage(Protocol):
    """Typed stage boundary: PipelineItem in, PipelineItem out.

    Legacy stage implementations still accept dictionaries for compatibility;
    this protocol documents the target contract used by the core pipeline.
    """

    def run_batch(self, items: list[PipelineItem], **kwargs: Any) -> list[PipelineItem]:
        ...


class PassThroughStage:
    """Default stage used while legacy processors are wrapped incrementally."""

    def run_batch(self, items: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in items]


class CleanStage:
    """Materialize multimodal text and apply existing cleaner rules."""

    def __init__(self, *, extractor: MultimodalTextExtractor | None = None, cleaner: CleanerPipeline | None = None) -> None:
        self.extractor = extractor or MultimodalTextExtractor()
        self.cleaner = cleaner or CleanerPipeline(keep_duplicates=True)

    def run_batch(self, items: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
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
            pipeline_item = PipelineItem(
                record=IntelRecord(
                    trace_id=str(merged.get("trace_id") or trace_id),
                    source_name=_optional_str(merged.get("source_name")),
                    source_type=_optional_str(merged.get("source_type")),
                    legal_basis=_optional_str(merged.get("legal_basis")),
                    content_text=str(merged.get("content_text") or merged.get("clean_text") or ""),
                    publish_time=_optional_str(merged.get("publish_time")),
                    metadata={key: value for key, value in merged.items() if key not in {"content_text", "clean_text"}},
                ),
                cleaned=CleanedRecord(
                    trace_id=str(merged.get("trace_id") or trace_id),
                    raw_text=str((raw_by_trace.get(trace_id, {}) or {}).get("content_text") or ""),
                    clean_text=str(merged.get("clean_text") or merged.get("content_text") or ""),
                    normalized_text=str(merged.get("normalized_text") or merged.get("clean_text") or merged.get("content_text") or ""),
                    quality_score=float(merged.get("quality_score") or 0.0),
                    noise_score=float(merged.get("noise_score") or 0.0),
                    dedup_group_id=_optional_str(merged.get("dedup_group_id")),
                ),
                payload=merged,
            )
            merged["domain_contract"] = pipeline_item.model_dump()
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
            _sync_cleaned_contract(payload)
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
            _sync_classification_contract(payload, classification)
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
            _sync_entity_contracts(payload, entities)
            output.append(payload)
        return output


class CorrelateStage:
    """Use RiskClueAggregator over classified/extracted stage records."""

    def __init__(self, aggregator: RiskClueAggregator | None = None, entity_graph: EntityGraphStore | None = None) -> None:
        self.aggregator = aggregator or RiskClueAggregator()
        self.entity_graph = entity_graph or EntityGraphStore()

    def run_batch(self, items: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        records = [dict(item) for item in items]
        classifications = [dict(item.get("classification") or {}) for item in records if isinstance(item.get("classification"), Mapping)]
        entities = [dict(entity) for item in records for entity in (item.get("entities") or []) if isinstance(entity, Mapping)]
        record_by_trace = {str(record.get("trace_id") or record.get("source_trace_id") or ""): record for record in records}
        for entity in entities:
            trace_id = str(entity.get("source_trace_id") or entity.get("trace_id") or "")
            self.entity_graph.add_observation(entity, record_by_trace.get(trace_id, {}))
        for record in records:
            item_entities = [entity for entity in (record.get("entities") or []) if isinstance(entity, Mapping)]
            entity_ids = [
                self.entity_graph.add_observation(entity, record).entity_id
                for entity in item_entities
                if str(entity.get("normalized_value") or entity.get("entity_value") or "").strip()
            ]
            for index, src_id in enumerate(entity_ids):
                for dst_id in entity_ids[index + 1 :]:
                    self.entity_graph.add_relation(
                        src_id,
                        dst_id,
                        "CO_OCCURS_IN_RECORD",
                        evidence_trace_ids=[str(record.get("trace_id") or record.get("source_trace_id") or "")],
                        confidence=0.72,
                    )
        clues = [
            clue.model_dump() if hasattr(clue, "model_dump") else dict(clue)
            for clue in self.aggregator.aggregate(records=records, classifications=classifications, entities=entities)
        ]
        graph_snapshot = self.entity_graph.snapshot()
        graph_clues = self.entity_graph.generate_clues() if bool((kwargs.get("context") or {}).get("enable_graph_clue_generation")) else []
        existing_keys = {f"{clue.get('clue_type')}|{clue.get('key')}|{clue.get('risk_category')}" for clue in clues}
        for graph_clue in graph_clues:
            key = f"{graph_clue.get('clue_type')}|{graph_clue.get('key')}|{graph_clue.get('risk_category')}"
            if key not in existing_keys:
                clues.append(graph_clue)
                existing_keys.add(key)
        for clue in clues:
            clue["entity_observation_refs"] = [
                observation["observation_id"]
                for observation in graph_snapshot["observations"]
                if observation["trace_id"] in set(clue.get("evidence_trace_ids") or [])
            ]
            clue["entity_graph_backend"] = "entity_graph_store"
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


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _contract_payload(item: dict[str, Any]) -> dict[str, Any]:
    existing = item.get("domain_contract") if isinstance(item.get("domain_contract"), Mapping) else {}
    if existing:
        return dict(existing)
    trace_id = str(item.get("trace_id") or item.get("source_trace_id") or item.get("hash_id") or "unknown")
    contract = PipelineItem(
        record=IntelRecord(
            trace_id=trace_id,
            source_name=_optional_str(item.get("source_name")),
            source_type=_optional_str(item.get("source_type")),
            legal_basis=_optional_str(item.get("legal_basis")),
            content_text=str(item.get("content_text") or item.get("clean_text") or ""),
            publish_time=_optional_str(item.get("publish_time")),
            metadata={},
        ),
        payload=dict(item),
    )
    return contract.model_dump()


def _sync_cleaned_contract(item: dict[str, Any]) -> None:
    contract = _contract_payload(item)
    trace_id = str(item.get("trace_id") or item.get("source_trace_id") or contract.get("record", {}).get("trace_id") or "unknown")
    cleaned = dict(contract.get("cleaned") or {})
    cleaned.update(
        CleanedRecord(
            trace_id=trace_id,
            raw_text=str(cleaned.get("raw_text") or item.get("content_text") or ""),
            clean_text=str(item.get("clean_text") or item.get("content_text") or ""),
            normalized_text=str(item.get("normalized_text") or item.get("clean_text") or item.get("content_text") or ""),
            quality_score=float(item.get("quality_score") or cleaned.get("quality_score") or 0.0),
            noise_score=float(item.get("noise_score") or cleaned.get("noise_score") or 0.0),
            dedup_group_id=_optional_str(item.get("dedup_group_id")),
            is_duplicate=bool(item.get("is_duplicate")),
            duplicate_of=_optional_str(item.get("duplicate_of")),
        ).model_dump()
    )
    contract["cleaned"] = cleaned
    contract["payload"] = dict(item)
    item["domain_contract"] = contract


def _sync_classification_contract(item: dict[str, Any], classification: Mapping[str, Any]) -> None:
    contract = _contract_payload(item)
    trace_id = str(item.get("trace_id") or item.get("source_trace_id") or classification.get("source_trace_id") or "unknown")
    contract["classification"] = RiskClassification(
        trace_id=trace_id,
        risk_category=str(classification.get("risk_category") or "unknown"),
        secondary_label=str(classification.get("secondary_label") or "待研判"),
        confidence=float(classification.get("confidence") or 0.0),
        conflict_status=_optional_str(classification.get("conflict_status")),
        evidence=[str(value) for value in (classification.get("evidence") or [])],
        review_required=bool(classification.get("review_required")),
        classifier_version=str(classification.get("classifier_version") or classification.get("decision_version") or "unknown"),
    ).model_dump()
    contract["payload"] = dict(item)
    item["domain_contract"] = contract


def _sync_entity_contracts(item: dict[str, Any], entities: Iterable[Mapping[str, Any]]) -> None:
    contract = _contract_payload(item)
    trace_id = str(item.get("trace_id") or item.get("source_trace_id") or contract.get("record", {}).get("trace_id") or "unknown")
    normalized_entities: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        value = str(entity.get("normalized_value") or entity.get("entity_value") or "")
        if not value:
            continue
        normalized_entities.append(
            ExtractedEntity(
                entity_id=str(entity.get("entity_id") or f"{trace_id}:{index}:{entity.get('entity_type') or 'entity'}"),
                trace_id=str(entity.get("source_trace_id") or trace_id),
                entity_type=str(entity.get("entity_type") or "unknown"),
                raw_value=_optional_str(entity.get("entity_value") or entity.get("raw_value")),
                normalized_value=value,
                masked_value=_optional_str(entity.get("masked_value")),
                confidence=float(entity.get("confidence") or 0.0),
                sensitivity_level=str(entity.get("sensitivity_level") or "normal"),
                extraction_method=str(entity.get("extraction_method") or entity.get("extractor_version") or "unknown"),
            ).model_dump()
        )
    contract["entities"] = normalized_entities
    contract["payload"] = dict(item)
    item["domain_contract"] = contract


__all__ = ["ClassifyStage", "CleanStage", "CorrelateStage", "DedupStage", "ExtractStage", "PassThroughStage", "ScoreStage", "Stage"]
