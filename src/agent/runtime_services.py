"""Independent services extracted from InvestigationRuntime phase mechanics.

The method names in this module are intentionally public (``run``/``build``/
``render``).  Runtime wiring may still provide callables backed by legacy
phase implementations during migration, but workflow code no longer depends on
or names ``_private`` runtime methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def _as_investigation_processing_summary(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["mode"] = "investigation_processing"
    return normalized


@dataclass(frozen=True)
class PhaseDependency:
    """Named phase dependency while legacy runtime methods are extracted."""

    phase_name: str
    run: Callable[..., Any]


@dataclass(frozen=True)
class SemanticLocalRetrievalDependencies:
    collect_semantic_local_records: Callable[..., Any]
    offline_builder: Any
    should_collect_live_sources: Callable[..., Any]
    initial_live_collection_decision: Callable[..., Any]
    semantic_local_limit: Callable[..., Any]
    summarize_retrieved_clues: Callable[..., Any]
    merge_retrieved_summary: Callable[..., Any]
    optional_positive_int: Callable[..., Any]


@dataclass(frozen=True)
class LiveCollectionDependencies:
    cap_live_sources: Callable[..., Any]
    filter_sources_for_collection: Callable[..., Any]
    query_rewrite_skipped_traces: Callable[..., Any]
    rewrite_selected_sources: Callable[..., Any]
    collect_records_from_sources: Callable[..., Any]
    deadline_exhausted: Callable[..., Any]
    runtime_context_label: Callable[..., Any]
    stage_deadline_ms: Callable[..., Any]
    phase_engine: Any


@dataclass(frozen=True)
class FreshProcessingDependencies:
    offline_builder: Any


@dataclass(frozen=True)
class RefinementDependencies:
    clue_refinement: Any
    merge_candidate_clues: Callable[..., Any]
    build_exploration_hypotheses: Callable[..., Any]


@dataclass(frozen=True)
class ExecutionSummaryDependencies:
    summarize_model_routes: Callable[..., Any]
    gateway_stats_since: Callable[..., Any]
    summarize_gateway_stats: Callable[..., Any]
    merge_llm_cost_summary: Callable[..., Any]
    build_telemetry: Callable[..., Any]
    mask_execution_summary: Callable[..., Any]
    deadline_exhausted: Callable[..., Any]
    orchestration_route: Callable[..., Any]
    execution_mode: Callable[..., Any]


class _PhaseService:
    def __init__(self, dependency: PhaseDependency | Callable[..., Any], *, phase_name: str) -> None:
        self.dependency = dependency if isinstance(dependency, PhaseDependency) else PhaseDependency(phase_name, dependency)

    def run(self, **kwargs: Any) -> Any:
        return self.dependency.run(**kwargs)


class SemanticLocalRetrievalService(_PhaseService):
    def __init__(self, dependency: PhaseDependency | Callable[..., Any]) -> None:
        super().__init__(dependency, phase_name="semantic_local_retrieval")


class LiveCollectionService(_PhaseService):
    def __init__(self, dependency: PhaseDependency | Callable[..., Any]) -> None:
        super().__init__(dependency, phase_name="live_collection")


class FreshProcessingService(_PhaseService):
    def __init__(self, dependency: PhaseDependency | Callable[..., Any] | FreshProcessingDependencies) -> None:
        if isinstance(dependency, FreshProcessingDependencies):
            self.dependencies = dependency
            super().__init__(PhaseDependency("fresh_processing", self._run_with_dependencies), phase_name="fresh_processing")
        else:
            self.dependencies = None
            super().__init__(dependency, phase_name="fresh_processing")

    def _run_with_dependencies(self, **kwargs: Any) -> Any:
        from .investigation_contracts import _FreshProcessingState

        assert isinstance(self.dependencies, FreshProcessingDependencies)
        query = kwargs["query"]
        run_state = kwargs["run_state"]
        retrieval_state = kwargs["retrieval_state"]
        semantic_state = kwargs["semantic_state"]
        live_state = kwargs["live_state"]
        fresh_records = retrieval_state.provided_records if retrieval_state.provided_records else (live_state.records or semantic_state.records)
        built_clues: list[dict[str, Any]] = []
        if live_state.records or retrieval_state.provided_records:
            self.dependencies.offline_builder.set_runtime_controls(
                llm_gateway=getattr(run_state, "llm_gateway", None),
                budget_controller=run_state.budget_controller,
                policy=run_state.run_policy,
            )
            build_result = self.dependencies.offline_builder.build(
                fresh_records,
                prompt_text=query,
                source_candidates=live_state.selected_sources or run_state.available_sources_list,
                quality_profile=str(run_state.intent_payload.get("quality_profile") or "balanced"),
                require_cross_source=bool(run_state.intent_payload.get("require_cross_source")),
                require_evidence_chain=bool(run_state.intent_payload.get("require_evidence_chain", True)),
                policy=run_state.run_policy,
            )
            phase_payload = _as_investigation_processing_summary(build_result.execution_summary)
            built_clues = build_result.clues
        else:
            phase_payload = {
                "status": "completed",
                "mode": "investigation_processing",
                "input_count": 0,
                "accepted_count": 0,
                "dropped_count": 0,
                "classification_count": 0,
                "entity_count": 0,
                "cluster_count": 0,
                "risk_clue_count": 0,
                "playbook_count": 0,
                "strategy_count": 0,
            }
        if semantic_state.phase_payload is not None and not (live_state.records or retrieval_state.provided_records):
            phase_payload = semantic_state.phase_payload
        return _FreshProcessingState(records=fresh_records, built_clues=built_clues, phase_payload=phase_payload)


class RefinementOrchestrationService(_PhaseService):
    def __init__(self, dependency: PhaseDependency | Callable[..., Any]) -> None:
        super().__init__(dependency, phase_name="refinement_orchestration")


class ExecutionSummaryService:
    def __init__(self, build_summary: Callable[..., dict[str, Any]]) -> None:
        self._build_summary = build_summary

    def build(self, **kwargs: Any) -> dict[str, Any]:
        return self._build_summary(**kwargs)


class ResultRenderService:
    def __init__(self, render_result: Callable[[Any], Any]) -> None:
        self._render_result = render_result

    def render(self, context: Any) -> Any:
        return self._render_result(context)


def semantic_local_retrieval_service(run_phase: Callable[..., Any]) -> SemanticLocalRetrievalService:
    return SemanticLocalRetrievalService(PhaseDependency("semantic_local_retrieval", run_phase))


def live_collection_service(run_phase: Callable[..., Any]) -> LiveCollectionService:
    return LiveCollectionService(PhaseDependency("live_collection", run_phase))


def fresh_processing_service(dependency: Callable[..., Any] | FreshProcessingDependencies) -> FreshProcessingService:
    if isinstance(dependency, FreshProcessingDependencies):
        return FreshProcessingService(dependency)
    return FreshProcessingService(PhaseDependency("fresh_processing", dependency))


def refinement_orchestration_service(run_phase: Callable[..., Any]) -> RefinementOrchestrationService:
    return RefinementOrchestrationService(PhaseDependency("refinement_orchestration", run_phase))


def execution_summary_service(build_summary: Callable[..., dict[str, Any]]) -> ExecutionSummaryService:
    return ExecutionSummaryService(build_summary)


def result_render_service(render_result: Callable[[Any], Any]) -> ResultRenderService:
    return ResultRenderService(render_result)


__all__ = [
    "ExecutionSummaryService",
    "ExecutionSummaryDependencies",
    "FreshProcessingService",
    "FreshProcessingDependencies",
    "LiveCollectionService",
    "LiveCollectionDependencies",
    "PhaseDependency",
    "RefinementOrchestrationService",
    "RefinementDependencies",
    "ResultRenderService",
    "SemanticLocalRetrievalService",
    "SemanticLocalRetrievalDependencies",
    "execution_summary_service",
    "fresh_processing_service",
    "live_collection_service",
    "refinement_orchestration_service",
    "result_render_service",
    "semantic_local_retrieval_service",
]
