"""Base stage primitives for the composable intelligence pipeline."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol

from src.cleaner.pipeline import CleanerPipeline
from src.domain import CleanedRecord, ClassificationResolution, ExtractedEntity, IntelRecord, PipelineItem, RiskClassification
from src.pipeline.classification_resolution import resolve_classification
from src.enhancement.clue_quality import ClueQualityEvaluator, build_evidence_reviewability
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

    def run_batch(self, items: Iterable[Mapping[str, Any] | PipelineItem], **kwargs: Any) -> list[PipelineItem]:
        return [_coerce_pipeline_item(item) for item in items]


class CleanStage:
    """Materialize multimodal text and apply existing cleaner rules."""

    def __init__(self, *, extractor: MultimodalTextExtractor | None = None, cleaner: CleanerPipeline | None = None) -> None:
        self.extractor = extractor or MultimodalTextExtractor()
        self.cleaner = cleaner or CleanerPipeline(keep_duplicates=True)

    def run_batch(self, items: Iterable[Mapping[str, Any] | PipelineItem], **kwargs: Any) -> list[PipelineItem]:
        materialized = [self.extractor.materialize(_payload_from_item(item)) for item in items]
        cleaned = self.cleaner.clean(materialized)
        raw_by_trace = {
            str(item.get("source_trace_id") or item.get("trace_id") or item.get("hash_id") or ""): item
            for item in materialized
        }
        output: list[PipelineItem] = []
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
            pipeline_item = _sync_item_payload(pipeline_item)
            output.append(pipeline_item)
        return output


class DedupStage:
    """Mark near duplicates while preserving all records for downstream clue aggregation."""

    def run_batch(self, items: Iterable[Mapping[str, Any] | PipelineItem], **_: Any) -> list[PipelineItem]:
        seen: dict[str, str] = {}
        output: list[PipelineItem] = []
        for item in items:
            current = _coerce_pipeline_item(item)
            payload = dict(current.payload)
            group_id = str(payload.get("dedup_group_id") or payload.get("clean_text") or payload.get("content_text") or "")
            if group_id in seen:
                payload["is_duplicate"] = True
                payload["duplicate_of"] = seen[group_id]
            elif group_id:
                seen[group_id] = str(payload.get("source_trace_id") or payload.get("trace_id") or "")
                payload["is_duplicate"] = False
            current = _item_with_payload(current, payload)
            current = current.model_copy(
                update={
                    "cleaned": CleanedRecord(
                        trace_id=current.cleaned.trace_id if current.cleaned is not None else current.record.trace_id,
                        raw_text=current.cleaned.raw_text if current.cleaned is not None else current.record.content_text,
                        clean_text=current.cleaned.clean_text if current.cleaned is not None else str(payload.get("clean_text") or current.record.content_text),
                        normalized_text=(
                            current.cleaned.normalized_text
                            if current.cleaned is not None
                            else str(payload.get("normalized_text") or payload.get("clean_text") or current.record.content_text)
                        ),
                        quality_score=current.cleaned.quality_score if current.cleaned is not None else float(payload.get("quality_score") or 0.0),
                        noise_score=current.cleaned.noise_score if current.cleaned is not None else float(payload.get("noise_score") or 0.0),
                        dedup_group_id=_optional_str(payload.get("dedup_group_id")),
                        is_duplicate=bool(payload.get("is_duplicate")),
                        duplicate_of=_optional_str(payload.get("duplicate_of")),
                    )
                }
            )
            output.append(_sync_item_payload(current))
        return output


class ClassifyStage:
    """Attach fine-grained deterministic classification results."""

    def __init__(self, classifier: FineGrainedIntentClassifier | None = None) -> None:
        self.classifier = classifier or FineGrainedIntentClassifier()

    def run_batch(self, items: Iterable[Mapping[str, Any] | PipelineItem], **_: Any) -> list[PipelineItem]:
        output: list[PipelineItem] = []
        for item in items:
            current = _coerce_pipeline_item(item)
            payload = dict(current.payload)
            classification = self.classifier.classify(payload).model_dump()
            resolution = resolve_classification(classification, {}, trace_id=str(payload.get("source_trace_id") or payload.get("trace_id") or current.record.trace_id))
            payload["rule_classification"] = classification
            payload["classification"] = {
                "rule": dict(classification),
                "llm": {},
                "final": dict(resolution.final),
                "resolution": resolution.model_dump(),
                # Legacy mirror fields remain only for JSON/CLI compatibility.
                **dict(resolution.final),
            }
            payload["classification_resolution"] = resolution.model_dump()
            payload["confidence"] = classification.get("confidence", payload.get("confidence"))
            payload["risk_category"] = classification.get("risk_category")
            payload["has_conflict"] = classification.get("conflict_status") == "CONFLICT_REVIEW"
            typed_classification = RiskClassification(
                trace_id=str(classification.get("source_trace_id") or payload.get("trace_id") or current.record.trace_id),
                risk_category=str(resolution.final.get("risk_category") or "unknown"),
                secondary_label=str(resolution.final.get("secondary_label") or "待研判"),
                final_secondary_label=_optional_str(resolution.final.get("final_secondary_label") or resolution.final.get("secondary_label")),
                candidate_secondary_labels=[
                    dict(item)
                    for item in (resolution.final.get("candidate_secondary_labels") or [])
                    if isinstance(item, Mapping)
                ],
                confidence=float(resolution.final.get("confidence") or 0.0),
                conflict_status=_optional_str(resolution.final.get("conflict_status")),
                evidence=[str(value) for value in (resolution.final.get("evidence") or [])],
                review_required=bool(resolution.final.get("review_required")),
                review_bucket=str(resolution.final.get("review_bucket") or "human_review_required"),
                classifier_version=str(resolution.final.get("classifier_version") or "unknown"),
            )
            current = _item_with_payload(
                current.model_copy(
                    update={
                        "classification": typed_classification,
                        "classification_resolution": resolution,
                    }
                ),
                payload,
            )
            output.append(_sync_item_payload(current))
        return output


class ExtractStage:
    """Attach deterministic entities and record-level routing features."""

    def __init__(self, extractor: AdvancedEntityExtractor | None = None) -> None:
        self.extractor = extractor or AdvancedEntityExtractor()

    def run_batch(self, items: Iterable[Mapping[str, Any] | PipelineItem], **_: Any) -> list[PipelineItem]:
        output: list[PipelineItem] = []
        for item in items:
            current = _coerce_pipeline_item(item)
            payload = dict(current.payload)
            entities = [entity.model_dump() for entity in self.extractor.extract(payload)]
            entity_types = {str(entity.get("entity_type") or "").lower() for entity in entities}
            payload["entities"] = entities
            payload["entity_count"] = len(entities)
            payload["has_contact"] = bool(entity_types.intersection({"contact", "account"}))
            payload["has_url"] = bool(entity_types.intersection({"url", "domain"}))
            payload["has_tool"] = "tool_name" in entity_types
            typed_entities = _entities_from_payload(payload, entities)
            current = _item_with_payload(current.model_copy(update={"entities": typed_entities}), payload)
            output.append(_sync_item_payload(current))
        return output


class CorrelateStage:
    """Use RiskClueAggregator over classified/extracted stage records."""

    def __init__(self, aggregator: RiskClueAggregator | None = None, entity_graph: EntityGraphStore | None = None) -> None:
        self.aggregator = aggregator or RiskClueAggregator()
        self.entity_graph = entity_graph or EntityGraphStore()

    def run_batch(self, items: Iterable[Mapping[str, Any] | PipelineItem], **kwargs: Any) -> list[dict[str, Any]]:
        records = [_payload_from_item(item) for item in items]
        classifications = [_final_classification(item) for item in records if isinstance(item.get("classification"), Mapping)]
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
        for clue in clues:
            clue.setdefault("clue_stage", "candidate")
            clue.setdefault("weak_reason", clue.get("threshold_reason") or "aggregator_candidate")
        graph_snapshot = self.entity_graph.snapshot()
        graph_clues = self.entity_graph.generate_clues() if bool((kwargs.get("context") or {}).get("enable_graph_clue_generation")) else []
        existing_keys = {f"{clue.get('clue_type')}|{clue.get('key')}|{clue.get('risk_category')}" for clue in clues}
        for graph_clue in graph_clues:
            key = f"{graph_clue.get('clue_type')}|{graph_clue.get('key')}|{graph_clue.get('risk_category')}"
            if key not in existing_keys:
                clue = dict(graph_clue)
                clue.setdefault("clue_stage", "candidate")
                clue.setdefault("weak_reason", clue.get("threshold_reason") or "entity_graph_candidate")
                clues.append(clue)
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
        entities = context.get("entities") or []
        records = context.get("records") or context.get("raw_records") or context.get("items") or []
        assessments = self.evaluator.evaluate_many(
            clues,
            classifications=context.get("classifications") or [],
            entities=entities,
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
                payload["evidence_reviewability"] = build_evidence_reviewability(
                    payload,
                    assessment=assessment,
                    entities=entities,
                    records=records,
                )
            output.append(payload)
        return output


def _coerce_pipeline_item(item: Mapping[str, Any] | PipelineItem) -> PipelineItem:
    if isinstance(item, PipelineItem):
        return _sync_item_payload(item)
    payload = dict(item)
    contract = payload.get("domain_contract") if isinstance(payload.get("domain_contract"), Mapping) else None
    if contract:
        loaded = PipelineItem.model_validate(dict(contract))
        if payload:
            loaded = loaded.model_copy(update={"payload": {**loaded.payload, **payload}})
        return _sync_item_payload(loaded)

    trace_id = str(payload.get("trace_id") or payload.get("source_trace_id") or payload.get("hash_id") or "unknown")
    content_text = str(payload.get("content_text") or payload.get("clean_text") or trace_id)
    cleaned = None
    if payload.get("clean_text") or payload.get("normalized_text") or payload.get("quality_score") is not None:
        cleaned = CleanedRecord(
            trace_id=trace_id,
            raw_text=str(payload.get("raw_text") or payload.get("content_text") or ""),
            clean_text=str(payload.get("clean_text") or payload.get("content_text") or content_text),
            normalized_text=str(payload.get("normalized_text") or payload.get("clean_text") or content_text),
            quality_score=float(payload.get("quality_score") or 0.0),
            noise_score=float(payload.get("noise_score") or 0.0),
            dedup_group_id=_optional_str(payload.get("dedup_group_id")),
            is_duplicate=bool(payload.get("is_duplicate")),
            duplicate_of=_optional_str(payload.get("duplicate_of")),
        )

    classification = None
    if isinstance(payload.get("classification"), Mapping):
        final = _final_classification(payload)
        classification = RiskClassification(
            trace_id=trace_id,
            risk_category=str(final.get("risk_category") or "unknown"),
            secondary_label=str(final.get("secondary_label") or "待研判"),
            final_secondary_label=_optional_str(final.get("final_secondary_label") or final.get("secondary_label")),
            candidate_secondary_labels=[
                dict(item)
                for item in (final.get("candidate_secondary_labels") or [])
                if isinstance(item, Mapping)
            ],
            confidence=float(final.get("confidence") or 0.0),
            conflict_status=_optional_str(final.get("conflict_status")),
            evidence=[str(value) for value in (final.get("evidence") or [])],
            review_required=bool(final.get("review_required")),
            review_bucket=str(final.get("review_bucket") or "human_review_required"),
            classifier_version=str(final.get("classifier_version") or final.get("decision_version") or "unknown"),
        )
    resolution = (
        ClassificationResolution.model_validate(dict(payload["classification_resolution"]))
        if isinstance(payload.get("classification_resolution"), Mapping)
        else None
    )
    return _sync_item_payload(
        PipelineItem(
            record=IntelRecord(
                trace_id=trace_id,
                source_name=_optional_str(payload.get("source_name")),
                source_type=_optional_str(payload.get("source_type")),
                legal_basis=_optional_str(payload.get("legal_basis")),
                content_text=content_text,
                publish_time=_optional_str(payload.get("publish_time")),
                metadata={key: value for key, value in payload.items() if key not in {"content_text", "clean_text"}},
            ),
            cleaned=cleaned,
            classification=classification,
            classification_resolution=resolution,
            entities=_entities_from_payload(payload, payload.get("entities") or []),
            payload=payload,
            llm_enrichment=dict(payload.get("llm_enrichment")) if isinstance(payload.get("llm_enrichment"), Mapping) else None,
        )
    )


def _payload_from_item(item: Mapping[str, Any] | PipelineItem) -> dict[str, Any]:
    if isinstance(item, PipelineItem):
        return dict(item.payload)
    return dict(item)


def _item_with_payload(item: PipelineItem, payload: Mapping[str, Any]) -> PipelineItem:
    return item.model_copy(update={"payload": dict(payload)})


def _sync_item_payload(item: PipelineItem) -> PipelineItem:
    payload = dict(item.payload)
    payload.setdefault("trace_id", item.record.trace_id)
    payload.setdefault("source_trace_id", item.record.trace_id)
    payload.setdefault("content_text", item.record.content_text)
    if item.cleaned is not None:
        payload.update(
            {
                "clean_text": item.cleaned.clean_text,
                "normalized_text": item.cleaned.normalized_text,
                "quality_score": item.cleaned.quality_score,
                "noise_score": item.cleaned.noise_score,
                "dedup_group_id": item.cleaned.dedup_group_id,
                "is_duplicate": item.cleaned.is_duplicate,
                "duplicate_of": item.cleaned.duplicate_of,
            }
        )
    if item.classification_resolution is not None:
        resolution = item.classification_resolution.model_dump()
        final = dict(resolution.get("final") or {})
        payload["classification_resolution"] = resolution
        payload["classification"] = {
            "rule": dict(resolution.get("rule") or {}),
            "llm": dict(resolution.get("llm") or {}),
            "final": final,
            "resolution": resolution,
            # Legacy mirror fields remain only for JSON/CLI compatibility.
            **final,
        }
        payload["rule_classification"] = dict(resolution.get("rule") or {})
        if resolution.get("llm"):
            payload["llm_classification"] = dict(resolution.get("llm") or {})
        payload["risk_category"] = final.get("risk_category")
        payload["confidence"] = final.get("confidence")
        payload["has_conflict"] = final.get("conflict_status") == "CONFLICT_REVIEW"
    elif item.classification is not None:
        classification = item.classification.model_dump()
        payload["classification"] = classification
        payload["risk_category"] = classification.get("risk_category")
        payload["confidence"] = classification.get("confidence")
    if item.entities:
        payload["entities"] = [
            {
                "entity_id": entity.entity_id,
                "entity_type": entity.entity_type,
                "entity_value": entity.raw_value or entity.normalized_value,
                "raw_value": entity.raw_value,
                "normalized_value": entity.normalized_value,
                "masked_value": entity.masked_value,
                "source_trace_id": entity.trace_id,
                "confidence": entity.confidence,
                "sensitivity_level": entity.sensitivity_level,
                "extraction_method": entity.extraction_method,
            }
            for entity in item.entities
        ]
        payload["entity_count"] = len(item.entities)
    payload["domain_contract"] = item.model_copy(update={"payload": {}}).model_dump()
    return item.model_copy(update={"payload": payload})


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
    final_classification = _final_classification(item)
    resolution_payload = item.get("classification_resolution") if isinstance(item.get("classification_resolution"), Mapping) else None
    if resolution_payload is None:
        resolution = resolve_classification(
            item.get("rule_classification") if isinstance(item.get("rule_classification"), Mapping) else final_classification,
            item.get("llm_classification") if isinstance(item.get("llm_classification"), Mapping) else {},
            trace_id=trace_id,
        )
        resolution_payload = resolution.model_dump()
    contract["classification"] = RiskClassification(
        trace_id=trace_id,
        risk_category=str(final_classification.get("risk_category") or "unknown"),
        secondary_label=str(final_classification.get("secondary_label") or "待研判"),
        final_secondary_label=_optional_str(final_classification.get("final_secondary_label") or final_classification.get("secondary_label")),
        candidate_secondary_labels=[
            dict(item)
            for item in (final_classification.get("candidate_secondary_labels") or [])
            if isinstance(item, Mapping)
        ],
        confidence=float(final_classification.get("confidence") or 0.0),
        conflict_status=_optional_str(final_classification.get("conflict_status")),
        evidence=[str(value) for value in (final_classification.get("evidence") or [])],
        review_required=bool(final_classification.get("review_required")),
        review_bucket=str(final_classification.get("review_bucket") or "human_review_required"),
        classifier_version=str(final_classification.get("classifier_version") or final_classification.get("decision_version") or "unknown"),
    ).model_dump()
    contract["classification_resolution"] = ClassificationResolution.model_validate(dict(resolution_payload)).model_dump()
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


def _entities_from_payload(item: Mapping[str, Any], entities: Iterable[Mapping[str, Any]] | Any) -> list[ExtractedEntity]:
    if not isinstance(entities, Iterable) or isinstance(entities, (str, bytes, Mapping)):
        return []
    trace_id = str(item.get("trace_id") or item.get("source_trace_id") or "unknown")
    normalized_entities: list[ExtractedEntity] = []
    for index, entity in enumerate(entities):
        if not isinstance(entity, Mapping):
            continue
        value = str(entity.get("normalized_value") or entity.get("entity_value") or "")
        if not value:
            continue
        normalized_entities.append(
            ExtractedEntity(
                entity_id=str(entity.get("entity_id") or f"{trace_id}:{index}:{entity.get('entity_type') or 'entity'}"),
                trace_id=str(entity.get("source_trace_id") or entity.get("trace_id") or trace_id),
                entity_type=str(entity.get("entity_type") or "unknown"),
                raw_value=_optional_str(entity.get("entity_value") or entity.get("raw_value")),
                normalized_value=value,
                masked_value=_optional_str(entity.get("masked_value")),
                confidence=float(entity.get("confidence") or 0.0),
                sensitivity_level=str(entity.get("sensitivity_level") or "normal"),
                extraction_method=str(entity.get("extraction_method") or entity.get("extractor_version") or "unknown"),
            )
        )
    return normalized_entities


def _final_classification(item: Mapping[str, Any]) -> dict[str, Any]:
    classification = item.get("classification") if isinstance(item.get("classification"), Mapping) else {}
    nested_final = classification.get("final") if isinstance(classification.get("final"), Mapping) else None
    if nested_final is not None:
        return dict(nested_final)
    resolution = item.get("classification_resolution") if isinstance(item.get("classification_resolution"), Mapping) else {}
    final = resolution.get("final") if isinstance(resolution.get("final"), Mapping) else None
    if final is not None:
        return dict(final)
    return dict(classification)


__all__ = ["ClassifyStage", "CleanStage", "CorrelateStage", "DedupStage", "ExtractStage", "PassThroughStage", "ScoreStage", "Stage"]
