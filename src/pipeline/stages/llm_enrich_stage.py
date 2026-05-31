"""Budgeted LLM enrichment for routed classification/extraction records."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from src.backend import LLMGateway
from src.domain import ClassificationResolution, ExtractedEntity, IntelRecord, PipelineItem, RiskClassification
from src.pipeline.classification_resolution import resolve_classification
from src.safety import OutputValidator, PromptGuard
from src.safety.prompt_sanitizer import sanitize_entity_for_llm, stable_json_dumps


class LLMEnrichStage:
    """Run LLM classification/extraction only for ModelRouter-selected samples."""

    def __init__(
        self,
        *,
        llm_gateway: LLMGateway | Any,
        budget_controller: Any | None = None,
        prompt_guard: PromptGuard | None = None,
        output_validator: OutputValidator | None = None,
    ) -> None:
        self.llm_gateway = llm_gateway
        self.budget = budget_controller
        self.prompt_guard = prompt_guard or PromptGuard()
        self.output_validator = output_validator or OutputValidator()
        self.traces: list[dict[str, Any]] = []

    def run_batch(
        self,
        items: Iterable[Mapping[str, Any] | PipelineItem],
        *,
        routed: Iterable[Mapping[str, Any]],
        context: Mapping[str, Any] | None = None,
    ) -> list[PipelineItem]:
        context = dict(context or {})
        materialized = [_coerce_pipeline_item(item) for item in items]
        routes = [dict(route) for route in routed]
        self.traces = []
        output: list[PipelineItem] = []
        for item, route in zip(materialized, routes, strict=False):
            if str(route.get("action") or "") != "llm_classify_extract":
                output.append(item)
                continue
            max_tokens = _positive_int(route.get("max_tokens"), 700)
            messages = self._build_messages(item, context)
            budget_estimated_tokens = _estimate_tokens(messages) + max_tokens
            if not self._allow(estimated_tokens=budget_estimated_tokens):
                skipped = _item_with_payload(item, {**item.payload, "llm_enrich_skipped_reason": "budget_denied"})
                self._trace(skipped, route, llm_ok=False, used_fallback=True, error="budget_denied", parsed_json=None)
                output.append(skipped)
                continue

            budget_before = self._budget_counter()
            response = self.llm_gateway.chat(
                messages,
                temperature=0.0,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                stage="llm_classify",
                budget=self.budget,
                cache_policy="read_write",
                deadline_ms=_positive_int(route.get("deadline_ms"), 6000),
                extra_body={"budget_item_count": 1, "budget_estimated_tokens": budget_estimated_tokens},
            )
            self._consume_if_needed(estimated_tokens=budget_estimated_tokens, before=budget_before)
            parsed = response.parsed_json or {}
            enriched, used_fallback = self._merge_response(item, parsed)
            self._trace(enriched, route, llm_ok=response.ok, used_fallback=used_fallback, error=response.error, parsed_json=parsed if parsed else None)
            output.append(enriched)
        return output

    def _build_messages(self, item: Mapping[str, Any] | PipelineItem, context: Mapping[str, Any]) -> list[dict[str, str]]:
        record_card = _record_card(item)
        guarded = self.prompt_guard.wrap_untrusted_text(stable_json_dumps(record_card))
        return [
            {
                "role": "system",
                "content": (
                    "You are BlackAgent's defensive classification/extraction enhancer. "
                    "Return only JSON with optional fields enhanced_classification and enhanced_entities. "
                    "Do not invent facts; preserve deterministic rule outputs when evidence is weak."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"record_card={guarded}\n"
                    f"allowed_risk_types={context.get('allowed_risk_types') or []}\n"
                    f"quality_profile={context.get('quality_profile') or 'balanced'}"
                ),
            },
        ]

    def _allow(self, *, estimated_tokens: int) -> bool:
        if self.budget is None:
            return True
        if hasattr(self.budget, "peek"):
            return bool(self.budget.peek(stage="llm_classify", estimated_tokens=estimated_tokens, item_count=1))
        if hasattr(self.budget, "allow_llm_call"):
            return bool(self.budget.allow_llm_call(stage="llm_classify", estimated_tokens=estimated_tokens, item_count=1))
        return True

    def _budget_counter(self) -> tuple[int | None, int | None]:
        if self.budget is None or not hasattr(self.budget, "snapshot"):
            return None, None
        snapshot = self.budget.snapshot()
        return int(snapshot.get("llm_calls") or 0), int(snapshot.get("classified_by_llm") or 0)

    def _consume_if_needed(self, *, estimated_tokens: int, before: tuple[int | None, int | None]) -> None:
        if self.budget is None or not hasattr(self.budget, "consume_llm") or not hasattr(self.budget, "snapshot"):
            return
        before_calls, before_classified = before
        if before_calls is None or before_classified is None:
            return
        after = self.budget.snapshot()
        if int(after.get("llm_calls") or 0) > before_calls or int(after.get("classified_by_llm") or 0) > before_classified:
            return
        self.budget.consume_llm(stage="llm_classify", estimated_tokens=estimated_tokens, item_count=1)

    def _merge_response(self, item: Mapping[str, Any] | PipelineItem, parsed: Mapping[str, Any]) -> tuple[PipelineItem, bool]:
        current = _coerce_pipeline_item(item)
        payload = dict(current.payload)
        classification = dict(payload.get("classification") or {})
        if isinstance(classification.get("final"), Mapping):
            classification = dict(classification["final"])
        payload.setdefault("rule_classification", dict(classification))
        raw_classification = parsed.get("enhanced_classification")
        enhanced_classification = raw_classification if isinstance(raw_classification, Mapping) else {}
        usable_classification = bool(enhanced_classification.get("risk_category"))
        llm_classification = _normalized_classification(enhanced_classification, fallback=classification) if usable_classification else {}
        if usable_classification:
            payload["llm_classification"] = llm_classification
            resolution = resolve_classification(
                classification,
                llm_classification,
                trace_id=str(payload.get("source_trace_id") or payload.get("trace_id") or "unknown"),
            )
            final_classification = dict(resolution.final)
            payload["classification"] = {
                "rule": dict(classification),
                "llm": dict(llm_classification),
                "final": final_classification,
                "resolution": resolution.model_dump(),
                # Legacy mirror fields remain only for JSON/CLI compatibility.
                **final_classification,
            }
            payload["classification_resolution"] = resolution.model_dump()
            payload["enhanced_classification"] = final_classification
            payload["risk_category"] = final_classification.get("risk_category", payload.get("risk_category"))
            payload["confidence"] = final_classification.get("confidence", payload.get("confidence"))
        else:
            resolution = resolve_classification(
                classification,
                {},
                trace_id=str(payload.get("source_trace_id") or payload.get("trace_id") or "unknown"),
            )
            payload["classification_resolution"] = resolution.model_dump()

        raw_entities = parsed.get("enhanced_entities")
        normalized_entities = _normalized_entities(raw_entities, trace_id=str(payload.get("source_trace_id") or payload.get("trace_id") or "unknown"))
        payload.setdefault("rule_entities", [dict(entity) for entity in (payload.get("entities") or []) if isinstance(entity, Mapping)])
        if normalized_entities:
            existing = [dict(entity) for entity in (payload.get("entities") or []) if isinstance(entity, Mapping)]
            payload["entities"] = _merge_entities(existing, normalized_entities)
            payload["llm_entities"] = normalized_entities
            payload["enhanced_entities"] = normalized_entities
            payload["entity_count"] = len(payload["entities"])
            entity_types = {str(entity.get("entity_type") or "").lower() for entity in payload["entities"]}
            payload["has_contact"] = bool(entity_types.intersection({"contact", "account"}))
            payload["has_url"] = bool(entity_types.intersection({"url", "domain"}))
            payload["has_tool"] = "tool_name" in entity_types

        payload["llm_enrichment"] = {
            "llm_ok": bool(parsed),
            "used_enhanced_classification": usable_classification,
            "used_enhanced_entities": bool(normalized_entities),
            "preserved_rule_classification": "rule_classification" in payload,
            "preserved_rule_entities": "rule_entities" in payload,
            "classification_resolution": dict(payload.get("classification_resolution") or {}),
        }
        typed_resolution = ClassificationResolution.model_validate(dict(payload.get("classification_resolution") or {}))
        final = dict(typed_resolution.final)
        typed_entities = _entities_from_payload(payload, payload.get("entities") or [])
        current = current.model_copy(
            update={
                "classification": RiskClassification(
                    trace_id=str(payload.get("source_trace_id") or payload.get("trace_id") or current.record.trace_id),
                    risk_category=str(final.get("risk_category") or "unknown"),
                    secondary_label=str(final.get("secondary_label") or "待研判"),
                    confidence=float(final.get("confidence") or 0.0),
                    conflict_status=_optional_str(final.get("conflict_status")),
                    evidence=[str(value) for value in (final.get("evidence") or [])],
                    review_required=bool(final.get("review_required")),
                    classifier_version=str(final.get("classifier_version") or "unknown"),
                ),
                "classification_resolution": typed_resolution,
                "entities": typed_entities,
                "llm_enrichment": dict(payload.get("llm_enrichment") or {}),
            }
        )
        return _sync_item_payload(_item_with_payload(current, payload)), not (usable_classification or normalized_entities)

    def _trace(
        self,
        item: Mapping[str, Any] | PipelineItem,
        route: Mapping[str, Any],
        *,
        llm_ok: bool,
        used_fallback: bool,
        error: str | None,
        parsed_json: Mapping[str, Any] | None,
    ) -> None:
        self.traces.append(
            {
                "stage": "llm_classify_extract",
                "source_trace_id": str(_payload_from_item(item).get("source_trace_id") or _payload_from_item(item).get("trace_id") or "unknown"),
                "route_reason": route.get("reason"),
                "llm_ok": llm_ok,
                "used_fallback": used_fallback,
                "parsed_json": dict(parsed_json) if isinstance(parsed_json, Mapping) else None,
                "error": error,
            }
        )


def _record_card(item: Mapping[str, Any] | PipelineItem) -> dict[str, Any]:
    payload = _payload_from_item(item)
    text = str(payload.get("clean_text") or payload.get("content_text") or "")
    entities = [sanitize_entity_for_llm(entity) for entity in (payload.get("entities") or []) if isinstance(entity, Mapping)]
    classification = payload.get("classification") if isinstance(payload.get("classification"), Mapping) else {}
    final_classification = classification.get("final") if isinstance(classification.get("final"), Mapping) else classification
    return {
        "trace_id": str(payload.get("source_trace_id") or payload.get("trace_id") or "unknown"),
        "source_type": payload.get("source_type"),
        "risk_score": payload.get("risk_score"),
        "quality_score": payload.get("quality_score"),
        "classification": {
            key: final_classification.get(key)
            for key in ("risk_category", "secondary_label", "confidence", "review_required", "conflict_status", "evidence")
            if final_classification.get(key) not in (None, "")
        },
        "entities": entities[:12],
        "text_excerpt": text[:700],
    }


def _normalized_classification(payload: Mapping[str, Any], *, fallback: Mapping[str, Any]) -> dict[str, Any]:
    confidence = _float(payload.get("confidence"), _float(fallback.get("confidence"), 0.0))
    return {
        "risk_category": str(payload.get("risk_category") or fallback.get("risk_category") or "unknown"),
        "secondary_label": str(payload.get("secondary_label") or fallback.get("secondary_label") or "待研判"),
        "confidence": round(max(0.0, min(confidence, 0.99)), 4),
        "review_required": _bool(payload.get("review_required"), default=bool(fallback.get("review_required", True))),
        "evidence": [str(item) for item in payload.get("evidence", []) if str(item).strip()] if isinstance(payload.get("evidence"), list) else list(fallback.get("evidence") or []),
        "classifier_version": "llm_enrich_v1",
    }


def _normalized_entities(value: Any, *, trace_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    entities: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        entity_type = str(raw.get("entity_type") or raw.get("type") or "").strip()
        entity_value = str(raw.get("normalized_value") or raw.get("entity_value") or raw.get("value") or "").strip()
        if not entity_type or not entity_value:
            continue
        entities.append(
            {
                "entity_type": entity_type,
                "entity_value": entity_value,
                "normalized_value": entity_value,
                "start_offset": int(raw.get("start_offset") or 0),
                "end_offset": max(int(raw.get("end_offset") or 0), int(raw.get("start_offset") or 0) + len(entity_value)),
                "source_trace_id": str(raw.get("source_trace_id") or trace_id),
                "confidence": round(max(0.0, min(_float(raw.get("confidence"), 0.75), 0.99)), 4),
                "extraction_method": "llm_enrich_v1",
            }
        )
    return entities


def _merge_entities(existing: list[dict[str, Any]], enhanced: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entity in [*existing, *enhanced]:
        key = (
            str(entity.get("source_trace_id") or ""),
            str(entity.get("entity_type") or "").lower(),
            str(entity.get("normalized_value") or entity.get("entity_value") or "").lower(),
        )
        if not key[1] or not key[2] or key in seen:
            continue
        seen.add(key)
        merged.append(dict(entity))
    return merged


def _coerce_pipeline_item(item: Mapping[str, Any] | PipelineItem) -> PipelineItem:
    if isinstance(item, PipelineItem):
        return _sync_item_payload(item)
    payload = dict(item)
    contract = payload.get("domain_contract") if isinstance(payload.get("domain_contract"), Mapping) else None
    if contract:
        loaded = PipelineItem.model_validate(dict(contract))
        return _sync_item_payload(loaded.model_copy(update={"payload": {**loaded.payload, **payload}}))
    trace_id = str(payload.get("trace_id") or payload.get("source_trace_id") or "unknown")
    content_text = str(payload.get("content_text") or payload.get("clean_text") or trace_id)
    resolution = (
        ClassificationResolution.model_validate(dict(payload["classification_resolution"]))
        if isinstance(payload.get("classification_resolution"), Mapping)
        else None
    )
    classification = None
    if isinstance(payload.get("classification"), Mapping):
        final = dict(resolution.final) if resolution is not None else dict(payload.get("classification") or {})
        if isinstance(final.get("final"), Mapping):
            final = dict(final["final"])
        classification = RiskClassification(
            trace_id=trace_id,
            risk_category=str(final.get("risk_category") or "unknown"),
            secondary_label=str(final.get("secondary_label") or "待研判"),
            confidence=float(final.get("confidence") or 0.0),
            conflict_status=_optional_str(final.get("conflict_status")),
            evidence=[str(value) for value in (final.get("evidence") or [])],
            review_required=bool(final.get("review_required")),
            classifier_version=str(final.get("classifier_version") or "unknown"),
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
            ),
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
    if item.classification_resolution is not None:
        resolution = item.classification_resolution.model_dump()
        final = dict(resolution.get("final") or {})
        payload["classification_resolution"] = resolution
        payload["classification"] = {
            "rule": dict(resolution.get("rule") or {}),
            "llm": dict(resolution.get("llm") or {}),
            "final": final,
            "resolution": resolution,
            **final,
        }
        payload["rule_classification"] = dict(resolution.get("rule") or {})
        if resolution.get("llm"):
            payload["llm_classification"] = dict(resolution.get("llm") or {})
    elif item.classification is not None:
        payload["classification"] = item.classification.model_dump()
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


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _estimate_tokens(messages: list[dict[str, str]]) -> int:
    text = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    return max(1, len(text) // 4)


__all__ = ["LLMEnrichStage"]
