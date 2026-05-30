"""LLM-driven investigation orchestration over the existing BlackAgent pipeline."""

from __future__ import annotations

from copy import deepcopy
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from src.config_loader import InvestigationConfig, InvestigationPolicyOverride
from src.backend import LLMGateway
from src.enhancement.clue_quality import ClueQualityEvaluator
from src.enhancement.engine import PhaseTwoThreeEngine
from src.enhancement.llm_clue_refiner import LLMClueRefiner
from src.pipeline import OfflineClueBuilder
from src.retrieval import ClueRetriever
from src.scheduling.layered_collection import (
    group_sources_by_collection_layer,
    prioritize_sources_for_investigation,
)
from src.safety import PIIMasker
from storage import ClueRepo, InMemoryClueRepo, InMemoryReviewRepo

from .budget_controller import BudgetController, RuntimeBudget
from .clue_ranker import ClueRanker
from .exploration_agent import ExplorationAgent
from .model_router import ModelRouter
from .query_rewriter import LLMSourceQueryRewriter
from .user_request_parser import (
    DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS,
    LLMInvestigationPlanner,
    LLMUserRequestParser,
    _fallback_intent,
    _fallback_plan,
)


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


@dataclass(frozen=True)
class RuntimeQualityGate:
    quality_profile: str
    minimum_quality_score: float
    require_cross_source: bool
    require_evidence_chain: bool

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
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
    deadline_at: float | None
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


class InvestigationOrchestrator:
    """Top-level user-query-driven coordinator with LLM planning and safe local execution."""

    def __init__(
        self,
        *,
        llm_gateway: LLMGateway,
        phase_engine: PhaseTwoThreeEngine | None = None,
        quality_evaluator: ClueQualityEvaluator | None = None,
        clue_repo: ClueRepo | None = None,
        clue_retriever: ClueRetriever | None = None,
        review_repo: InMemoryReviewRepo | None = None,
        investigation_config: InvestigationConfig | None = None,
        routing_profiles: Mapping[str, Any] | None = None,
    ) -> None:
        self.llm_gateway = llm_gateway
        self.phase_engine = phase_engine or PhaseTwoThreeEngine()
        self.quality_evaluator = quality_evaluator or ClueQualityEvaluator()
        self.clue_repo = clue_repo if clue_repo is not None else InMemoryClueRepo()
        self.clue_retriever = clue_retriever or ClueRetriever()
        self.review_repo = review_repo or InMemoryReviewRepo()
        self.investigation_config = investigation_config or InvestigationConfig()
        self.routing_profiles = {str(key): value for key, value in (routing_profiles or {}).items()}
        self.clue_refiner = LLMClueRefiner(llm_gateway)
        self.query_rewriter = LLMSourceQueryRewriter(llm_gateway)
        self.model_router = ModelRouter()
        self.clue_ranker = ClueRanker()
        self.exploration_agent = ExplorationAgent()
        self.offline_builder = OfflineClueBuilder(
            phase_engine=self.phase_engine,
            quality_evaluator=self.quality_evaluator,
            clue_repo=self.clue_repo,
        )
        self.intent_parser = LLMUserRequestParser(llm_gateway)
        self.planner = LLMInvestigationPlanner(llm_gateway)

    def ingest_review_decision(
        self,
        hypothesis_id: str,
        *,
        decision: str,
        reviewer: str = "system",
        notes: str | None = None,
        edited_risk_type: str | None = None,
        secondary_label: str | None = None,
        corrected_entities: list[dict[str, Any]] | None = None,
        add_to_wordlist: bool = False,
    ) -> dict[str, Any]:
        hypothesis = self.review_repo.get(hypothesis_id)
        state = self.review_repo.mark_reviewed(
            hypothesis_id,
            decision=decision,
            reviewer=reviewer,
            notes=notes,
            edited_risk_type=edited_risk_type,
            secondary_label=secondary_label,
            corrected_entities=corrected_entities,
            add_to_wordlist=add_to_wordlist,
        )
        payload = {
            "decision": state.decision.value if state.decision is not None else decision,
            "source_trace_id": str(hypothesis.source_trace_id) if hypothesis is not None else "unknown",
            "reviewer": reviewer,
            "notes": notes,
            "edits": {
                "edited_risk_type": edited_risk_type,
                "secondary_label": secondary_label,
                "corrected_entities": list(corrected_entities or ()),
                "add_to_wordlist": add_to_wordlist,
            },
        }
        self.phase_engine.lifecycle_manager.ingest_review_decision(payload)
        return {
            "review_state": state.model_dump(),
            "lifecycle_context": deepcopy(
                self.phase_engine.lifecycle_manager.prompt_context(
                    label=edited_risk_type,
                    include_candidates=True,
                )
            ),
        }

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
    ) -> InvestigationRunResult:
        run_state = self._prepare_run_state(
            query=query,
            available_sources=available_sources,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
        )
        retrieval_state = self._retrieve_initial_candidates(
            query=query,
            records=records,
            run_state=run_state,
            collect_source_records=collect_source_records,
        )
        semantic_state = self._run_semantic_local_phase(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        live_state = self._run_live_collection_phase(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=semantic_state,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
        )
        fresh_state = self._process_fresh_records(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=semantic_state,
            live_state=live_state,
        )
        refinement_state = self._refine_and_explore_candidates(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=semantic_state,
            live_state=live_state,
            fresh_state=fresh_state,
        )
        execution_summary = self._build_execution_summary(
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=semantic_state,
            live_state=live_state,
            fresh_state=fresh_state,
            refinement_state=refinement_state,
        )

        return InvestigationRunResult(
            status="completed" if (fresh_state.records or semantic_state.clues or retrieval_state.retrieved_clues) else "no_data",
            mode="llm_driven_investigation",
            query=query,
            input_count=len(fresh_state.records),
            fetched_count=len(live_state.records) if live_state.records else len(fresh_state.records),
            selected_source_count=len(live_state.selected_sources),
            high_quality_count=len(refinement_state.high_quality_clues),
            candidate_count=len(refinement_state.candidate_clues),
            intent=run_state.intent_payload,
            investigation_plan=run_state.plan_payload,
            llm_traces=[
                run_state.intent_trace.model_dump(),
                run_state.plan_trace.model_dump(),
                *semantic_state.traces,
                *live_state.rewrite_traces,
                *refinement_state.model_route_traces,
                *refinement_state.refine_traces,
            ],
            selected_sources=live_state.selected_sources,
            collection_runs=live_state.collection_runs,
            execution_summary=execution_summary,
            high_quality_clues=refinement_state.high_quality_clues,
            candidate_clues=refinement_state.candidate_clues,
            exploration_hypotheses=refinement_state.exploration_hypotheses,
        )

    def _prepare_run_state(
        self,
        *,
        query: str,
        available_sources: Iterable[Mapping[str, Any]],
        max_sources: int | None,
        retrieval_filters: Mapping[str, Any] | None,
        routing_profile: str | None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None,
    ) -> _RunPlanningState:
        started_at = time.perf_counter()
        normalized_policy_override = self._normalize_policy_override(policy_override)
        profile = self._normalize_routing_profile(routing_profile)
        profile_config = self._routing_profile_config(profile) if (routing_profile is not None or self.routing_profiles) else {}
        effective_config = self._effective_investigation_config(
            routing_profile=routing_profile,
            policy_override=normalized_policy_override,
        )
        initial_runtime_context = self._planner_runtime_context()
        budget_controller = BudgetController(RuntimeBudget.from_mapping(self._profile_budget_defaults(profile_config)))
        if bool(profile_config.get("enable_llm_intent_parse", True)):
            intent, intent_trace = self.intent_parser.parse(
                query,
                runtime_context=initial_runtime_context,
                budget=budget_controller,
                deadline_ms=self._stage_deadline_ms(profile_config, default=1500),
            )
        else:
            intent = _fallback_intent(query, runtime_context=initial_runtime_context)
            intent_trace = self._disabled_llm_trace(
                "intent_parse",
                reason="profile_disabled_llm_intent_parse",
                runtime_context=initial_runtime_context,
            )
        intent_payload = intent.model_dump()
        available_sources_list = [dict(source) for source in available_sources]
        if profile == "fast":
            plan = _fallback_plan(intent, runtime_context=initial_runtime_context)
            plan_trace = self._disabled_llm_trace(
                "investigation_plan",
                reason="profile_fast_uses_deterministic_fallback_plan",
                runtime_context=initial_runtime_context,
            )
        else:
            plan, plan_trace = self.planner.plan(
                query,
                intent,
                available_sources=available_sources_list,
                runtime_context=initial_runtime_context,
                budget=budget_controller,
                deadline_ms=self._stage_deadline_ms(profile_config, default=2500),
            )
        plan_payload = plan.model_dump()
        runtime_quality_gate = self._runtime_quality_gate(
            intent=intent_payload,
            plan=plan_payload,
            policy_override=normalized_policy_override,
        )
        plan_execution_controls = self._apply_profile_execution_controls(
            self._plan_execution_controls(plan_payload),
            profile_config=profile_config,
            profile=profile,
        )
        budget = self._resolve_budget(
            plan_payload,
            explicit_max_sources=max_sources,
            available_source_count=len(available_sources_list),
            policy_override=normalized_policy_override,
            profile_config=profile_config,
        )
        budget_controller.budget = RuntimeBudget.from_mapping(budget)
        selected_sources = self._select_sources(
            plan_payload,
            available_sources_list,
            max_sources=budget["max_sources"],
            risk_types=intent.risk_types,
        )
        if isinstance(budget.get("max_sources"), int) and budget["max_sources"] > 0:
            selected_sources = selected_sources[: int(budget["max_sources"])]
        return _RunPlanningState(
            started_at=started_at,
            normalized_policy_override=normalized_policy_override,
            profile=profile,
            profile_config=profile_config,
            effective_config=effective_config,
            budget_controller=budget_controller,
            intent_payload=intent_payload,
            plan_payload=plan_payload,
            intent_trace=intent_trace,
            plan_trace=plan_trace,
            runtime_quality_gate=runtime_quality_gate,
            plan_execution_controls=plan_execution_controls,
            budget=budget,
            deadline_at=self._deadline_at(started_at, budget["max_elapsed_seconds"]),
            available_sources_list=available_sources_list,
            retrieval_filters=dict(retrieval_filters or {}),
            selected_sources=selected_sources,
        )

    def _retrieve_initial_candidates(
        self,
        *,
        query: str,
        records: Iterable[Mapping[str, Any] | Any],
        run_state: _RunPlanningState,
        collect_source_records: SourceCollector | None,
    ) -> _RetrievalState:
        retrieved_clues = self.clue_retriever.retrieve(
            self.clue_repo.list(),
            query=query,
            intent=run_state.intent_payload,
            limit=run_state.budget["max_candidate_clues"],
            time_range_hours=self._optional_positive_int(run_state.retrieval_filters.get("time_range_hours")),
            allowed_source_types=run_state.retrieval_filters.get("source_types") or (),
            allowed_risk_types=run_state.retrieval_filters.get("risk_types") or (),
            min_quality_score=self._optional_float(run_state.retrieval_filters.get("min_quality_score")),
        )
        retrieved_summary = self._summarize_retrieved_clues(
            retrieved_clues,
            time_range_hours=self._optional_positive_int(run_state.retrieval_filters.get("time_range_hours"))
            or self._optional_positive_int(run_state.intent_payload.get("time_range_hours")),
            quality_gate=run_state.runtime_quality_gate,
        )
        provided_records = [dict(record) if isinstance(record, Mapping) else record for record in records]
        if len(provided_records) > run_state.budget["max_raw_records"]:
            provided_records = provided_records[: run_state.budget["max_raw_records"]]
        return _RetrievalState(
            retrieved_clues=retrieved_clues,
            retrieved_summary=retrieved_summary,
            provided_records=provided_records,
        )

    def _initial_live_collection_decision(
        self,
        *,
        run_state: _RunPlanningState,
        retrieval_state: _RetrievalState,
        collect_source_records: SourceCollector | None,
    ) -> tuple[bool, list[str]]:
        return self._should_collect_live_sources(
            config=run_state.effective_config,
            intent=run_state.intent_payload,
            quality_gate=run_state.runtime_quality_gate,
            execution_controls=run_state.plan_execution_controls,
            selected_sources=run_state.selected_sources,
            retrieved_summary=retrieval_state.retrieved_summary,
            retrieval_filters=run_state.retrieval_filters,
            collect_source_records=collect_source_records,
            has_provided_records=bool(retrieval_state.provided_records),
        )

    def _run_semantic_local_phase(
        self,
        *,
        query: str,
        run_state: _RunPlanningState,
        retrieval_state: _RetrievalState,
        collect_source_records: SourceCollector | None,
    ) -> _SemanticLocalState:
        summary = {
            "query_limit": 0,
            "hit_count": 0,
            "record_count": 0,
            "clue_count": 0,
            "graph_expanded_count": 0,
        }
        should_collect_live, live_collection_reasons = self._initial_live_collection_decision(
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        records: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        clues: list[dict[str, Any]] = []
        phase_payload: dict[str, Any] | None = None
        if not (should_collect_live and not retrieval_state.provided_records):
            return _SemanticLocalState(records, traces, clues, phase_payload, summary, should_collect_live, live_collection_reasons)

        semantic_local_limit = self._semantic_local_limit(budget=run_state.budget)
        summary["query_limit"] = semantic_local_limit
        records, traces = self._collect_semantic_local_records(query=query, limit=semantic_local_limit)
        summary["hit_count"] = sum(1 for item in traces if item.get("stage") == "semantic_local_retrieval")
        summary["record_count"] = len(records)
        summary["graph_expanded_count"] = sum(1 for item in traces if item.get("stage") == "semantic_graph_expansion")
        if not records:
            return _SemanticLocalState(records, traces, clues, phase_payload, summary, should_collect_live, live_collection_reasons)

        semantic_local_build = self.offline_builder.build(
            records,
            prompt_text=query,
            source_candidates=run_state.selected_sources or run_state.available_sources_list,
            quality_profile=str(run_state.intent_payload.get("quality_profile") or "balanced"),
            require_cross_source=bool(run_state.intent_payload.get("require_cross_source")),
            require_evidence_chain=bool(run_state.intent_payload.get("require_evidence_chain", True)),
        )
        phase_payload = _as_investigation_processing_summary(semantic_local_build.execution_summary)
        clues = semantic_local_build.clues
        summary["clue_count"] = len(clues)
        semantic_retrieved_summary = self._summarize_retrieved_clues(
            clues,
            time_range_hours=self._optional_positive_int(run_state.retrieval_filters.get("time_range_hours"))
            or self._optional_positive_int(run_state.intent_payload.get("time_range_hours")),
            quality_gate=run_state.runtime_quality_gate,
        )
        merged_retrieved_summary = self._merge_retrieved_summary(retrieval_state.retrieved_summary, semantic_retrieved_summary)
        should_collect_live, live_collection_reasons = self._should_collect_live_sources(
            config=run_state.effective_config,
            intent=run_state.intent_payload,
            quality_gate=run_state.runtime_quality_gate,
            execution_controls=run_state.plan_execution_controls,
            selected_sources=run_state.selected_sources,
            retrieved_summary=merged_retrieved_summary,
            retrieval_filters=run_state.retrieval_filters,
            collect_source_records=collect_source_records,
            has_provided_records=bool(retrieval_state.provided_records),
        )
        if should_collect_live and int(semantic_retrieved_summary.get("high_quality_count") or 0) > 0:
            if set(live_collection_reasons).issubset({"insufficient_high_quality_pool_clues"}):
                should_collect_live = False
                live_collection_reasons = ["semantic_local_high_quality_satisfied"]
        return _SemanticLocalState(records, traces, clues, phase_payload, summary, should_collect_live, live_collection_reasons)

    def _run_live_collection_phase(
        self,
        *,
        query: str,
        run_state: _RunPlanningState,
        retrieval_state: _RetrievalState,
        semantic_state: _SemanticLocalState,
        collect_source_records: SourceCollector | None,
        max_concurrent_sources: int,
    ) -> _LiveCollectionState:
        selected_sources = [dict(item) for item in run_state.selected_sources]
        live_collection_reasons = list(semantic_state.live_collection_reasons)
        rewrite_traces: list[dict[str, Any]] = []
        live_records: list[dict[str, Any]] = []
        collection_runs: list[dict[str, Any]] = []
        if not (semantic_state.should_collect_live and collect_source_records is not None):
            return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons)

        collection_deadline_at = run_state.deadline_at
        planning_exhausted_before_first_collection = (
            self._deadline_exhausted(collection_deadline_at)
            and not retrieval_state.provided_records
            and not semantic_state.records
            and not retrieval_state.retrieved_clues
        )
        if planning_exhausted_before_first_collection:
            live_collection_reasons.append("elapsed_budget_reset_for_first_live_collection")
            collection_deadline_at = None
        if self._deadline_exhausted(collection_deadline_at):
            live_collection_reasons.append("elapsed_budget_exhausted_before_live_collection")
            return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons)

        selected_sources = self._cap_live_sources(
            selected_sources,
            retrieved_summary=retrieval_state.retrieved_summary,
            config=run_state.effective_config,
        )
        if (
            run_state.plan_execution_controls.query_rewrite_policy == "off"
            or planning_exhausted_before_first_collection
            or not bool(run_state.profile_config.get("enable_query_rewrite", True))
            or int(run_state.budget.get("max_query_rewrite_sources") or 0) <= 0
        ):
            rewrite_traces = self._query_rewrite_skipped_traces(
                selected_sources,
                reason=(
                    "elapsed_budget_exhausted_before_query_rewrite"
                    if planning_exhausted_before_first_collection
                    else "profile_disabled_query_rewrite"
                    if not bool(run_state.profile_config.get("enable_query_rewrite", True))
                    else "query_rewrite_budget_zero"
                    if int(run_state.budget.get("max_query_rewrite_sources") or 0) <= 0
                    else "plan_query_rewrite_disabled"
                ),
            )
        else:
            selected_sources, rewrite_traces = self._rewrite_selected_sources(
                selected_sources,
                query=query,
                intent=run_state.intent_payload,
                plan=run_state.plan_payload,
                runtime_context=self.phase_engine.runtime_prompt_context(
                    label=self._runtime_context_label(run_state.intent_payload),
                    include_candidates=True,
                ),
                max_rewrite_sources=int(run_state.budget.get("max_query_rewrite_sources") or 0),
                budget=run_state.budget_controller,
                deadline_ms=self._stage_deadline_ms(run_state.profile_config, default=2000),
            )
            if (
                self._deadline_exhausted(collection_deadline_at)
                and not retrieval_state.provided_records
                and not semantic_state.records
                and not retrieval_state.retrieved_clues
            ):
                live_collection_reasons.append("elapsed_budget_reset_after_query_rewrite_for_first_live_collection")
                collection_deadline_at = None
        live_records, collection_runs = self._collect_records_from_sources(
            selected_sources,
            collect_source_records=collect_source_records,
            max_raw_records=run_state.budget["max_raw_records"],
            max_concurrent_sources=max_concurrent_sources,
            deadline_at=collection_deadline_at,
        )
        return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons)

    def _process_fresh_records(
        self,
        *,
        query: str,
        run_state: _RunPlanningState,
        retrieval_state: _RetrievalState,
        semantic_state: _SemanticLocalState,
        live_state: _LiveCollectionState,
    ) -> _FreshProcessingState:
        fresh_records = retrieval_state.provided_records if retrieval_state.provided_records else (live_state.records or semantic_state.records)
        built_clues: list[dict[str, Any]] = []
        if live_state.records or retrieval_state.provided_records:
            build_result = self.offline_builder.build(
                fresh_records,
                prompt_text=query,
                source_candidates=live_state.selected_sources or run_state.available_sources_list,
                quality_profile=str(run_state.intent_payload.get("quality_profile") or "balanced"),
                require_cross_source=bool(run_state.intent_payload.get("require_cross_source")),
                require_evidence_chain=bool(run_state.intent_payload.get("require_evidence_chain", True)),
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

    def _refine_and_explore_candidates(
        self,
        *,
        query: str,
        run_state: _RunPlanningState,
        retrieval_state: _RetrievalState,
        semantic_state: _SemanticLocalState,
        live_state: _LiveCollectionState,
        fresh_state: _FreshProcessingState,
    ) -> _RefinementState:
        pool_clues_for_merge = (
            retrieval_state.retrieved_clues
            if not fresh_state.records
            else [
                dict(clue)
                for clue in retrieval_state.retrieved_clues
                if float(clue.get("retrieval_score") or 0.0) >= run_state.effective_config.retrieval_score_threshold_for_pool_merge
            ]
        )
        merged_candidates = self._merge_candidate_clues(
            pool_clues=pool_clues_for_merge,
            fresh_clues=[*semantic_state.clues, *fresh_state.built_clues],
        )
        if not merged_candidates and retrieval_state.retrieved_clues and not fresh_state.records:
            merged_candidates = [dict(clue) for clue in retrieval_state.retrieved_clues]
        requested_max_refine = 0 if run_state.plan_execution_controls.refine_policy == "off" else int(run_state.budget["max_llm_refine_clues"] or 0)
        effective_max_refine = max(0, requested_max_refine)
        refine_budget_reasons: list[str] = []
        refined_high_quality, refined_candidates, refine_traces, model_route_traces, budget_controller_snapshot = self._refine_retrieved_clues(
            merged_candidates,
            query=query,
            intent=run_state.intent_payload,
            quality_gate=run_state.runtime_quality_gate,
            max_refine=effective_max_refine,
            deadline_at=run_state.deadline_at,
            routing_profile=run_state.profile,
            budget_controller=run_state.budget_controller,
        )
        exploration_hypotheses = self._build_exploration_hypotheses(
            query=query,
            processed_records=fresh_state.records,
            candidate_clues=refined_candidates,
            high_quality_clues=refined_high_quality,
            runtime_quality_gate=run_state.runtime_quality_gate,
        )
        return _RefinementState(
            pool_clues_for_merge=pool_clues_for_merge,
            merged_candidates=merged_candidates,
            high_quality_clues=refined_high_quality,
            candidate_clues=refined_candidates,
            refine_traces=refine_traces,
            model_route_traces=model_route_traces,
            budget_controller_snapshot=budget_controller_snapshot,
            actual_refined_count=sum(1 for trace in refine_traces if trace.get("stage") == "clue_refine"),
            requested_max_refine=requested_max_refine,
            effective_max_refine=effective_max_refine,
            refine_budget_reasons=refine_budget_reasons,
            exploration_hypotheses=exploration_hypotheses,
        )

    def _build_execution_summary(
        self,
        *,
        run_state: _RunPlanningState,
        retrieval_state: _RetrievalState,
        semantic_state: _SemanticLocalState,
        live_state: _LiveCollectionState,
        fresh_state: _FreshProcessingState,
        refinement_state: _RefinementState,
    ) -> dict[str, Any]:
        used_clue_pool = bool(refinement_state.pool_clues_for_merge or (retrieval_state.retrieved_clues and not fresh_state.records))
        orchestration_route = self._orchestration_route(
            used_clue_pool=used_clue_pool,
            used_fresh_processing=bool(fresh_state.records),
            used_live_collection=bool(live_state.records),
            used_provided_records=bool(retrieval_state.provided_records),
            used_semantic_local=bool(semantic_state.records),
        )
        query_rewrite_count = sum(1 for item in live_state.selected_sources if item.get("query_rewrite_applied"))
        execution_summary = {
            **fresh_state.phase_payload,
            "status": "completed" if (fresh_state.records or semantic_state.clues) else "retrieved_from_clue_pool",
            "mode": self._execution_mode(
                used_clue_pool=used_clue_pool,
                used_fresh_processing=bool(fresh_state.records or semantic_state.clues),
            ),
            "budget": run_state.budget,
            "refined_clue_count": refinement_state.actual_refined_count,
            "query_rewrite_count": query_rewrite_count,
            "query_rewrite_fallback_count": sum(1 for item in live_state.selected_sources if item.get("query_rewrite_used_fallback")),
            "candidate_clue_hits": len(retrieval_state.retrieved_clues),
            "fresh_candidate_count": len(fresh_state.built_clues) + len(semantic_state.clues),
            "live_fresh_candidate_count": len(fresh_state.built_clues),
            "semantic_local_candidate_count": len(semantic_state.clues),
            "merged_candidate_count": len(refinement_state.merged_candidates),
            "used_clue_pool": used_clue_pool,
            "used_live_collection": bool(live_state.records),
            "used_provided_records": bool(retrieval_state.provided_records),
            "used_semantic_local_retrieval": bool(semantic_state.records),
            "semantic_local_summary": semantic_state.summary,
            "orchestration_route": orchestration_route,
            "live_collection_reasons": live_state.live_collection_reasons,
            "elapsed_budget_exhausted": self._deadline_exhausted(run_state.deadline_at),
            "runtime_quality_gate": run_state.runtime_quality_gate.model_dump(),
            "plan_execution_controls": run_state.plan_execution_controls.model_dump(),
            "requested_max_llm_refine_clues": refinement_state.requested_max_refine,
            "effective_max_llm_refine_clues": refinement_state.effective_max_refine,
            "refine_budget_reasons": refinement_state.refine_budget_reasons,
            "model_route_count": len(refinement_state.model_route_traces),
            "model_route_summary": self._summarize_model_routes(refinement_state.model_route_traces),
            "budget_controller": refinement_state.budget_controller_snapshot,
            "llm_gateway": self._summarize_gateway_stats(),
            "exploration_hypothesis_count": len(refinement_state.exploration_hypotheses),
            "collection_layers_executed": [
                str(item.get("collection_layer") or "")
                for item in live_state.collection_runs
                if item.get("fetched_count", 0) > 0
            ],
        }
        if run_state.effective_config.telemetry_enabled:
            execution_summary["telemetry"] = self._build_telemetry(
                started_at=run_state.started_at,
                budget=run_state.budget,
                requested_max_llm_refine_clues=refinement_state.requested_max_refine,
                effective_max_llm_refine_clues=refinement_state.effective_max_refine,
                selected_source_count=len(live_state.selected_sources),
                collected_record_count=len(live_state.records),
                provided_record_count=len(retrieval_state.provided_records),
                retrieved_clue_count=len(retrieval_state.retrieved_clues),
                merged_candidate_count=len(refinement_state.merged_candidates),
                refined_clue_count=refinement_state.actual_refined_count,
                rewrite_count=query_rewrite_count,
                used_live_collection=bool(live_state.records),
                used_clue_pool=used_clue_pool,
                elapsed_budget_exhausted=self._deadline_exhausted(run_state.deadline_at),
                semantic_local_record_count=len(semantic_state.records),
                model_route_traces=refinement_state.model_route_traces,
                budget_controller_snapshot=refinement_state.budget_controller_snapshot,
                llm_gateway_stats=self._gateway_stats(),
            )
        execution_summary["routing_profile"] = run_state.profile
        return self._mask_execution_summary(execution_summary)

    def _planner_runtime_context(self) -> dict[str, Any]:
        return self.phase_engine.runtime_prompt_context(include_candidates=True)

    def _semantic_local_limit(self, *, budget: Mapping[str, Any]) -> int:
        max_sources = int(budget.get("max_sources") or 1)
        max_raw_records = int(budget.get("max_raw_records") or 1)
        return min(max_raw_records, max(3, max_sources))

    def _cap_live_sources(
        self,
        selected_sources: list[dict[str, Any]],
        *,
        retrieved_summary: Mapping[str, Any] | None = None,
        config: InvestigationConfig | None = None,
    ) -> list[dict[str, Any]]:
        if int((retrieved_summary or {}).get("total_count") or 0) <= 0:
            return selected_sources
        active_config = config or self.investigation_config
        limit = max(1, int(active_config.max_live_sources_when_pool_hit or 1))
        return selected_sources[:limit] if len(selected_sources) > limit else selected_sources

    def _execution_mode(self, *, used_clue_pool: bool, used_fresh_processing: bool) -> str:
        if used_clue_pool and used_fresh_processing:
            return "hybrid_investigation"
        if used_fresh_processing:
            return "investigation_processing"
        return "candidate_clue_retrieval"

    def _orchestration_route(
        self,
        *,
        used_clue_pool: bool,
        used_fresh_processing: bool,
        used_live_collection: bool,
        used_provided_records: bool,
        used_semantic_local: bool,
    ) -> str:
        if used_clue_pool and used_live_collection:
            return "pool_plus_live_collection"
        if used_clue_pool and used_provided_records:
            return "pool_plus_provided_records"
        if used_clue_pool and used_semantic_local:
            return "pool_plus_semantic_local"
        if used_live_collection:
            return "live_collection_only"
        if used_provided_records:
            return "provided_records_only"
        if used_semantic_local:
            return "semantic_local_only"
        if used_fresh_processing:
            return "fresh_processing_only"
        return "clue_pool_only"

    def _should_collect_live_sources(
        self,
        *,
        config: InvestigationConfig,
        intent: Mapping[str, Any],
        quality_gate: RuntimeQualityGate,
        execution_controls: PlanExecutionControls,
        selected_sources: list[dict[str, Any]],
        retrieved_summary: Mapping[str, Any],
        retrieval_filters: Mapping[str, Any],
        collect_source_records: SourceCollector | None,
        has_provided_records: bool,
    ) -> tuple[bool, list[str]]:
        if has_provided_records or collect_source_records is None or not selected_sources:
            return False, []
        if not config.live_collection_enabled:
            return False, []
        if execution_controls.collection_mode == "pool_only":
            return False, ["plan_prefers_pool_only"]
        if execution_controls.collection_mode == "live_only":
            return True, ["plan_requires_live_collection"]
        reasons = self._live_collection_reasons_from_summary(
            config=config,
            intent=intent,
            quality_gate=quality_gate,
            retrieved_summary=retrieved_summary,
            retrieval_filters=retrieval_filters,
        )
        if not reasons:
            return False, []
        return True, reasons

    def _plan_execution_controls(self, plan: Mapping[str, Any]) -> PlanExecutionControls:
        strategy = plan.get("source_selection_strategy") if isinstance(plan.get("source_selection_strategy"), Mapping) else {}
        execution_notes = [str(item).strip().lower() for item in (plan.get("execution_notes") or []) if str(item).strip()]
        agent_actions = " ".join(
            str(item.get("action") or "").lower()
            for item in (plan.get("agent_steps") or [])
            if isinstance(item, Mapping)
        )
        collection_mode = str(strategy.get("collection_mode") or "adaptive").strip().lower()
        if collection_mode not in {"adaptive", "pool_only", "live_only", "hybrid"}:
            collection_mode = "adaptive"
        if "retrieve_clue_pool_only" in agent_actions or any("pool_only" in note for note in execution_notes):
            collection_mode = "pool_only"
        if "force_live_collection" in agent_actions or any("live_only" in note for note in execution_notes):
            collection_mode = "live_only"

        query_rewrite_policy = str(strategy.get("query_rewrite_policy") or "auto").strip().lower()
        if query_rewrite_policy != "off":
            query_rewrite_policy = "auto"
        if "skip_query_rewrite" in agent_actions or any("disable_query_rewrite" in note or "query_rewrite=off" in note for note in execution_notes):
            query_rewrite_policy = "off"

        refine_policy = "budgeted"
        if "skip_llm_refine" in agent_actions or any("refine_policy=off" in note or "skip_refine" in note for note in execution_notes):
            refine_policy = "off"

        return PlanExecutionControls(
            collection_mode=collection_mode,
            query_rewrite_policy=query_rewrite_policy,
            refine_policy=refine_policy,
        )

    def _apply_profile_execution_controls(
        self,
        controls: PlanExecutionControls,
        *,
        profile_config: Mapping[str, Any],
        profile: str,
    ) -> PlanExecutionControls:
        collection_mode = controls.collection_mode
        query_rewrite_policy = controls.query_rewrite_policy
        refine_policy = controls.refine_policy
        if not bool(profile_config.get("enable_live_collection", True)):
            collection_mode = "pool_only"
        if not bool(profile_config.get("enable_query_rewrite", True)):
            query_rewrite_policy = "off"
        if profile == "fast" and int(profile_config.get("max_llm_refine_clues") or 0) <= 0:
            refine_policy = "off"
        return PlanExecutionControls(
            collection_mode=collection_mode,
            query_rewrite_policy=query_rewrite_policy,
            refine_policy=refine_policy,
        )

    def _query_rewrite_skipped_traces(
        self,
        selected_sources: list[dict[str, Any]],
        *,
        reason: str,
    ) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        for source in selected_sources:
            source_name = str(source.get("source_name") or "unknown_source")
            source["query_rewrite_applied"] = False
            source["query_rewrite_used_fallback"] = False
            source["query_rewrite_reason"] = reason
            traces.append(
                {
                    "stage": "source_query_rewrite",
                    "source_name": source_name,
                    "llm_ok": False,
                    "used_fallback": False,
                    "applied": False,
                    "parsed_json": None,
                    "error": reason,
                }
            )
        return traces

    def _live_collection_reasons_from_summary(
        self,
        *,
        config: InvestigationConfig,
        intent: Mapping[str, Any],
        quality_gate: RuntimeQualityGate,
        retrieved_summary: Mapping[str, Any],
        retrieval_filters: Mapping[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        requested_hours = self._optional_positive_int(retrieval_filters.get("time_range_hours")) or self._optional_positive_int(
            intent.get("time_range_hours")
        )
        high_precision = quality_gate.quality_profile == "high_precision"
        require_cross_source = quality_gate.require_cross_source
        require_evidence_chain = quality_gate.require_evidence_chain
        if int(retrieved_summary.get("total_count") or 0) == 0:
            reasons.append("no_candidate_clues_in_pool")
        min_pool_high_quality = (
            config.high_precision_min_pool_high_quality_count
            if high_precision
            else config.balanced_min_pool_high_quality_count
        )
        if int(retrieved_summary.get("high_quality_count") or 0) < min_pool_high_quality:
            reasons.append("insufficient_high_quality_pool_clues")
        if requested_hours is not None and requested_hours <= config.short_window_hours and int(retrieved_summary.get("recent_count") or 0) == 0:
            reasons.append("need_fresh_signals_for_short_time_window")
        if requested_hours is not None and requested_hours <= config.short_window_hours and int(retrieved_summary.get("recent_high_quality_count") or 0) == 0:
            reasons.append("need_recent_high_quality_signals")
        if require_cross_source and int(retrieved_summary.get("max_cross_source_count") or 0) < config.min_cross_source_count:
            reasons.append("insufficient_cross_source_support")
        if require_evidence_chain and int(retrieved_summary.get("high_quality_count") or 0) < config.evidence_chain_min_pool_high_quality_count:
            reasons.append("evidence_chain_not_satisfied_by_pool")
        return reasons

    def _summarize_retrieved_clues(
        self,
        clues: list[dict[str, Any]],
        *,
        time_range_hours: int | None,
        quality_gate: RuntimeQualityGate,
    ) -> dict[str, int]:
        summary = {
            "total_count": len(clues),
            "high_quality_count": 0,
            "recent_count": 0,
            "recent_high_quality_count": 0,
            "max_cross_source_count": 0,
        }
        for clue in clues:
            cross_source_count = len({str(item) for item in (clue.get("source_names") or []) if str(item).strip()})
            summary["max_cross_source_count"] = max(summary["max_cross_source_count"], cross_source_count)
            is_high_quality = self._passes_runtime_quality_gate(clue, quality_gate=quality_gate)
            if is_high_quality:
                summary["high_quality_count"] += 1
            is_recent = self._clue_within_hours(clue, hours=time_range_hours)
            if is_recent:
                summary["recent_count"] += 1
                if is_high_quality:
                    summary["recent_high_quality_count"] += 1
        return summary

    def _clue_within_hours(self, clue: Mapping[str, Any], *, hours: int | None) -> bool:
        if hours is None:
            return True
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        for field in ("last_seen", "updated_at", "created_at"):
            value = clue.get(field)
            if not value:
                continue
            try:
                parsed = __import__("datetime").datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=__import__("datetime").timezone.utc)
            return now - parsed <= __import__("datetime").timedelta(hours=hours)
        return True

    def _select_sources(
        self,
        plan: Mapping[str, Any],
        available_sources: list[dict[str, Any]],
        *,
        max_sources: int | None,
        risk_types: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        if not available_sources:
            return []
        selected_names = {item.lower() for item in (plan.get("selected_source_names") or []) if str(item).strip()}
        strategy = plan.get("source_selection_strategy") or {}
        preferred_types = {
            _normalize_source_pref(item)
            for item in (strategy.get("preferred_source_types") or [])
            if _normalize_source_pref(item)
        }
        match_keywords = {str(item).lower() for item in (strategy.get("match_query_keywords") or []) if str(item).strip()}

        scored: list[tuple[int, dict[str, Any]]] = []
        for source in available_sources:
            score = 0
            source_name = str(source.get("source_name") or "").strip()
            source_type = _normalize_source_pref(source.get("source_type"))
            source_theme = str(source.get("query_theme") or "").lower()
            source_query = str(source.get("search_query") or "").lower()
            if source_name.lower() in selected_names:
                score += 4
            if source_type and source_type in preferred_types:
                score += 3
            if any(keyword in source_theme or keyword in source_query or keyword in source_name.lower() for keyword in match_keywords):
                score += 1
            scored.append((score, source))

        scored.sort(key=lambda item: (item[0], str(item[1].get("source_name") or "")), reverse=True)
        fallback = [dict(source) for _, source in scored]
        if max_sources is None or max_sources >= len(fallback):
            selected = fallback
        else:
            chosen = [dict(source) for score, source in scored if score > 0][:max_sources]
            if chosen:
                selected = chosen
            else:
                selected = fallback[:max_sources] if max_sources is not None and max_sources > 0 else fallback
        return prioritize_sources_for_investigation(
            selected,
            risk_types=risk_types,
            preferred_source_types=preferred_types,
            selected_source_names=selected_names,
        )

    def _resolve_budget(
        self,
        plan: Mapping[str, Any],
        *,
        explicit_max_sources: int | None,
        available_source_count: int = 0,
        policy_override: InvestigationPolicyOverride | None = None,
        profile_config: Mapping[str, Any] | None = None,
    ) -> dict[str, int | None]:
        raw = self._profile_budget_defaults(profile_config or {})
        plan_budget = plan.get("budget") or {}
        if not isinstance(plan_budget, Mapping):
            plan_budget = {}
        # Profile budgets are the runtime source of truth; the LLM plan can only
        # tighten them.  This keeps fast/balanced/high_recall cost and latency
        # caps real even when fallback plans carry larger defaults.
        for key in (
            "max_sources",
            "max_raw_records",
            "max_candidate_clues",
            "max_llm_refine_clues",
            "max_llm_calls",
            "max_llm_tokens",
            "max_llm_classify_records",
            "max_llm_extract_records",
            "max_query_rewrite_sources",
            "max_elapsed_seconds",
        ):
            if key not in plan_budget or plan_budget.get(key) in (None, ""):
                continue
            plan_value = self._optional_positive_int(plan_budget.get(key))
            if plan_value is None:
                continue
            current_value = raw.get(key)
            if current_value is None:
                raw[key] = plan_value
            else:
                raw[key] = min(int(current_value), plan_value)
        if policy_override is not None:
            raw = {
                **raw,
                **self._budget_override_payload(policy_override),
            }
        explicit_limit = explicit_max_sources if explicit_max_sources and explicit_max_sources > 0 else None
        resolved_max_sources = self._resolve_max_sources(
            raw.get("max_sources"),
            explicit_max_sources=explicit_limit,
            available_source_count=available_source_count,
        )
        candidate_budget_default = max(20, (resolved_max_sources or max(available_source_count, 1)) * 10)
        elapsed_budget = self._positive_int(
            raw.get("max_elapsed_seconds"),
            DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS,
        )
        budget = {
            "max_sources": resolved_max_sources,
            "max_raw_records": self._positive_int(raw.get("max_raw_records"), 5000),
            "max_candidate_clues": self._positive_int(raw.get("max_candidate_clues"), candidate_budget_default),
            "max_llm_refine_clues": self._positive_int(raw.get("max_llm_refine_clues"), 20),
            "max_llm_calls": self._positive_int(raw.get("max_llm_calls"), max(20, self._positive_int(raw.get("max_llm_refine_clues"), 20))),
            "max_llm_tokens": self._positive_int(raw.get("max_llm_tokens"), 20_000),
            "max_llm_classify_records": self._positive_int(raw.get("max_llm_classify_records"), 20),
            "max_llm_extract_records": self._positive_int(raw.get("max_llm_extract_records"), 20),
            "max_query_rewrite_sources": self._positive_int(raw.get("max_query_rewrite_sources"), 5),
            "max_elapsed_seconds": elapsed_budget,
        }
        if budget["max_sources"] is not None and available_source_count > 0:
            budget["max_sources"] = min(budget["max_sources"], available_source_count)
        if budget["max_sources"] is not None and explicit_limit is not None:
            budget["max_sources"] = min(budget["max_sources"], explicit_limit)
        budget["max_llm_refine_clues"] = min(budget["max_llm_refine_clues"], budget["max_candidate_clues"])
        return budget

    def _collect_records_from_sources(
        self,
        selected_sources: list[dict[str, Any]],
        *,
        collect_source_records: SourceCollector,
        max_raw_records: int,
        max_concurrent_sources: int,
        deadline_at: float | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        collection_runs: list[dict[str, Any]] = []
        collected_records: list[dict[str, Any]] = []
        if not selected_sources or max_raw_records <= 0:
            return collected_records, collection_runs

        grouped_sources = group_sources_by_collection_layer(selected_sources)
        worker_count = max(1, int(max_concurrent_sources or 1))

        for layer_name, layer_sources in grouped_sources:
            if self._deadline_exhausted(deadline_at):
                break
            if len(collected_records) >= max_raw_records:
                break
            layer_records, layer_runs = self._collect_layer_records(
                layer_name,
                layer_sources,
                collect_source_records=collect_source_records,
                remaining_budget=max_raw_records - len(collected_records),
                max_concurrent_sources=worker_count,
                deadline_at=deadline_at,
            )
            collected_records.extend(layer_records)
            collection_runs.extend(layer_runs)
            if len(collected_records) >= max_raw_records:
                break
        return collected_records, collection_runs

    def _collect_layer_records(
        self,
        layer_name: str,
        layer_sources: list[dict[str, Any]],
        *,
        collect_source_records: SourceCollector,
        remaining_budget: int,
        max_concurrent_sources: int,
        deadline_at: float | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not layer_sources or remaining_budget <= 0 or self._deadline_exhausted(deadline_at):
            return [], []

        worker_count = max(1, min(int(max_concurrent_sources or 1), len(layer_sources)))
        if worker_count == 1:
            results = [
                self._collect_one_source(
                    layer_name,
                    source,
                    collect_source_records=collect_source_records,
                )
                for source in layer_sources
                if not self._deadline_exhausted(deadline_at)
            ]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(
                        self._collect_one_source,
                        layer_name,
                        source,
                        collect_source_records=collect_source_records,
                    )
                    for source in layer_sources
                ]
                results = [future.result() for future in futures]

        layer_records: list[dict[str, Any]] = []
        layer_runs: list[dict[str, Any]] = []
        for records, run in results:
            if len(layer_records) >= remaining_budget:
                break
            allowed = remaining_budget - len(layer_records)
            accepted_records = records[:allowed]
            run["fetched_count"] = len(accepted_records)
            layer_records.extend(accepted_records)
            layer_runs.append(run)
        return layer_records, layer_runs

    @staticmethod
    def _collect_one_source(
        layer_name: str,
        source: Mapping[str, Any],
        *,
        collect_source_records: SourceCollector,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        source_payload = dict(source)
        source_payload.setdefault("collection_layer", layer_name)
        run = {
            "source_name": str(source_payload.get("source_name") or "unknown_source"),
            "source_type": str(source_payload.get("source_type") or ""),
            "collection_layer": layer_name,
            "fetched_count": 0,
            "error": None,
        }
        try:
            raw_records = collect_source_records(source_payload)
        except Exception as exc:  # pragma: no cover - exercised through API collectors.
            run["error"] = str(exc)
            return [], run

        records: list[dict[str, Any]] = []
        for record in raw_records or []:
            item = dict(record) if isinstance(record, Mapping) else {"value": record}
            item.setdefault("source_name", source_payload.get("source_name"))
            item.setdefault("source_type", source_payload.get("source_type"))
            item.setdefault("source_url", source_payload.get("source_url"))
            records.append(item)
        run["fetched_count"] = len(records)
        return records, run

    def _resolve_max_sources(
        self,
        value: Any,
        *,
        explicit_max_sources: int | None,
        available_source_count: int,
    ) -> int | None:
        if explicit_max_sources is not None:
            return explicit_max_sources
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed > 0:
            return parsed
        if available_source_count > 0:
            return available_source_count
        return None

    def _rewrite_selected_sources(
        self,
        selected_sources: list[dict[str, Any]],
        *,
        query: str,
        intent: Mapping[str, Any],
        plan: Mapping[str, Any],
        runtime_context: Mapping[str, Any] | None = None,
        max_rewrite_sources: int | None = None,
        budget: BudgetController | None = None,
        deadline_ms: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rewritten_sources: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        rewrite_limit = max(0, int(max_rewrite_sources if max_rewrite_sources is not None else len(selected_sources)))
        rewrite_targets = selected_sources[:rewrite_limit]
        untouched_sources = selected_sources[rewrite_limit:]
        for source in rewrite_targets:
            rewritten_source, trace = self.query_rewriter.rewrite(
                source,
                query=query,
                intent=intent,
                plan=plan,
                runtime_context=runtime_context,
                budget=budget,
                deadline_ms=deadline_ms,
            )
            rewritten_sources.append(rewritten_source)
            traces.append(trace.model_dump())
        if untouched_sources:
            rewritten_sources.extend(untouched_sources)
            traces.extend(self._query_rewrite_skipped_traces(untouched_sources, reason="query_rewrite_source_limit_reached"))
        return rewritten_sources, traces

    def _refine_retrieved_clues(
        self,
        clues: list[dict[str, Any]],
        *,
        query: str,
        intent: Mapping[str, Any],
        quality_gate: RuntimeQualityGate,
        max_refine: int,
        deadline_at: float | None = None,
        routing_profile: str | None = None,
        budget_controller: BudgetController | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        refined: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        model_route_traces: list[dict[str, Any]] = []
        active_budget_controller = budget_controller or BudgetController(RuntimeBudget(max_llm_refine_clues=max_refine))
        routed_clues = self.clue_ranker.rank(clues)
        active_router = self.model_router.with_profile(routing_profile)
        pending_refine: list[dict[str, Any]] = []
        pending_meta: list[dict[str, Any]] = []
        for clue in routed_clues:
            item = dict(clue)
            route_decision = active_router.decide_clue_refinement(item)
            route_trace = {
                "stage": "model_route",
                "route_target": "clue_refine",
                "clue_id": str(item.get("clue_id") or "unknown_clue"),
                **route_decision.model_dump(),
            }
            model_route_traces.append(route_trace)
            if route_decision.action == "llm_refine_only":
                item["model_route"] = route_decision.model_dump()
            should_refine = (
                route_decision.action == "llm_refine_only"
                and len(pending_refine) < max_refine
                and not self._deadline_exhausted(deadline_at)
                and active_budget_controller.allow_llm_call(
                    stage="clue_refine",
                    estimated_tokens=route_decision.max_tokens,
                    item_count=1,
                )
            )
            if should_refine:
                pending_refine.append(item)
                pending_meta.append(route_decision.model_dump())
            elif route_decision.action == "llm_refine_only":
                route_trace["skipped_reason"] = (
                    "elapsed_budget_exhausted"
                    if self._deadline_exhausted(deadline_at)
                    else "llm_refine_budget_exhausted"
                    if len(pending_refine) >= max_refine
                    else "budget_controller_denied"
                )
            refined.append(item)
        if pending_refine:
            max_tokens = sum(int(meta.get("max_tokens") or 0) for meta in pending_meta) or 900
            deadline_ms = max(int(meta.get("deadline_ms") or 0) for meta in pending_meta) or None
            runtime_context = self.phase_engine.runtime_prompt_context(
                label=self._runtime_context_label(intent),
                include_candidates=True,
            )
            enriched_batch, batch_traces = self.clue_refiner.refine_batch(
                pending_refine,
                query=query,
                intent=intent,
                runtime_context=runtime_context,
                max_tokens=max_tokens,
                deadline_ms=deadline_ms,
                budget=active_budget_controller,
            )
            by_clue_id = {str(item.get("clue_id") or ""): item for item in enriched_batch}
            meta_by_clue_id = {
                str(item.get("clue_id") or ""): meta
                for item, meta in zip(pending_refine, pending_meta, strict=False)
            }
            refined = [by_clue_id.get(str(item.get("clue_id") or ""), item) for item in refined]
            for trace in batch_traces:
                meta = meta_by_clue_id.get(str(trace.get("clue_id") or ""), {})
                trace["model_route_reason"] = meta.get("reason")
                trace["model_route_priority"] = meta.get("priority")
                trace["max_tokens_budgeted"] = meta.get("max_tokens")
            traces.extend(batch_traces)
        for item in refined:
            self.clue_repo.save(item)
        high_quality = [clue for clue in refined if self._passes_runtime_quality_gate(clue, quality_gate=quality_gate)]
        candidates = [clue for clue in refined if clue not in high_quality]
        return high_quality, candidates, traces, model_route_traces, active_budget_controller.snapshot()

    def _runtime_quality_gate(
        self,
        *,
        intent: Mapping[str, Any],
        plan: Mapping[str, Any],
        policy_override: InvestigationPolicyOverride | None,
    ) -> RuntimeQualityGate:
        plan_gate = plan.get("quality_gate") if isinstance(plan.get("quality_gate"), Mapping) else {}
        quality_profile = str(
            plan_gate.get("quality_profile")
            or intent.get("quality_profile")
            or "balanced"
        ).strip().lower()
        if quality_profile not in {"balanced", "high_precision", "high_recall"}:
            quality_profile = "balanced"
        minimum_quality_score = self._runtime_minimum_quality_score(
            quality_profile=quality_profile,
            plan_gate=plan_gate,
            policy_override=policy_override,
        )
        require_cross_source = self._runtime_gate_bool(
            override_value=getattr(policy_override, "require_cross_source", None) if policy_override else None,
            plan_value=plan_gate.get("require_cross_source"),
            intent_value=intent.get("require_cross_source"),
            default=False,
        )
        require_evidence_chain = self._runtime_gate_bool(
            override_value=getattr(policy_override, "require_evidence_chain", None) if policy_override else None,
            plan_value=plan_gate.get("require_evidence_chain"),
            intent_value=intent.get("require_evidence_chain"),
            default=True,
        )
        return RuntimeQualityGate(
            quality_profile=quality_profile,
            minimum_quality_score=minimum_quality_score,
            require_cross_source=require_cross_source,
            require_evidence_chain=require_evidence_chain,
        )

    def _runtime_minimum_quality_score(
        self,
        *,
        quality_profile: str,
        plan_gate: Mapping[str, Any],
        policy_override: InvestigationPolicyOverride | None,
    ) -> float:
        default_threshold = {
            "high_precision": 0.78,
            "high_recall": 0.52,
            "balanced": 0.65,
        }.get(quality_profile, 0.65)
        if policy_override and policy_override.minimum_quality_score is not None:
            return round(float(policy_override.minimum_quality_score), 4)
        plan_value = self._optional_float(plan_gate.get("minimum_quality_score"))
        if plan_value is not None:
            return round(plan_value, 4)
        return round(default_threshold, 4)

    @staticmethod
    def _runtime_gate_bool(
        *,
        override_value: bool | None,
        plan_value: Any,
        intent_value: Any,
        default: bool,
    ) -> bool:
        for value in (override_value, plan_value, intent_value):
            if isinstance(value, bool):
                return value
            if value is None:
                continue
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
        return default

    def _passes_runtime_quality_gate(
        self,
        clue: Mapping[str, Any],
        *,
        quality_gate: RuntimeQualityGate,
    ) -> bool:
        quality = clue.get("quality") or {}
        quality_score = float(clue.get("quality_score") or 0.0)
        if quality_score < quality_gate.minimum_quality_score:
            return False
        cross_source_count = self._cross_source_count(clue)
        if quality_gate.require_cross_source and cross_source_count < 2:
            return False
        evidence_count = self._evidence_count(clue)
        if quality_gate.require_evidence_chain and evidence_count < 2:
            return False
        if "pass_threshold" in quality and bool(quality.get("pass_threshold")) is False and quality_score < quality_gate.minimum_quality_score:
            return False
        return True

    @staticmethod
    def _cross_source_count(clue: Mapping[str, Any]) -> int:
        return len({str(item) for item in (clue.get("source_names") or []) if str(item).strip()})

    @staticmethod
    def _evidence_count(clue: Mapping[str, Any]) -> int:
        return len({str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()})

    def _merge_candidate_clues(
        self,
        *,
        pool_clues: list[dict[str, Any]],
        fresh_clues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for origin, clues in (("clue_pool", pool_clues), ("fresh_processing", fresh_clues)):
            for clue in clues:
                item = dict(clue)
                item["orchestration_origin"] = origin
                item["orchestration_origins"] = [origin]
                merge_key = self._candidate_merge_key(item)
                if merge_key not in merged:
                    merged[merge_key] = item
                    continue
                merged[merge_key] = self._combine_candidate_clues(merged[merge_key], item)
        return self._sort_candidate_clues(list(merged.values()))

    def _sort_candidate_clues(self, clues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = [dict(clue) for clue in clues]
        ordered.sort(key=self._candidate_rank, reverse=True)
        return ordered

    def _candidate_merge_key(self, clue: Mapping[str, Any]) -> str:
        clue_type = str(clue.get("clue_type") or "").strip().lower()
        key = str(clue.get("key") or "").strip().lower()
        risk_category = str(clue.get("risk_category") or "").strip().lower()
        if clue_type and key:
            return f"{clue_type}|{key}|{risk_category}"
        clue_id = str(clue.get("clue_id") or "").strip()
        return clue_id or f"fallback|{risk_category}|{key}"

    def _combine_candidate_clues(self, existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        origins = {
            *[str(item) for item in (existing.get("orchestration_origins") or []) if str(item).strip()],
            *[str(item) for item in (candidate.get("orchestration_origins") or []) if str(item).strip()],
        }
        fields_to_merge = ("evidence_trace_ids", "source_names", "entity_values", "source_types")
        base = dict(existing if self._candidate_rank(existing) >= self._candidate_rank(candidate) else candidate)
        for field in fields_to_merge:
            merged_values = []
            seen: set[str] = set()
            for item in [*(existing.get(field) or []), *(candidate.get(field) or [])]:
                text = str(item).strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                merged_values.append(text)
            if merged_values:
                base[field] = merged_values
        base["quality_score"] = max(float(existing.get("quality_score") or 0.0), float(candidate.get("quality_score") or 0.0))
        base["confidence"] = max(float(existing.get("confidence") or 0.0), float(candidate.get("confidence") or 0.0))
        base["retrieval_score"] = max(float(existing.get("retrieval_score") or 0.0), float(candidate.get("retrieval_score") or 0.0))
        base["orchestration_origins"] = sorted(origins)
        base["orchestration_origin"] = "hybrid" if len(origins) > 1 else next(iter(origins), base.get("orchestration_origin"))
        return base

    def _candidate_rank(self, clue: Mapping[str, Any]) -> tuple[int, float, float, float, int]:
        origins = {str(item) for item in (clue.get("orchestration_origins") or []) if str(item).strip()}
        return (
            1 if "fresh_processing" in origins else 0,
            float(clue.get("quality_score") or 0.0),
            float(clue.get("confidence") or 0.0),
            float(clue.get("retrieval_score") or 0.0),
            len(clue.get("evidence_trace_ids") or []),
        )

    def _build_telemetry(
        self,
        *,
        started_at: float,
        budget: Mapping[str, Any],
        requested_max_llm_refine_clues: int,
        effective_max_llm_refine_clues: int,
        selected_source_count: int,
        collected_record_count: int,
        provided_record_count: int,
        retrieved_clue_count: int,
        merged_candidate_count: int,
        refined_clue_count: int,
        rewrite_count: int,
        used_live_collection: bool,
        used_clue_pool: bool,
        elapsed_budget_exhausted: bool,
        semantic_local_record_count: int,
        model_route_traces: list[dict[str, Any]] | None = None,
        budget_controller_snapshot: Mapping[str, Any] | None = None,
        llm_gateway_stats: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        max_sources = budget.get("max_sources")
        max_candidate_clues = int(budget.get("max_candidate_clues") or 0)
        effective_refine_budget = max(0, int(effective_max_llm_refine_clues or 0))
        retrieval_fill_ratio = round(min(retrieved_clue_count / max(max_candidate_clues, 1), 1.0), 4) if max_candidate_clues else 0.0
        refine_budget_utilization = (
            round(min(refined_clue_count / max(effective_refine_budget, 1), 1.0), 4)
            if effective_refine_budget
            else 0.0
        )
        source_budget_utilization = (
            round(min(selected_source_count / max(int(max_sources), 1), 1.0), 4)
            if isinstance(max_sources, int) and max_sources > 0
            else None
        )
        telemetry = {
            "elapsed_ms": elapsed_ms,
            "selected_source_count": selected_source_count,
            "collected_record_count": collected_record_count,
            "provided_record_count": provided_record_count,
            "semantic_local_record_count": semantic_local_record_count,
            "retrieved_clue_count": retrieved_clue_count,
            "merged_candidate_count": merged_candidate_count,
            "refined_clue_count": refined_clue_count,
            "query_rewrite_count": rewrite_count,
            "requested_max_llm_refine_clues": requested_max_llm_refine_clues,
            "effective_max_llm_refine_clues": effective_refine_budget,
            "retrieval_fill_ratio": retrieval_fill_ratio,
            "refine_budget_utilization": refine_budget_utilization,
            "source_budget_utilization": source_budget_utilization,
            "used_live_collection": used_live_collection,
            "used_clue_pool": used_clue_pool,
            "elapsed_budget_exhausted": elapsed_budget_exhausted,
            "model_route_summary": self._summarize_model_routes(model_route_traces or []),
            "budget_controller": dict(budget_controller_snapshot or {}),
            "llm": self._summarize_gateway_stats(llm_gateway_stats),
        }
        return telemetry

    @staticmethod
    def _summarize_model_routes(model_route_traces: list[dict[str, Any]]) -> dict[str, int]:
        summary: dict[str, int] = {}
        for trace in model_route_traces:
            action = str(trace.get("action") or "unknown")
            summary[action] = summary.get(action, 0) + 1
        return summary

    def _gateway_stats(self) -> list[dict[str, Any]]:
        if hasattr(self.llm_gateway, "stats"):
            return [dict(item) for item in self.llm_gateway.stats()]
        return []

    def _summarize_gateway_stats(self, stats: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        items = [dict(item) for item in (stats if stats is not None else self._gateway_stats())]
        by_stage: dict[str, dict[str, Any]] = {}
        for item in items:
            stage = str(item.get("stage") or "unknown")
            bucket = by_stage.setdefault(
                stage,
                {
                    "call_count": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "cache_hit_count": 0,
                    "estimated_tokens": 0,
                    "elapsed_ms": 0,
                },
            )
            bucket["call_count"] += 1
            if bool(item.get("ok")):
                bucket["success_count"] += 1
            else:
                bucket["failed_count"] += 1
            if bool(item.get("cache_hit")):
                bucket["cache_hit_count"] += 1
            bucket["estimated_tokens"] += int(item.get("prompt_tokens_estimated") or 0) + int(item.get("completion_tokens_limit") or 0)
            bucket["elapsed_ms"] += int(item.get("elapsed_ms") or 0)
        return {
            "call_count": len(items),
            "success_count": sum(1 for item in items if bool(item.get("ok"))),
            "failed_count": sum(1 for item in items if not bool(item.get("ok"))),
            "cache_hit_count": sum(1 for item in items if bool(item.get("cache_hit"))),
            "estimated_tokens": sum(
                int(item.get("prompt_tokens_estimated") or 0) + int(item.get("completion_tokens_limit") or 0)
                for item in items
            ),
            "by_stage": by_stage,
        }

    def _collect_semantic_local_records(
        self,
        *,
        query: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if limit <= 0:
            return [], []
        results = self.phase_engine.semantic_search(query, top_k=limit)
        records: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        seen: set[str] = set()
        direct_trace_ids: list[str] = []
        for item in results:
            trace_id = str(item.get("item_id") or item.get("metadata", {}).get("trace_id") or "").strip()
            if not trace_id or trace_id in seen:
                continue
            record = self.phase_engine.get_cached_record(trace_id)
            if record is None:
                continue
            seen.add(trace_id)
            direct_trace_ids.append(trace_id)
            records.append(record)
            traces.append(
                {
                    "stage": "semantic_local_retrieval",
                    "trace_id": trace_id,
                    "score": float(item.get("score") or 0.0),
                    "metadata": dict(item.get("metadata") or {}),
                }
            )
        expanded_trace_ids = self.phase_engine.expand_related_trace_ids(
            direct_trace_ids,
            limit=max(limit + 3, len(direct_trace_ids) + 3),
        )
        graph_only_count = 0
        for trace_id in expanded_trace_ids:
            if trace_id in seen:
                continue
            record = self.phase_engine.get_cached_record(trace_id)
            if record is None:
                continue
            seen.add(trace_id)
            graph_only_count += 1
            records.append(record)
            traces.append(
                {
                    "stage": "semantic_graph_expansion",
                    "trace_id": trace_id,
                    "source": "graph_neighbors",
                }
            )
        if traces:
            traces.append(
                {
                    "stage": "semantic_local_summary",
                    "direct_hit_count": len(direct_trace_ids),
                    "graph_expanded_count": graph_only_count,
                    "record_count": len(records),
                }
            )
        return records, traces

    @staticmethod
    def _merge_retrieved_summary(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, int]:
        merged: dict[str, int] = {}
        for key in ("total_count", "high_quality_count", "recent_count", "recent_high_quality_count"):
            merged[key] = int(base.get(key) or 0) + int(extra.get(key) or 0)
        merged["max_cross_source_count"] = max(int(base.get("max_cross_source_count") or 0), int(extra.get("max_cross_source_count") or 0))
        return merged

    def _build_exploration_hypotheses(
        self,
        *,
        query: str,
        processed_records: list[dict[str, Any]],
        candidate_clues: list[dict[str, Any]],
        high_quality_clues: list[dict[str, Any]],
        runtime_quality_gate: RuntimeQualityGate,
    ) -> list[dict[str, Any]]:
        phase_payload = self.phase_engine.last_run_payload() or {}
        entities = phase_payload.get("entities") if isinstance(phase_payload.get("entities"), list) else []
        classifications = phase_payload.get("classifications") if isinstance(phase_payload.get("classifications"), list) else []
        if not self._should_run_controlled_exploration(
            processed_records=processed_records,
            high_quality_clues=high_quality_clues,
            candidate_clues=candidate_clues,
            classifications=classifications,
            entities=entities,
            runtime_quality_gate=runtime_quality_gate,
        ):
            return []
        entity_by_trace: dict[str, list[dict[str, Any]]] = {}
        for entity in entities:
            trace_id = str(entity.get("source_trace_id") or "").strip()
            if not trace_id:
                continue
            entity_by_trace.setdefault(trace_id, []).append(dict(entity))
        classification_by_trace: dict[str, dict[str, Any]] = {}
        for item in classifications:
            trace_id = str(item.get("source_trace_id") or "").strip()
            if trace_id:
                classification_by_trace[trace_id] = dict(item)
        prompt_context = self.phase_engine.runtime_prompt_context(
            label=self._exploration_label(candidate_clues, runtime_quality_gate=runtime_quality_gate),
        )
        prompt_context["history"] = processed_records[-12:]
        hypotheses: list[dict[str, Any]] = []
        explored_trace_ids: set[str] = set()
        for record in processed_records:
            trace_id = str(record.get("source_trace_id") or record.get("trace_id") or "").strip()
            if not trace_id or trace_id in explored_trace_ids:
                continue
            explored_trace_ids.add(trace_id)
            hypothesis = self.exploration_agent.analyze(
                raw=record,
                classification=classification_by_trace.get(trace_id),
                entities=entity_by_trace.get(trace_id, []),
                context=prompt_context,
            )
            stored = self.review_repo.add_hypothesis(hypothesis)
            payload = stored.model_dump(mode="json") if hasattr(stored, "model_dump") else dict(stored)
            payload["source"] = "controlled_exploration"
            payload["query"] = query
            hypotheses.append(payload)
        return hypotheses

    def _should_run_controlled_exploration(
        self,
        *,
        processed_records: list[dict[str, Any]],
        high_quality_clues: list[dict[str, Any]],
        candidate_clues: list[dict[str, Any]],
        classifications: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        runtime_quality_gate: RuntimeQualityGate,
    ) -> bool:
        if not processed_records:
            return False
        if not high_quality_clues:
            return True
        if candidate_clues:
            return True
        if any(self._classification_needs_exploration(item) for item in classifications):
            return True
        if any(str(item.get("entity_type") or "").strip().lower() == "slang_term" for item in entities):
            return True
        if runtime_quality_gate.quality_profile == "high_recall":
            return True
        return False

    @staticmethod
    def _classification_needs_exploration(classification: Mapping[str, Any]) -> bool:
        confidence = float(classification.get("confidence") or 0.0)
        conflict_status = str(classification.get("conflict_status") or "").strip().upper()
        secondary_label = str(classification.get("secondary_label") or "").strip()
        risk_category = str(classification.get("risk_category") or "").strip().lower()
        if bool(classification.get("review_required")):
            return True
        if confidence < 0.72:
            return True
        if conflict_status == "CONFLICT_REVIEW":
            return True
        if secondary_label in {"未细分", "待研判"}:
            return True
        if risk_category in {"unknown", "unknown_risk_pattern"}:
            return True
        return False

    @staticmethod
    def _exploration_label(
        candidate_clues: list[dict[str, Any]],
        *,
        runtime_quality_gate: RuntimeQualityGate,
    ) -> str:
        for clue in candidate_clues:
            risk_category = str(clue.get("risk_category") or "").strip()
            if risk_category:
                return risk_category
        return runtime_quality_gate.quality_profile

    @staticmethod
    def _runtime_context_label(payload: Mapping[str, Any]) -> str:
        risk_types = payload.get("risk_types") if isinstance(payload.get("risk_types"), list) else []
        if risk_types:
            first = str(risk_types[0]).strip()
            if first:
                return first
        include_keywords = payload.get("include_keywords") if isinstance(payload.get("include_keywords"), list) else []
        if include_keywords:
            first = str(include_keywords[0]).strip()
            if first:
                return first
        return str(payload.get("goal") or payload.get("quality_profile") or "balanced").strip()

    def _effective_investigation_config(
        self,
        *,
        routing_profile: str | None,
        policy_override: InvestigationPolicyOverride | None,
    ) -> InvestigationConfig:
        config = self.investigation_config.model_copy(deep=True)
        profile = self._normalize_routing_profile(routing_profile)
        profile_overrides = self._profile_overrides(profile)
        if profile_overrides:
            config = config.model_copy(update=profile_overrides)
        if policy_override:
            override_payload = policy_override.model_dump(exclude_none=True)
            if override_payload:
                config = config.model_copy(update=override_payload)
        return config

    @staticmethod
    def _normalize_policy_override(
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None,
    ) -> InvestigationPolicyOverride | None:
        if not policy_override:
            return None
        if isinstance(policy_override, InvestigationPolicyOverride):
            return policy_override
        return InvestigationPolicyOverride.model_validate(policy_override)

    @staticmethod
    def _budget_override_payload(policy_override: InvestigationPolicyOverride) -> dict[str, int]:
        payload = policy_override.model_dump(exclude_none=True)
        return {
            key: int(payload[key])
            for key in (
                "max_sources",
                "max_raw_records",
                "max_candidate_clues",
                "max_llm_refine_clues",
                "max_elapsed_seconds",
            )
            if key in payload
        }

    @staticmethod
    def _deadline_at(started_at: float, max_elapsed_seconds: int | None) -> float | None:
        if max_elapsed_seconds is None:
            return None
        return started_at + float(max_elapsed_seconds)

    @staticmethod
    def _deadline_exhausted(deadline_at: float | None) -> bool:
        return deadline_at is not None and time.perf_counter() >= deadline_at

    @staticmethod
    def _normalize_routing_profile(value: str | None) -> str:
        text = str(value or "").strip().lower()
        if text in {"fast", "latency", "low_latency"}:
            return "fast"
        if text in {"high_recall", "recall", "quality"}:
            return "high_recall"
        return "balanced"

    def _profile_overrides(self, profile: str) -> dict[str, Any]:
        profile_config = self._routing_profile_config(profile)
        if profile == "fast":
            return {
                "live_collection_enabled": bool(profile_config.get("enable_live_collection", False)),
                "balanced_min_pool_high_quality_count": 1,
                "high_precision_min_pool_high_quality_count": 1,
                "min_cross_source_count": 2,
                "max_live_sources_when_pool_hit": 1,
                "retrieval_score_threshold_for_pool_merge": 0.25,
            }
        if profile == "high_recall":
            return {
                "live_collection_enabled": bool(profile_config.get("enable_live_collection", True)),
                "balanced_min_pool_high_quality_count": 2,
                "high_precision_min_pool_high_quality_count": 3,
                "min_cross_source_count": 3,
                "max_live_sources_when_pool_hit": max(3, self.investigation_config.max_live_sources_when_pool_hit),
                "retrieval_score_threshold_for_pool_merge": 0.0,
            }
        if profile_config:
            return {"live_collection_enabled": bool(profile_config.get("enable_live_collection", True))}
        return {}

    def _routing_profile_config(self, profile: str) -> dict[str, Any]:
        default_profiles = {
            "fast": {
                "max_elapsed_seconds": 8,
                "max_sources": 1,
                "max_raw_records": 500,
                "max_candidate_clues": 20,
                "max_llm_calls": 3,
                "max_llm_tokens": 3000,
                "max_llm_classify_records": 5,
                "max_llm_extract_records": 5,
                "max_llm_refine_clues": 2,
                "max_query_rewrite_sources": 0,
                "enable_llm_intent_parse": False,
                "enable_query_rewrite": False,
                "enable_live_collection": False,
                "prefer_clue_pool": True,
                "min_rule_confidence_for_auto_accept": 0.82,
            },
            "balanced": {
                "max_elapsed_seconds": 30,
                "max_sources": 2,
                "max_raw_records": 3000,
                "max_candidate_clues": 50,
                "max_llm_calls": 10,
                "max_llm_tokens": 10000,
                "max_llm_classify_records": 20,
                "max_llm_extract_records": 20,
                "max_llm_refine_clues": 6,
                "max_query_rewrite_sources": 2,
                "enable_llm_intent_parse": True,
                "enable_query_rewrite": True,
                "enable_live_collection": True,
                "prefer_clue_pool": True,
                "min_rule_confidence_for_auto_accept": 0.85,
            },
            "high_recall": {
                "max_elapsed_seconds": 180,
                "max_sources": 5,
                "max_raw_records": 20000,
                "max_candidate_clues": 200,
                "max_llm_calls": 40,
                "max_llm_tokens": 50000,
                "max_llm_classify_records": 100,
                "max_llm_extract_records": 100,
                "max_llm_refine_clues": 20,
                "max_query_rewrite_sources": 5,
                "enable_llm_intent_parse": True,
                "enable_query_rewrite": True,
                "enable_live_collection": True,
                "prefer_clue_pool": False,
                "min_rule_confidence_for_auto_accept": 0.78,
            },
        }
        configured = self.routing_profiles.get(profile)
        if configured is None:
            configured_payload: Mapping[str, Any] = {}
        elif hasattr(configured, "model_dump"):
            configured_payload = configured.model_dump()
        elif isinstance(configured, Mapping):
            configured_payload = configured
        else:
            configured_payload = {}
        return {**default_profiles.get(profile, default_profiles["balanced"]), **dict(configured_payload)}

    @staticmethod
    def _profile_budget_defaults(profile_config: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "max_elapsed_seconds": profile_config.get(
                "max_elapsed_seconds",
                DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS,
            ),
            "max_sources": profile_config.get("max_sources"),
            "max_raw_records": profile_config.get("max_raw_records", 5000),
            "max_candidate_clues": profile_config.get("max_candidate_clues", 100),
            "max_llm_calls": profile_config.get("max_llm_calls", 20),
            "max_llm_tokens": profile_config.get("max_llm_tokens", 20000),
            "max_llm_classify_records": profile_config.get("max_llm_classify_records", 20),
            "max_llm_extract_records": profile_config.get("max_llm_extract_records", 20),
            "max_llm_refine_clues": profile_config.get("max_llm_refine_clues", 20),
            "max_query_rewrite_sources": profile_config.get("max_query_rewrite_sources", 5),
        }

    @staticmethod
    def _stage_deadline_ms(profile_config: Mapping[str, Any], *, default: int) -> int:
        elapsed = int(profile_config.get("max_elapsed_seconds") or 0)
        if elapsed <= 0:
            return default
        return max(250, min(default, int(elapsed * 1000 / 4)))

    @staticmethod
    def _disabled_llm_trace(stage: str, *, reason: str, runtime_context: Mapping[str, Any] | None = None) -> Any:
        runtime_context = dict(runtime_context or {})
        return type(
            "DisabledLLMTrace",
            (),
            {
                "model_dump": lambda self: {
                    "stage": stage,
                    "llm_ok": False,
                    "used_fallback": True,
                    "parsed_json": {
                        "reason": reason,
                        "runtime_slang_term_count": len(runtime_context.get("slang_terms", []) or []),
                        "runtime_few_shot_count": len(runtime_context.get("few_shot_examples", []) or []),
                    },
                    "error": reason,
                }
            },
        )()

    @staticmethod
    def _mask_execution_summary(summary: dict[str, Any]) -> dict[str, Any]:
        masker = PIIMasker()

        def mask(value: Any) -> Any:
            if isinstance(value, str):
                return masker.mask_text(value)
            if isinstance(value, list):
                return [mask(item) for item in value]
            if isinstance(value, dict):
                return {key: mask(item) for key, item in value.items()}
            return value

        return mask(summary)

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= 0 else default

    @staticmethod
    def _optional_positive_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed


def _normalize_source_pref(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"telegram", "tg", "电报"}:
        return "telegram"
    if text in {"forum", "论坛", "贴吧"}:
        return "forum"
    if text in {"im", "chat", "群", "私聊"}:
        return "im"
    return text


def _as_investigation_processing_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose local processing as one integrated step in the investigation flow."""

    normalized = dict(payload)
    normalized["mode"] = "investigation_processing"
    return normalized


__all__ = ["InvestigationOrchestrator", "InvestigationRunResult"]
