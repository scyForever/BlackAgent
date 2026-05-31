"""Composable intelligence pipeline wrapper over real deterministic stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.agent.model_router import ModelRouter
from src.domain import RunPolicyContext
from src.pipeline.stages import (
    ClassifyStage,
    CleanStage,
    CorrelateStage,
    DedupStage,
    ExtractStage,
    LLMEnrichStage,
    PassThroughStage,
    ScoreStage,
)
from src.rules import RuleRegistry


@dataclass
class PipelineResult:
    cleaned: list[dict[str, Any]] = field(default_factory=list)
    classified: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    routed: list[dict[str, Any]] = field(default_factory=list)
    enriched: list[dict[str, Any]] = field(default_factory=list)
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
        llm_enrich_stage: Any | None = None,
        correlate_stage: Any | None = None,
        score_stage: Any | None = None,
        model_router: Any | None = None,
        llm_gateway: Any | None = None,
        budget_controller: Any | None = None,
        policy: RunPolicyContext | Mapping[str, Any] | None = None,
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
        self.score_stage = score_stage or ScoreStage()
        self.model_router = model_router or ModelRouter(profile=self.policy.routing_profile)

    def run(self, raw_items: Iterable[Mapping[str, Any]], context: Mapping[str, Any] | None = None) -> PipelineResult:
        context = dict(context or {})
        policy = _coerce_policy(context.get("policy") or self.policy)
        self.policy = policy
        if hasattr(self.model_router, "with_profile"):
            self.model_router = self.model_router.with_profile(policy.routing_profile)
        context["policy"] = policy.model_dump()
        context["routing_profile"] = policy.routing_profile
        context["llm_stage_policy"] = dict(policy.llm_stage_policy)
        materialized_raw = [dict(item) for item in raw_items]
        cleaned = self.clean_stage.run_batch(materialized_raw, context=context)
        deduped = self.dedup_stage.run_batch(cleaned, context=context)
        triaged = self.triage_stage.run_batch(deduped, context=context)
        classified = self.classify_stage.run_batch(triaged, context=context)
        extracted = self.extract_stage.run_batch(classified, context=context)
        routed = [self._route_item(item) for item in extracted]
        enriched = self._run_llm_enrichment(extracted, routed=routed, context=context)
        classifications = [dict(item.get("classification") or {}) for item in enriched if isinstance(item.get("classification"), Mapping)]
        entities = [dict(entity) for item in enriched for entity in (item.get("entities") or []) if isinstance(entity, Mapping)]
        stage_context = {**context, "classifications": classifications, "entities": entities}
        correlated = self.correlate_stage.run_batch(enriched, routed=routed, context=stage_context)
        scored = self.score_stage.run_batch(correlated, context=stage_context)
        entity_graph_summary = {}
        graph_store = getattr(self.correlate_stage, "entity_graph", None)
        if graph_store is not None and hasattr(graph_store, "snapshot"):
            graph_snapshot = graph_store.snapshot()
            entity_graph_summary = {
                key: graph_snapshot.get(key)
                for key in ("entity_count", "observation_count", "relation_count", "cross_source_entity_count")
            }
        return PipelineResult(
            cleaned=[dict(item) for item in cleaned],
            classified=classifications,
            entities=entities,
            routed=routed,
            enriched=[dict(item) for item in enriched],
            clues=[dict(item) for item in scored],
            execution_summary={
                "status": "completed",
                "input_count": len(materialized_raw),
                "cleaned_count": len(cleaned),
                "classified_count": len(classifications),
                "entity_count": len(entities),
                "clue_count": len(scored),
                "llm_enrich_count": sum(1 for item in enriched if item.get("llm_enrichment")),
                "llm_enrich_skipped_count": sum(1 for item in enriched if item.get("llm_enrich_skipped_reason")),
                "llm_enrich_trace_count": len(getattr(self.llm_enrich_stage, "traces", []) or []),
                "stage_mode": "real_components",
                "pipeline_backend": "intelligence_pipeline",
                "routing_profile": policy.routing_profile,
                "model_router_profile": getattr(self.model_router, "profile", None),
                "llm_stage_policy": dict(policy.llm_stage_policy),
                "domain_contract_version": "pipeline_item_v1",
                "entity_graph": entity_graph_summary,
                "rule_version": self.rule_registry.version_hash(),
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

    def _run_llm_enrichment(
        self,
        items: list[dict[str, Any]],
        *,
        routed: list[dict[str, Any]],
        context: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        policy = _coerce_policy(context.get("policy") or self.policy)
        if self.llm_enrich_stage is None or not policy.enable_llm_record_enrich:
            reason = "policy_disabled_record_enrich" if not policy.enable_llm_record_enrich else None
            output = [dict(item) for item in items]
            if reason:
                for item, route in zip(output, routed, strict=False):
                    if str(route.get("action") or "") == "llm_classify_extract":
                        item["llm_enrich_skipped_reason"] = reason
            return output
        return self.llm_enrich_stage.run_batch(
            items,
            routed=routed,
            context={
                **dict(context),
                "allowed_risk_types": [str(item.get("risk_category") or "") for item in items if item.get("risk_category")],
            },
        )


def _coerce_policy(value: RunPolicyContext | Mapping[str, Any] | None) -> RunPolicyContext:
    if isinstance(value, RunPolicyContext):
        return value
    if isinstance(value, Mapping):
        return RunPolicyContext.model_validate(dict(value))
    return RunPolicyContext()


__all__ = ["IntelligencePipeline", "PipelineResult"]
