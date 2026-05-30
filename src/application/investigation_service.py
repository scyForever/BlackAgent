"""Application service around the investigation orchestrator."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from src.agent import InvestigationOrchestrator
from src.config_loader import InvestigationPolicyOverride


SourceCollector = Callable[[dict[str, Any]], list[dict[str, Any]]]


class InvestigationService:
    """Thin application boundary for user-query investigation use cases."""

    def __init__(self, orchestrator: InvestigationOrchestrator) -> None:
        self.orchestrator = orchestrator

    def run(
        self,
        query: str,
        *,
        records: Iterable[Mapping[str, Any] | Any] = (),
        available_sources: Iterable[Mapping[str, Any]] = (),
        collect_source_records: SourceCollector | None = None,
        max_sources: int | None = None,
        retrieval_filters: Mapping[str, Any] | None = None,
        max_concurrent_sources: int = 1,
        routing_profile: str | None = None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None = None,
    ) -> Any:
        if not str(query or "").strip():
            raise ValueError("query must not be empty")
        return self.orchestrator.run(
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


__all__ = ["InvestigationService"]
