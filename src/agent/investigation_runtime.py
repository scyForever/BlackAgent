"""Thin runtime shell for BlackAgent investigation orchestration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

from src.config_loader import InvestigationConfig, InvestigationPolicyOverride
from src.backend import LLMGateway
from src.enhancement.clue_quality import ClueQualityEvaluator
from src.enhancement.engine import PhaseTwoThreeEngine
from src.enhancement.llm_clue_refiner import LLMClueRefiner
from src.evaluation.llm_ablation import load_latest_llm_value_report
from src.pipeline import OfflineClueBuilder
from src.retrieval import ClueRetriever
from src.safety.source_policy_guard import SourcePolicyGuard
from src.workflows import InvestigationWorkflow
from storage.entity_graph import EntityGraphStore
from storage import ClueRepo, InMemoryClueRepo, InMemoryReviewRepo

from .clue_ranker import ClueRanker
from .exploration_agent import ExplorationAgent
from .investigation_contracts import (
    EvidenceGap,
    InvestigationRunResult,
    PlanExecutionControls,
    RuntimeQualityGate,
    SourceCollector,
    _FreshProcessingState,
    _LiveCollectionState,
    _RefinementState,
    _RetrievalState,
    _RunPlanningState,
    _SemanticLocalState,
)
from .investigation_runtime_mixins import InvestigationRuntimeMixin
from .model_router import ModelRouter
from .query_rewriter import LLMSourceQueryRewriter
from .runtime_services import (
    execution_summary_service,
    fresh_processing_service,
    FreshProcessingDependencies,
    live_collection_service,
    refinement_orchestration_service,
    result_render_service,
    semantic_local_retrieval_service,
)
from .services import ClueRefinementService, InitialCandidateRetrievalService, RunStatePreparationService
from .user_request_parser import LLMInvestigationPlanner, LLMUserRequestParser


class InvestigationRuntime(InvestigationRuntimeMixin):
    """Small public runtime shell that wires independent services."""

    def __init__(
            self,
            *,
            llm_gateway: LLMGateway,
            phase_engine: PhaseTwoThreeEngine | None = None,
            quality_evaluator: ClueQualityEvaluator | None = None,
            clue_repo: ClueRepo | None = None,
            clue_retriever: ClueRetriever | None = None,
            review_repo: InMemoryReviewRepo | None = None,
            entity_graph: EntityGraphStore | None = None,
            investigation_config: InvestigationConfig | None = None,
            routing_profiles: Mapping[str, Any] | None = None,
        ) -> None:
            self.llm_gateway = llm_gateway
            self.phase_engine = phase_engine or PhaseTwoThreeEngine()
            self.quality_evaluator = quality_evaluator or ClueQualityEvaluator()
            self.clue_repo = clue_repo if clue_repo is not None else InMemoryClueRepo()
            self.clue_retriever = clue_retriever or ClueRetriever()
            self.review_repo = review_repo or InMemoryReviewRepo()
            self.entity_graph = entity_graph if entity_graph is not None else EntityGraphStore()
            self.investigation_config = investigation_config or InvestigationConfig()
            self.routing_profiles = {str(key): value for key, value in (routing_profiles or {}).items()}
            self.clue_refiner = LLMClueRefiner(llm_gateway)
            self.query_rewriter = LLMSourceQueryRewriter(llm_gateway)
            self.source_policy_guard = SourcePolicyGuard()
            self.llm_value_metrics = load_latest_llm_value_report()
            self.model_router = ModelRouter()
            if self.llm_value_metrics:
                self.model_router = self.model_router.with_llm_value_metrics(self.llm_value_metrics)
            else:
                self.model_router = self.model_router.with_record_enrich_policy(
                    enabled=False,
                    reason="llm_value_report_missing_hard_cases_only",
                )
            self.clue_ranker = ClueRanker()
            self.exploration_agent = ExplorationAgent()
            self.intent_parser = LLMUserRequestParser(llm_gateway)
            self.planner = LLMInvestigationPlanner(llm_gateway)
            self.run_state_preparation = RunStatePreparationService(
                routing_profiles=self.routing_profiles,
                intent_parser=self.intent_parser,
                planner=self.planner,
                gateway_stats_count=self._gateway_stats_count,
                normalize_policy_override=self._normalize_policy_override,
                normalize_routing_profile=self._normalize_routing_profile,
                routing_profile_config=self._routing_profile_config,
                effective_investigation_config=self._effective_investigation_config,
                planner_runtime_context=self._planner_runtime_context,
                profile_budget_defaults=self._profile_budget_defaults,
                stage_deadline_ms=self._stage_deadline_ms,
                disabled_llm_trace=self._disabled_llm_trace,
                runtime_quality_gate=self._runtime_quality_gate,
                apply_profile_execution_controls=self._apply_profile_execution_controls,
                plan_execution_controls=self._plan_execution_controls,
                resolve_budget=self._resolve_budget,
                select_sources=self._select_sources,
                deadline_at=self._deadline_at,
            )
            self.initial_candidate_retrieval = InitialCandidateRetrievalService(
                clue_retriever=self.clue_retriever,
                clue_repo=self.clue_repo,
                optional_positive_int=self._optional_positive_int,
                optional_float=self._optional_float,
                summarize_retrieved_clues=self._summarize_retrieved_clues,
                entity_graph=self.entity_graph,
            )
            self.clue_refinement = ClueRefinementService(
                clue_refiner=self.clue_refiner,
                clue_ranker=self.clue_ranker,
                model_router=self.model_router,
                clue_repo=self.clue_repo,
                runtime_context_factory=lambda intent: self.phase_engine.runtime_prompt_context(
                    label=self._runtime_context_label(intent),
                    include_candidates=True,
                    include_gray=True,
                ),
                quality_gate_checker=self._passes_runtime_quality_gate,
                deadline_checker=self._deadline_exhausted,
            )
            self.offline_builder = OfflineClueBuilder(
                phase_engine=self.phase_engine,
                quality_evaluator=self.quality_evaluator,
                clue_repo=self.clue_repo,
                entity_graph=self.entity_graph,
            )
            self.run_state_type = _RunPlanningState
            self.retrieval_state_type = _RetrievalState
            self.semantic_local_retrieval = semantic_local_retrieval_service(self._run_semantic_local_phase)
            self.live_collection_service = live_collection_service(self._run_live_collection_phase)
            self.fresh_processing_service = fresh_processing_service(FreshProcessingDependencies(offline_builder=self.offline_builder))
            self.execution_summary_service = execution_summary_service(self._build_execution_summary)
            self.result_render_service = result_render_service(self._render_run_result)
            self.refinement_orchestration_service = refinement_orchestration_service(self._refine_and_explore_candidates)
            self.workflow = InvestigationWorkflow(
                run_state_preparation=self.run_state_preparation,
                initial_candidate_retrieval=self.initial_candidate_retrieval,
                semantic_local_retrieval=self.semantic_local_retrieval,
                live_collection_service=self.live_collection_service,
                fresh_processing_service=self.fresh_processing_service,
                refinement_service=self.refinement_orchestration_service,
                execution_summary_service=self.execution_summary_service,
                result_render_service=self.result_render_service,
                run_state_type=self.run_state_type,
                retrieval_state_type=self.retrieval_state_type,
            )

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
                        include_gray=True,
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
            return self.workflow.run(
                query,
                records=records,
                available_sources=available_sources,
                collect_source_records=collect_source_records,
                max_sources=max_sources,
                retrieval_filters=retrieval_filters,
                max_concurrent_sources=max_concurrent_sources,
                routing_profile=routing_profile,
                policy_override=policy_override,
            ).payload


__all__ = [
    "InvestigationRuntime",
    "InvestigationRunResult",
    "EvidenceGap",
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
