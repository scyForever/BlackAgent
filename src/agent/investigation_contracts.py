"""Investigation runtime state/result contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from src.config_loader import InvestigationConfig, InvestigationPolicyOverride
from src.domain import RunPolicyContext

from .budget_controller import BudgetController


SourceCollector = Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass
class InvestigationRunResult:
    status: str
    mode: str
    query: str
    input_count: int
    fetched_count: int
    selected_source_count: int
    high_quality_count: int
    candidate_count: int
    intent: dict[str, Any]
    investigation_plan: dict[str, Any]
    llm_traces: list[dict[str, Any]] = field(default_factory=list)
    selected_sources: list[dict[str, Any]] = field(default_factory=list)
    collection_runs: list[dict[str, Any]] = field(default_factory=list)
    execution_summary: dict[str, Any] = field(default_factory=dict)
    high_quality_clues: list[dict[str, Any]] = field(default_factory=list)
    candidate_clues: list[dict[str, Any]] = field(default_factory=list)
    exploration_hypotheses: list[dict[str, Any]] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimeQualityGate:
    quality_profile: str
    minimum_quality_score: float
    require_cross_source: bool
    require_evidence_chain: bool

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanExecutionControls:
    collection_mode: str
    query_rewrite_policy: str
    refine_policy: str

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _RunPlanningState:
    started_at: float
    normalized_policy_override: InvestigationPolicyOverride | None
    profile: str
    profile_config: dict[str, Any]
    effective_config: InvestigationConfig
    budget_controller: BudgetController
    intent_payload: dict[str, Any]
    plan_payload: dict[str, Any]
    intent_trace: Any
    plan_trace: Any
    runtime_quality_gate: RuntimeQualityGate
    plan_execution_controls: PlanExecutionControls
    budget: dict[str, Any]
    run_policy: RunPolicyContext
    deadline_at: float | None
    gateway_stats_start: int
    available_sources_list: list[dict[str, Any]]
    retrieval_filters: dict[str, Any]
    selected_sources: list[dict[str, Any]]


@dataclass
class _RetrievalState:
    retrieved_clues: list[dict[str, Any]]
    retrieved_summary: dict[str, Any]
    provided_records: list[Any]


@dataclass
class _SemanticLocalState:
    records: list[dict[str, Any]]
    traces: list[dict[str, Any]]
    clues: list[dict[str, Any]]
    phase_payload: dict[str, Any] | None
    summary: dict[str, Any]
    should_collect_live: bool
    live_collection_reasons: list[str]


@dataclass
class _LiveCollectionState:
    records: list[dict[str, Any]]
    collection_runs: list[dict[str, Any]]
    rewrite_traces: list[dict[str, Any]]
    selected_sources: list[dict[str, Any]]
    live_collection_reasons: list[str]


@dataclass
class _FreshProcessingState:
    records: list[Any]
    built_clues: list[dict[str, Any]]
    phase_payload: dict[str, Any]


@dataclass
class _RefinementState:
    pool_clues_for_merge: list[dict[str, Any]]
    merged_candidates: list[dict[str, Any]]
    high_quality_clues: list[dict[str, Any]]
    candidate_clues: list[dict[str, Any]]
    refine_traces: list[dict[str, Any]]
    model_route_traces: list[dict[str, Any]]
    budget_controller_snapshot: Mapping[str, Any]
    actual_refined_count: int
    requested_max_refine: int
    effective_max_refine: int
    refine_budget_reasons: list[str]
    exploration_hypotheses: list[dict[str, Any]]


__all__ = [
    "InvestigationRunResult",
    "PlanExecutionControls",
    "RuntimeQualityGate",
    "SourceCollector",
    "_FreshProcessingState",
    "_LiveCollectionState",
    "_RefinementState",
    "_RetrievalState",
    "_RunPlanningState",
    "_SemanticLocalState",
]
