"""Thin workflow runner that owns the investigation step sequence."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.config_loader import InvestigationPolicyOverride
from src.agent.investigation_orchestrator import time as orchestrator_time

from .workflow_context import WorkflowContext
from .workflow_result import WorkflowResult


class InvestigationWorkflow:
    """Sequence the investigation services without embedding business logic."""

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

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
        self._sync_compat_time_module()
        run_state = self.orchestrator.run_state_preparation.prepare(
            query=query,
            available_sources=available_sources,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            run_state_type=self.orchestrator.run_state_type,
        )
        retrieval_state = self.orchestrator.initial_candidate_retrieval.retrieve(
            query=query,
            records=records,
            run_state=run_state,
            retrieval_state_type=self.orchestrator.retrieval_state_type,
        )
        context = WorkflowContext(query=query, run_state=run_state, retrieval_state=retrieval_state)
        context.semantic_state = self.orchestrator._run_semantic_local_phase(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        context.live_state = self.orchestrator._run_live_collection_phase(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
        )
        context.fresh_state = self.orchestrator._process_fresh_records(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
        )
        context.refinement_state = self.orchestrator._refine_and_explore_candidates(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
        )
        context.execution_summary = self.orchestrator._build_execution_summary(
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
            refinement_state=context.refinement_state,
        )
        return WorkflowResult(context=context, payload=self.orchestrator._render_run_result(context))

    def _sync_compat_time_module(self) -> None:
        """Respect legacy monkeypatches against investigation_orchestrator.time."""

        runtime_method = getattr(self.orchestrator._deadline_exhausted, "__func__", None)
        if runtime_method is not None:
            runtime_method.__globals__["time"] = orchestrator_time
        telemetry_method = getattr(self.orchestrator._build_telemetry, "__func__", None)
        if telemetry_method is not None:
            telemetry_method.__globals__["time"] = orchestrator_time


__all__ = ["InvestigationWorkflow"]
