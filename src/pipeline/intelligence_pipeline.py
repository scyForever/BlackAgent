"""Composable intelligence pipeline wrapper over real deterministic stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.agent.model_router import ModelRouter
from src.domain import (
    ActionableClue,
    ArchivedWeakClue,
    CandidateClue,
    CleanedRecord,
    ClassificationResolution,
    ExtractedEntity,
    IntelRecord,
    PipelineItem,
    PipelineExecutionSummary,
    PipelineLegacySnapshot,
    RiskClassification,
    RoutedRecord,
    RunPolicyContext,
)
from src.pipeline.stages import (
    ClassifyStage,
    CleanStage,
    CluePromotionStage,
    CorrelateStage,
    DedupStage,
    ExtractStage,
    LLMEnrichStage,
    PassThroughStage,
    ScoreStage,
)
from src.rules import RuleRegistry
from src.evaluation.llm_ablation import load_latest_llm_value_report


@dataclass
class PipelineResult:
    items: list[PipelineItem] = field(default_factory=list)
    candidate_clues: list[CandidateClue] = field(default_factory=list)
    actionable_clues: list[ActionableClue] = field(default_factory=list)
    archived_weak_clues: list[ArchivedWeakClue] = field(default_factory=list)
    execution_summary: PipelineExecutionSummary = field(default_factory=PipelineExecutionSummary)
    legacy_snapshot: PipelineLegacySnapshot = field(default_factory=PipelineLegacySnapshot)

    def model_dump(self) -> dict[str, Any]:
        return {
            "items": [item.model_dump() for item in self.items],
            "candidate_clues": [item.model_dump() for item in self.candidate_clues],
            "actionable_clues": [item.model_dump() for item in self.actionable_clues],
            "archived_weak_clues": [item.model_dump() for item in self.archived_weak_clues],
            "execution_summary": self.execution_summary.model_dump(),
            "legacy_snapshot": self.legacy_snapshot.model_dump(),
        }

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the dict-compatible shape expected by existing CLI/tests."""

        payload = self.legacy_snapshot.model_dump()
        payload["items"] = [item.model_dump() for item in self.items]
        payload["candidate_clues"] = [item.model_dump() for item in self.candidate_clues]
        payload["actionable_clues"] = [item.model_dump() for item in self.actionable_clues]
        payload["archived_weak_clues"] = [item.model_dump() for item in self.archived_weak_clues]
        payload["typed_execution_summary"] = self.execution_summary.model_dump()
        return payload

    @property
    def cleaned(self) -> list[dict[str, Any]]:
        return self.legacy_snapshot.cleaned

    @property
    def classified(self) -> list[dict[str, Any]]:
        return self.legacy_snapshot.classified

    @property
    def entities(self) -> list[dict[str, Any]]:
        return self.legacy_snapshot.entities

    @property
    def routed(self) -> list[dict[str, Any]]:
        return self.legacy_snapshot.routed

    @property
    def enriched(self) -> list[dict[str, Any]]:
        return self.legacy_snapshot.enriched

    @property
    def clues(self) -> list[dict[str, Any]]:
        return self.legacy_snapshot.clues


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
        llm_enrich_stage: Any | None = None,
        correlate_stage: Any | None = None,
        score_stage: Any | None = None,
        clue_promotion_stage: Any | None = None,
        model_router: Any | None = None,
        llm_gateway: Any | None = None,
        budget_controller: Any | None = None,
        policy: RunPolicyContext | Mapping[str, Any] | None = None,
        load_runtime_llm_value: bool = True,
    ) -> None:
        self.policy = _coerce_policy(policy)
        self.clean_stage = clean_stage or CleanStage()
        self.rule_registry = RuleRegistry()
        self.dedup_stage = dedup_stage or DedupStage()
        self.triage_stage = triage_stage or PassThroughStage()
        self.classify_stage = classify_stage or ClassifyStage()
        self.extract_stage = extract_stage or ExtractStage()
        self.llm_enrich_stage = llm_enrich_stage
        if self.llm_enrich_stage is None and llm_gateway is not None:
            self.llm_enrich_stage = LLMEnrichStage(llm_gateway=llm_gateway, budget_controller=budget_controller)
        self.correlate_stage = correlate_stage or CorrelateStage()
        self.clue_promotion_stage = clue_promotion_stage or CluePromotionStage()
        self.score_stage = score_stage or ScoreStage()
        self.model_router = model_router or ModelRouter(profile=self.policy.routing_profile)
        self.load_runtime_llm_value = bool(load_runtime_llm_value)
        self.llm_value_metrics = load_latest_llm_value_report() if load_runtime_llm_value else None
        self.model_router = _apply_runtime_llm_value_policy(
            self.model_router,
            metrics=self.llm_value_metrics,
            profile=self.policy.routing_profile,
            enabled=self.load_runtime_llm_value,
        )

    def run(self, raw_items: Iterable[Mapping[str, Any]], context: Mapping[str, Any] | None = None) -> PipelineResult:
        context = dict(context or {})
        policy = _coerce_policy(context.get("policy") or self.policy)
        self.policy = policy
        if hasattr(self.model_router, "with_profile"):
            self.model_router = self.model_router.with_profile(policy.routing_profile)
        if not policy.enable_llm_record_enrich and hasattr(self.model_router, "with_record_enrich_policy"):
            self.model_router = self.model_router.with_record_enrich_policy(
                enabled=False,
                reason="policy_disabled_record_enrich",
                profile=policy.routing_profile,
                policy="disabled",
            )
        else:
            self.model_router = _apply_runtime_llm_value_policy(
                self.model_router,
                metrics=self.llm_value_metrics,
                profile=policy.routing_profile,
                enabled=self.load_runtime_llm_value,
            )
        context["policy"] = policy.model_dump()
        context["routing_profile"] = policy.routing_profile
        context["llm_stage_policy"] = dict(policy.llm_stage_policy)
        materialized_raw = [dict(item) for item in raw_items]
        items = [_initial_pipeline_item(item) for item in materialized_raw]
        cleaned_items = [_coerce_pipeline_item(item) for item in self.clean_stage.run_batch(items, context=context)]
        cleaned = [_payload_from_item(item) for item in cleaned_items]
        deduped_items = [_coerce_pipeline_item(item) for item in self.dedup_stage.run_batch(cleaned_items, context=context)]
        triaged_items = [_coerce_pipeline_item(item) for item in self.triage_stage.run_batch(deduped_items, context=context)]
        classified_items = [_coerce_pipeline_item(item) for item in self.classify_stage.run_batch(triaged_items, context=context)]
        classified_payloads = [_payload_from_item(item) for item in classified_items]
        extracted_items = [_coerce_pipeline_item(item) for item in self.extract_stage.run_batch(classified_items, context=context)]
        extracted_payloads = [_payload_from_item(item) for item in extracted_items]
        routed = [self._route_item(item) for item in extracted_items]
        enriched_items = self._run_llm_enrichment(extracted_items, routed=routed, context=context)
        enriched = [_payload_from_item(item) for item in enriched_items]
        classifications = [_final_classification_from_item(item) for item in enriched_items if _classification_available(item)]
        entities = [_legacy_entity_payload(entity) for item in enriched_items for entity in item.entities]
        stage_context = {**context, "classifications": classifications, "entities": entities}
        correlated = self.correlate_stage.run_batch(enriched_items, routed=routed, context=stage_context)
        promoted = self.clue_promotion_stage.run_batch(correlated, context=stage_context)
        scored = self.score_stage.run_batch(promoted, context=stage_context)
        typed_items = [_coerce_pipeline_item(item, route=route) for item, route in zip(enriched_items, routed, strict=False)]
        entity_graph_summary = {}
        graph_store = getattr(self.correlate_stage, "entity_graph", None)
        if graph_store is not None and hasattr(graph_store, "snapshot"):
            graph_snapshot = graph_store.snapshot()
            entity_graph_summary = {
                key: graph_snapshot.get(key)
                for key in ("entity_count", "observation_count", "relation_count", "cross_source_entity_count")
            }
        max_candidate_clues = _positive_int(policy.budget.get("max_candidate_clues") if isinstance(policy.budget, Mapping) else None)
        final_clues = [dict(item) for item in scored]
        candidate_clue_payloads = [dict(item) for item in getattr(self.clue_promotion_stage, "candidate_clues", correlated)]
        archived_weak_payloads = [dict(item) for item in getattr(self.clue_promotion_stage, "archived_weak_clues", [])]
        if max_candidate_clues is not None:
            final_clues = _cap_clues_by_type(final_clues, max_candidate_clues)
        typed_candidates = [_candidate_clue_from_payload(item) for item in candidate_clue_payloads]
        typed_actionable = [_actionable_clue_from_payload(item) for item in final_clues]
        typed_archived = [_archived_clue_from_payload(item) for item in archived_weak_payloads]
        execution_summary = {
            "status": "completed",
            "input_count": len(materialized_raw),
            "cleaned_count": len(cleaned),
            "classified_count": len(classifications),
            "entity_count": len(entities),
            "candidate_clue_count": len(typed_candidates),
            "actionable_clue_count": len(final_clues),
            "archived_weak_clue_count": len(typed_archived),
            "clue_count": len(final_clues),
            "clue_layering": {
                "candidate_clues": len(typed_candidates),
                "actionable_clues": len(final_clues),
                "archived_weak_clues": len(typed_archived),
            },
            "llm_enrich_count": sum(1 for item in enriched if item.get("llm_enrichment")),
            "llm_enrich_skipped_count": sum(1 for item in enriched if item.get("llm_enrich_skipped_reason")),
            "llm_enrich_trace_count": len(getattr(self.llm_enrich_stage, "traces", []) or []),
            "llm_call_traces": list(getattr(self.llm_enrich_stage, "call_traces", []) or []),
            "llm_item_traces": list(getattr(self.llm_enrich_stage, "item_traces", []) or []),
            "stage_mode": "real_components",
            "pipeline_backend": "intelligence_pipeline",
            "routing_profile": policy.routing_profile,
            "model_router_profile": getattr(self.model_router, "profile", None),
            "llm_stage_policy": dict(policy.llm_stage_policy),
                "llm_value_gate": {
                    "metrics_loaded": bool(self.llm_value_metrics),
                    "metrics_applied": _llm_value_applies(self.llm_value_metrics, policy.routing_profile),
                    "record_enrich_enabled": getattr(self.model_router, "record_enrich_enabled", None),
                    "record_enrich_policy": _record_enrich_policy(policy, self.model_router),
                "reason": getattr(self.model_router, "value_gate_reason", "")
                or (self.llm_value_metrics or {}).get("gate_reason"),
                "profile": (self.llm_value_metrics or {}).get("profile"),
            },
        "domain_contract_version": "pipeline_item_v1",
            "pipeline_data_plane": "typed_first_pipeline_item_internal_legacy_snapshot_adapter",
            "entity_graph": entity_graph_summary,
            "rule_version": self.rule_registry.version_hash(),
        }
        typed_summary = PipelineExecutionSummary(
            status="completed",
            input_count=len(materialized_raw),
            cleaned_count=len(cleaned),
            classified_count=len(classifications),
            entity_count=len(entities),
            candidate_clue_count=len(typed_candidates),
            actionable_clue_count=len(final_clues),
            archived_weak_clue_count=len(typed_archived),
            rule_version=self.rule_registry.version_hash(),
            extra={key: value for key, value in execution_summary.items() if key not in PipelineExecutionSummary.model_fields},
        )
        legacy_snapshot = PipelineLegacySnapshot(
            cleaned=[dict(item) for item in cleaned],
            classified=classifications,
            entities=entities,
            routed=routed,
            enriched=[dict(item) for item in enriched],
            clues=final_clues,
            execution_summary=execution_summary,
        )
        return PipelineResult(
            items=typed_items,
            candidate_clues=typed_candidates,
            actionable_clues=typed_actionable,
            archived_weak_clues=typed_archived,
            execution_summary=typed_summary,
            legacy_snapshot=legacy_snapshot,
        )

    def _route_item(self, item: Mapping[str, Any] | PipelineItem) -> dict[str, Any]:
        payload = _payload_from_item(item)
        classification = _final_classification_from_item(item) if isinstance(item, PipelineItem) else _final_classification(payload)
        entities = item.entities if isinstance(item, PipelineItem) else _entities_from_payload(payload, payload.get("entities") or [])
        entity_types = {entity.entity_type.lower() for entity in entities}
        decision = self.model_router.decide_record(
            rule_confidence=float(classification.get("confidence") or payload.get("rule_confidence") or payload.get("confidence") or 0.0),
            risk_score=float(payload.get("risk_score") or 0.0),
            entity_count=len(entities) or int(payload.get("entity_count") or 0),
            has_contact=bool(entity_types.intersection({"contact", "account"}) or payload.get("has_contact")),
            has_url=bool(entity_types.intersection({"url", "domain"}) or payload.get("has_url")),
            has_tool=bool("tool_name" in entity_types or payload.get("has_tool")),
            has_conflict=bool(
                classification.get("conflict_status") == "CONFLICT_REVIEW"
                or classification.get("review_required")
                or payload.get("has_conflict")
            ),
            is_duplicate=bool((item.cleaned.is_duplicate if isinstance(item, PipelineItem) and item.cleaned is not None else False) or payload.get("is_duplicate") or payload.get("duplicate_of")),
            quality_score=float((item.cleaned.quality_score if isinstance(item, PipelineItem) and item.cleaned is not None else 0.0) or payload.get("quality_score") or 0.0),
        )
        return decision.model_dump()

    def _run_llm_enrichment(
        self,
        items: list[PipelineItem],
        *,
        routed: list[dict[str, Any]],
        context: Mapping[str, Any],
    ) -> list[PipelineItem]:
        policy = _coerce_policy(context.get("policy") or self.policy)
        if self.llm_enrich_stage is None or not policy.enable_llm_record_enrich:
            reason = "policy_disabled_record_enrich" if not policy.enable_llm_record_enrich else None
            output = [_coerce_pipeline_item(item) for item in items]
            if reason:
                for index, (item, route) in enumerate(zip(output, routed, strict=False)):
                    if str(route.get("action") or "") == "llm_classify_extract":
                        payload = {**item.payload, "llm_enrich_skipped_reason": reason}
                        output[index] = item.model_copy(update={"payload": payload})
            return output
        return [_coerce_pipeline_item(item) for item in self.llm_enrich_stage.run_batch(
            items,
            routed=routed,
            context={
                **dict(context),
                "allowed_risk_types": [
                    str(_final_classification_from_item(item).get("risk_category") or "")
                    for item in items
                    if _final_classification_from_item(item).get("risk_category")
                ],
            },
        )]


def _coerce_policy(value: RunPolicyContext | Mapping[str, Any] | None) -> RunPolicyContext:
    if isinstance(value, RunPolicyContext):
        return value
    if isinstance(value, Mapping):
        return RunPolicyContext.model_validate(dict(value))
    return RunPolicyContext()


def _llm_value_applies(metrics: Mapping[str, Any] | None, profile: str) -> bool:
    if not metrics:
        return False
    metric_profile = str(metrics.get("profile") or "").strip().lower()
    if not metric_profile:
        return True
    return metric_profile == str(profile or "").strip().lower()


def _apply_runtime_llm_value_policy(
    router: Any,
    *,
    metrics: Mapping[str, Any] | None,
    profile: str,
    enabled: bool,
) -> Any:
    if not enabled:
        return router
    from src.evaluation.llm_ablation import LLMValueGate

    if _llm_value_applies(metrics, profile) and hasattr(router, "with_llm_value_metrics"):
        return router.with_llm_value_metrics(metrics, profile=profile)
    if not hasattr(router, "with_record_enrich_policy"):
        return router
    normalized = str(profile or "").strip().lower()
    policy = LLMValueGate().record_enrich_policy(normalized, None)
    reason = (
        "llm_value_gate_fast_profile_record_enrich_disabled"
        if normalized == "fast"
        else "llm_value_report_missing_for_profile_hard_cases_only"
        if metrics
        else "llm_value_report_missing_hard_cases_only"
    )
    return router.with_record_enrich_policy(enabled=False, reason=reason, profile=profile, policy=policy)


def _record_enrich_policy(policy: RunPolicyContext, router: Any) -> str:
    if not policy.enable_llm_record_enrich:
        return "disabled"
    return str(getattr(router, "record_enrich_policy", None) or ("enabled" if bool(getattr(router, "record_enrich_enabled", True)) else "hard_cases_only"))


def _initial_pipeline_item(payload: Mapping[str, Any]) -> PipelineItem:
    trace_id = str(payload.get("trace_id") or payload.get("source_trace_id") or payload.get("hash_id") or "unknown")
    content_text = str(payload.get("content_text") or payload.get("clean_text") or trace_id)
    return PipelineItem(
        record=IntelRecord(
            trace_id=trace_id,
            source_name=_optional_str(payload.get("source_name")),
            source_type=_optional_str(payload.get("source_type")),
            legal_basis=_optional_str(payload.get("legal_basis")),
            content_text=content_text,
            publish_time=_optional_str(payload.get("publish_time")),
            metadata={key: value for key, value in payload.items() if key not in {"content_text", "clean_text"}},
        ),
        payload={**dict(payload), "trace_id": trace_id, "source_trace_id": str(payload.get("source_trace_id") or trace_id)},
    )


def _coerce_pipeline_item(item: PipelineItem | Mapping[str, Any], *, route: Mapping[str, Any] | None = None) -> PipelineItem:
    if isinstance(item, PipelineItem):
        typed_route = _routed_record_from_payload(item.payload, route)
        if typed_route is not None:
            return item.model_copy(update={"route": typed_route})
        return item
    return _pipeline_item_from_payload(item, route=route)


def _payload_from_item(item: PipelineItem | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, PipelineItem):
        return dict(item.payload)
    return dict(item)


def _pipeline_item_from_payload(payload: Mapping[str, Any], *, route: Mapping[str, Any] | None = None) -> PipelineItem:
    contract = payload.get("domain_contract") if isinstance(payload.get("domain_contract"), Mapping) else None
    typed_route = _routed_record_from_payload(payload, route)
    if contract:
        item = PipelineItem.model_validate(dict(contract))
        updates = {}
        if payload.get("llm_enrichment") is not None and item.llm_enrichment is None:
            updates["llm_enrichment"] = dict(payload.get("llm_enrichment") or {})
        if payload.get("classification_resolution") is not None and item.classification_resolution is None:
            updates["classification_resolution"] = ClassificationResolution.model_validate(dict(payload.get("classification_resolution") or {}))
        if updates:
            item = item.model_copy(update=updates)
        if typed_route is not None:
            item = item.model_copy(update={"route": typed_route})
        return item
    content_text = str(payload.get("content_text") or payload.get("clean_text") or payload.get("trace_id") or payload.get("source_trace_id") or "compat_payload")
    return PipelineItem.model_validate(
        {
            "record": {
                "trace_id": str(payload.get("trace_id") or payload.get("source_trace_id") or "unknown"),
                "content_text": content_text,
                "source_name": payload.get("source_name"),
                "source_type": payload.get("source_type"),
                "legal_basis": payload.get("legal_basis"),
                "publish_time": payload.get("publish_time"),
            },
            "payload": dict(payload),
            "route": typed_route.model_dump() if typed_route is not None else None,
            "llm_enrichment": payload.get("llm_enrichment") if isinstance(payload.get("llm_enrichment"), Mapping) else None,
            "classification_resolution": payload.get("classification_resolution") if isinstance(payload.get("classification_resolution"), Mapping) else None,
            "classification": _risk_classification_from_payload(payload),
            "entities": _entities_from_payload(payload, payload.get("entities") or []),
        }
    )


def _routed_record_from_payload(payload: Mapping[str, Any], route: Mapping[str, Any] | None) -> RoutedRecord | None:
    if not isinstance(route, Mapping):
        return None
    action = str(route.get("action") or "").strip()
    if not action:
        return None
    return RoutedRecord(
        trace_id=str(payload.get("trace_id") or payload.get("source_trace_id") or "unknown"),
        route_action=action,
        route_reason=str(route.get("reason") or "unspecified"),
        max_tokens=max(0, int(route.get("max_tokens") or 0)),
        deadline_ms=max(0, int(route.get("deadline_ms") or 0)),
        requires_review=bool(route.get("requires_review")),
    )


def _final_classification(payload: Mapping[str, Any]) -> dict[str, Any]:
    classification = payload.get("classification") if isinstance(payload.get("classification"), Mapping) else {}
    final = classification.get("final") if isinstance(classification.get("final"), Mapping) else None
    if final is not None:
        return dict(final)
    resolution = payload.get("classification_resolution") if isinstance(payload.get("classification_resolution"), Mapping) else {}
    resolved_final = resolution.get("final") if isinstance(resolution.get("final"), Mapping) else None
    if resolved_final is not None:
        return dict(resolved_final)
    return dict(classification)


def _final_classification_from_item(item: PipelineItem | Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, PipelineItem):
        return _final_classification(dict(item))
    if item.classification_resolution is not None:
        return dict(item.classification_resolution.final)
    if item.classification is not None:
        return item.classification.model_dump()
    return _final_classification(item.payload)


def _classification_available(item: PipelineItem) -> bool:
    final = _final_classification_from_item(item)
    return bool(str(final.get("risk_category") or "").strip())


def _candidate_clue_from_payload(payload: Mapping[str, Any]) -> CandidateClue:
    return CandidateClue(
        clue_id=str(payload.get("clue_id") or f"candidate:{payload.get('clue_type') or 'unknown'}:{payload.get('key') or 'unknown'}"),
        clue_type=str(payload.get("clue_type") or "unknown"),
        key=str(payload.get("key") or payload.get("entity_asset_id") or payload.get("clue_id") or "unknown"),
        risk_category=str(payload.get("risk_category") or "unknown"),
        evidence_trace_ids=_string_list(payload.get("evidence_trace_ids")),
        related_entity_ids=_string_list(payload.get("related_entity_ids")),
        source_names=_string_list(payload.get("source_names")),
        entity_values=_string_list(payload.get("entity_values")),
        confidence=_bounded_float(payload.get("confidence"), 0.0),
        weak_reason=str(payload.get("weak_reason") or payload.get("threshold_reason") or ""),
    )


def _actionable_clue_from_payload(payload: Mapping[str, Any]) -> ActionableClue:
    candidate = _candidate_clue_from_payload(payload).model_dump()
    return ActionableClue(
        **candidate,
        promotion_reason=str(payload.get("promotion_reason") or ""),
        actionability_score=_bounded_float(payload.get("actionability_score") or payload.get("quality_score"), 0.0),
    )


def _archived_clue_from_payload(payload: Mapping[str, Any]) -> ArchivedWeakClue:
    candidate = _candidate_clue_from_payload(payload).model_dump()
    return ArchivedWeakClue(
        **candidate,
        archive_reason=str(payload.get("archive_reason") or ""),
    )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _risk_classification_from_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload.get("classification"), Mapping) and not isinstance(payload.get("classification_resolution"), Mapping):
        return None
    final = _final_classification(payload)
    return RiskClassification(
        trace_id=str(payload.get("trace_id") or payload.get("source_trace_id") or "unknown"),
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
        classifier_version=str(final.get("classifier_version") or final.get("decision_version") or "unknown"),
    ).model_dump()


def _entities_from_payload(payload: Mapping[str, Any], entities: Iterable[Mapping[str, Any]] | Any) -> list[dict[str, Any]]:
    if not isinstance(entities, Iterable) or isinstance(entities, (str, bytes, Mapping)):
        return []
    trace_id = str(payload.get("trace_id") or payload.get("source_trace_id") or "unknown")
    normalized_entities: list[dict[str, Any]] = []
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
            ).model_dump()
        )
    return normalized_entities


def _legacy_entity_payload(entity: ExtractedEntity) -> dict[str, Any]:
    payload = entity.model_dump()
    payload.setdefault("source_trace_id", entity.trace_id)
    payload.setdefault("entity_value", entity.raw_value or entity.normalized_value)
    return payload


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [str(value)] if str(value).strip() else []
    try:
        return [str(item) for item in value if str(item).strip()]
    except TypeError:
        return [str(value)] if str(value).strip() else []


def _bounded_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(0.0, min(parsed, 1.0)), 4)


def _cap_clues_by_type(clues: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(clues) <= limit:
        return clues
    by_type: dict[str, list[dict[str, Any]]] = {}
    for clue in clues:
        by_type.setdefault(str(clue.get("clue_type") or "unknown"), []).append(clue)
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for clue_type in sorted(by_type):
        if len(selected) >= limit:
            break
        clue = by_type[clue_type][0]
        selected.append(clue)
        selected_ids.add(id(clue))
    for clue in clues:
        if len(selected) >= limit:
            break
        if id(clue) in selected_ids:
            continue
        selected.append(clue)
        selected_ids.add(id(clue))
    return selected


__all__ = ["IntelligencePipeline", "PipelineResult"]
