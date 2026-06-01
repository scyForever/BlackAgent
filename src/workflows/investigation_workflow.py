"""Thin workflow runner that owns the investigation step sequence."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.config_loader import InvestigationPolicyOverride
from .workflow_context import WorkflowContext
from .workflow_result import WorkflowResult


class InvestigationWorkflow:
    """Sequence the investigation services without embedding business logic."""

    def __init__(
        self,
        *,
        run_state_preparation: Any,
        initial_candidate_retrieval: Any,
        semantic_local_retrieval: Any,
        live_collection_service: Any,
        fresh_processing_service: Any,
        refinement_service: Any,
        execution_summary_service: Any,
        result_render_service: Any,
        run_state_type: type[Any],
        retrieval_state_type: type[Any],
    ) -> None:
        self.run_state_preparation = run_state_preparation
        self.initial_candidate_retrieval = initial_candidate_retrieval
        self.semantic_local_retrieval = semantic_local_retrieval
        self.live_collection_service = live_collection_service
        self.fresh_processing_service = fresh_processing_service
        self.refinement_service = refinement_service
        self.execution_summary_service = execution_summary_service
        self.result_render_service = result_render_service
        self.run_state_type = run_state_type
        self.retrieval_state_type = retrieval_state_type

    def run(
        self,
        query: str,
        *,
        records: Iterable[Mapping[str, Any] | Any] = (),
        available_sources: Iterable[Mapping[str, Any]] = (),
        collect_source_records: Any | None = None,
        max_sources: int | None = None,
        retrieval_filters: Mapping[str, Any] | None = None,
        max_concurrent_sources: int = 1,
        routing_profile: str | None = None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None = None,
    ) -> WorkflowResult:
        context = self.build_context(
            query,
            records=records,
            available_sources=available_sources,
            collect_source_records=collect_source_records,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            max_concurrent_sources=max_concurrent_sources,
            routing_profile=routing_profile,
            policy_override=policy_override,
        )
        return WorkflowResult(context=context, payload=self.result_render_service.render(context))

    def build_context(
        self,
        query: str,
        *,
        records: Iterable[Mapping[str, Any] | Any] = (),
        available_sources: Iterable[Mapping[str, Any]] = (),
        collect_source_records: Any | None = None,
        max_sources: int | None = None,
        retrieval_filters: Mapping[str, Any] | None = None,
        max_concurrent_sources: int = 1,
        routing_profile: str | None = None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None = None,
    ) -> WorkflowContext:
        records_list = list(records)
        available_sources_list = list(available_sources)
        if records_list:
            return self._build_planned_context(
                query,
                records=records_list,
                available_sources=available_sources_list,
                collect_source_records=collect_source_records,
                max_sources=max_sources,
                retrieval_filters=retrieval_filters,
                max_concurrent_sources=max_concurrent_sources,
                routing_profile=routing_profile,
                policy_override=policy_override,
            )
        run_state = self.run_state_preparation.prepare(
            query=query,
            available_sources=available_sources_list,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            run_state_type=self.run_state_type,
            planning_mode="preflight",
        )
        retrieval_state = self.initial_candidate_retrieval.retrieve(
            query=query,
            records=records_list,
            run_state=run_state,
            retrieval_state_type=self.retrieval_state_type,
        )
        context = WorkflowContext(query=query, run_state=run_state, retrieval_state=retrieval_state)
        context.semantic_state = self.semantic_local_retrieval.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        if not self._preflight_satisfied(retrieval_state=retrieval_state, semantic_state=context.semantic_state):
            evidence_gap = getattr(context.semantic_state, "evidence_gap", None)
            flow_decision_traces = [
                {
                    "stage": "preflight_evidence_gap",
                    "next_action": "run_conditional_planning",
                    "reason": "asset_retrieval_insufficient_for_direct_report",
                    "evidence_gap": evidence_gap.model_dump() if hasattr(evidence_gap, "model_dump") else evidence_gap,
                }
            ]
            run_state = self.run_state_preparation.prepare(
                query=query,
                available_sources=available_sources_list,
                max_sources=max_sources,
                retrieval_filters=retrieval_filters,
                routing_profile=routing_profile,
                policy_override=policy_override,
                run_state_type=self.run_state_type,
                evidence_gap=evidence_gap.model_dump() if hasattr(evidence_gap, "model_dump") else evidence_gap,
                flow_decision_traces=flow_decision_traces,
            )
            retrieval_state = self.initial_candidate_retrieval.retrieve(
                query=query,
                records=records_list,
                run_state=run_state,
                retrieval_state_type=self.retrieval_state_type,
            )
            context = WorkflowContext(query=query, run_state=run_state, retrieval_state=retrieval_state)
            context.semantic_state = self.semantic_local_retrieval.run(
                query=query,
                run_state=run_state,
                retrieval_state=retrieval_state,
                collect_source_records=collect_source_records,
            )
        else:
            run_state.flow_decision_traces.append(
                {
                    "stage": "preflight_evidence_gap",
                    "next_action": "skip_conditional_planning",
                    "reason": "asset_retrieval_satisfied_evidence_gap",
                    "evidence_gap": context.semantic_state.evidence_gap.model_dump(),
                }
            )
        context.live_state = self.live_collection_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
        )
        context.fresh_state = self.fresh_processing_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
        )
        context.refinement_state = self.refinement_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
        )
        context.execution_summary = self.execution_summary_service.build(
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
            refinement_state=context.refinement_state,
        )
        return context

    def _build_planned_context(
        self,
        query: str,
        *,
        records: list[Mapping[str, Any] | Any],
        available_sources: Iterable[Mapping[str, Any]],
        collect_source_records: Any | None,
        max_sources: int | None,
        retrieval_filters: Mapping[str, Any] | None,
        max_concurrent_sources: int,
        routing_profile: str | None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None,
    ) -> WorkflowContext:
        run_state = self.run_state_preparation.prepare(
            query=query,
            available_sources=available_sources,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            run_state_type=self.run_state_type,
        )
        retrieval_state = self.initial_candidate_retrieval.retrieve(
            query=query,
            records=records,
            run_state=run_state,
            retrieval_state_type=self.retrieval_state_type,
        )
        context = WorkflowContext(query=query, run_state=run_state, retrieval_state=retrieval_state)
        context.semantic_state = self.semantic_local_retrieval.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        context.live_state = self.live_collection_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
        )
        context.fresh_state = self.fresh_processing_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
        )
        context.refinement_state = self.refinement_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
        )
        context.execution_summary = self.execution_summary_service.build(
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
            refinement_state=context.refinement_state,
        )
        return context

    @staticmethod
    def _preflight_satisfied(*, retrieval_state: Any, semantic_state: Any) -> bool:
        if bool(getattr(retrieval_state, "provided_records", [])):
            return False
        has_assets = bool(getattr(retrieval_state, "retrieved_clues", [])) or bool(getattr(semantic_state, "clues", []))
        gap = getattr(semantic_state, "evidence_gap", None)
        return bool(has_assets and getattr(gap, "is_sufficient", False))


__all__ = ["InvestigationWorkflow"]
