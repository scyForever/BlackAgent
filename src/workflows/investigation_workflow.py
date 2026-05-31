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


__all__ = ["InvestigationWorkflow"]
