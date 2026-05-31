"""Independent services extracted from InvestigationRuntime phase mechanics.

The method names in this module are intentionally public (``run``/``build``/
``render``).  Runtime wiring may still provide callables backed by legacy
phase implementations during migration, but workflow code no longer depends on
or names ``_private`` runtime methods.
"""

from __future__ import annotations

from typing import Any, Callable


class SemanticLocalRetrievalService:
    def __init__(self, run_phase: Callable[..., Any]) -> None:
        self._run_phase = run_phase

    def run(self, **kwargs: Any) -> Any:
        return self._run_phase(**kwargs)


class LiveCollectionService:
    def __init__(self, run_phase: Callable[..., Any]) -> None:
        self._run_phase = run_phase

    def run(self, **kwargs: Any) -> Any:
        return self._run_phase(**kwargs)


class FreshProcessingService:
    def __init__(self, run_phase: Callable[..., Any]) -> None:
        self._run_phase = run_phase

    def run(self, **kwargs: Any) -> Any:
        return self._run_phase(**kwargs)


class RefinementOrchestrationService:
    def __init__(self, run_phase: Callable[..., Any]) -> None:
        self._run_phase = run_phase

    def run(self, **kwargs: Any) -> Any:
        return self._run_phase(**kwargs)


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
    return SemanticLocalRetrievalService(run_phase)


def live_collection_service(run_phase: Callable[..., Any]) -> LiveCollectionService:
    return LiveCollectionService(run_phase)


def fresh_processing_service(run_phase: Callable[..., Any]) -> FreshProcessingService:
    return FreshProcessingService(run_phase)


def refinement_orchestration_service(run_phase: Callable[..., Any]) -> RefinementOrchestrationService:
    return RefinementOrchestrationService(run_phase)


def execution_summary_service(build_summary: Callable[..., dict[str, Any]]) -> ExecutionSummaryService:
    return ExecutionSummaryService(build_summary)


def result_render_service(render_result: Callable[[Any], Any]) -> ResultRenderService:
    return ResultRenderService(render_result)


__all__ = [
    "ExecutionSummaryService",
    "FreshProcessingService",
    "LiveCollectionService",
    "RefinementOrchestrationService",
    "ResultRenderService",
    "SemanticLocalRetrievalService",
    "execution_summary_service",
    "fresh_processing_service",
    "live_collection_service",
    "refinement_orchestration_service",
    "result_render_service",
    "semantic_local_retrieval_service",
]
