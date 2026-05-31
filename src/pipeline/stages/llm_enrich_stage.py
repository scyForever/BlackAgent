"""Budgeted LLM enrichment for routed classification/extraction records."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from src.backend import LLMGateway
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
        items: Iterable[Mapping[str, Any]],
        *,
        routed: Iterable[Mapping[str, Any]],
        context: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        context = dict(context or {})
        materialized = [dict(item) for item in items]
        routes = [dict(route) for route in routed]
        self.traces = []
        output: list[dict[str, Any]] = []
        for item, route in zip(materialized, routes, strict=False):
            if str(route.get("action") or "") != "llm_classify_extract":
                output.append(item)
                continue
            max_tokens = _positive_int(route.get("max_tokens"), 700)
            messages = self._build_messages(item, context)
            budget_estimated_tokens = _estimate_tokens(messages) + max_tokens
            if not self._allow(estimated_tokens=budget_estimated_tokens):
                skipped = dict(item)
                skipped["llm_enrich_skipped_reason"] = "budget_denied"
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

    def _build_messages(self, item: Mapping[str, Any], context: Mapping[str, Any]) -> list[dict[str, str]]:
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

    def _merge_response(self, item: Mapping[str, Any], parsed: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        payload = dict(item)
        classification = dict(payload.get("classification") or {})
        payload.setdefault("rule_classification", dict(classification))
        raw_classification = parsed.get("enhanced_classification")
        enhanced_classification = raw_classification if isinstance(raw_classification, Mapping) else {}
        usable_classification = bool(enhanced_classification.get("risk_category"))
        if usable_classification:
            merged_classification = {**classification, **_normalized_classification(enhanced_classification, fallback=classification)}
            payload["llm_classification"] = _normalized_classification(enhanced_classification, fallback=classification)
            payload["classification"] = merged_classification
            payload["enhanced_classification"] = merged_classification
            payload["risk_category"] = merged_classification.get("risk_category", payload.get("risk_category"))
            payload["confidence"] = merged_classification.get("confidence", payload.get("confidence"))

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
            "classification_resolution": {
                "rule": dict(payload.get("rule_classification") or classification),
                "llm": dict(payload.get("llm_classification") or {}),
                "final": dict(payload.get("classification") or classification),
                "strategy": "prefer_llm_when_structured_response_present" if usable_classification else "preserve_rule_when_llm_low_evidence",
                "reason": "llm_enrichment_structured_json" if usable_classification else "no_usable_llm_classification",
            },
        }
        return payload, not (usable_classification or normalized_entities)

    def _trace(
        self,
        item: Mapping[str, Any],
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
                "source_trace_id": str(item.get("source_trace_id") or item.get("trace_id") or "unknown"),
                "route_reason": route.get("reason"),
                "llm_ok": llm_ok,
                "used_fallback": used_fallback,
                "parsed_json": dict(parsed_json) if isinstance(parsed_json, Mapping) else None,
                "error": error,
            }
        )


def _record_card(item: Mapping[str, Any]) -> dict[str, Any]:
    text = str(item.get("clean_text") or item.get("content_text") or "")
    entities = [sanitize_entity_for_llm(entity) for entity in (item.get("entities") or []) if isinstance(entity, Mapping)]
    classification = item.get("classification") if isinstance(item.get("classification"), Mapping) else {}
    return {
        "trace_id": str(item.get("source_trace_id") or item.get("trace_id") or "unknown"),
        "source_type": item.get("source_type"),
        "risk_score": item.get("risk_score"),
        "quality_score": item.get("quality_score"),
        "classification": {
            key: classification.get(key)
            for key in ("risk_category", "secondary_label", "confidence", "review_required", "conflict_status", "evidence")
            if classification.get(key) not in (None, "")
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
