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
from storage import ClueRepo, InMemoryClueRepo, InMemoryReviewRepo

from .exploration_agent import ExplorationAgent
from .query_rewriter import LLMSourceQueryRewriter
from .user_request_parser import LLMInvestigationPlanner, LLMUserRequestParser


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
    ) -> None:
        self.llm_gateway = llm_gateway
        self.phase_engine = phase_engine or PhaseTwoThreeEngine()
        self.quality_evaluator = quality_evaluator or ClueQualityEvaluator()
        self.clue_repo = clue_repo if clue_repo is not None else InMemoryClueRepo()
        self.clue_retriever = clue_retriever or ClueRetriever()
        self.review_repo = review_repo or InMemoryReviewRepo()
        self.investigation_config = investigation_config or InvestigationConfig()
        self.clue_refiner = LLMClueRefiner(llm_gateway)
        self.query_rewriter = LLMSourceQueryRewriter(llm_gateway)
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
        started_at = time.perf_counter()
        normalized_policy_override = self._normalize_policy_override(policy_override)
        effective_config = self._effective_investigation_config(
            routing_profile=routing_profile,
            policy_override=normalized_policy_override,
        )
        initial_runtime_context = self._planner_runtime_context()
        intent, intent_trace = self.intent_parser.parse(
            query,
            runtime_context=initial_runtime_context,
        )
        intent_payload = intent.model_dump()
        available_sources_list = [dict(source) for source in available_sources]
        plan, plan_trace = self.planner.plan(
            query,
            intent,
            available_sources=available_sources_list,
            runtime_context=initial_runtime_context,
        )
        plan_payload = plan.model_dump()
        runtime_quality_gate = self._runtime_quality_gate(
            intent=intent_payload,
            plan=plan_payload,
            policy_override=normalized_policy_override,
        )
        plan_execution_controls = self._plan_execution_controls(plan_payload)
        budget = self._resolve_budget(
            plan_payload,
            explicit_max_sources=max_sources,
            available_source_count=len(available_sources_list),
            policy_override=normalized_policy_override,
        )
        deadline_at = self._deadline_at(started_at, budget["max_elapsed_seconds"])
        retrieval_filters = dict(retrieval_filters or {})
        selected_sources = self._select_sources(
            plan_payload,
            available_sources_list,
            max_sources=budget["max_sources"],
            risk_types=intent.risk_types,
        )
        if isinstance(budget.get("max_sources"), int) and budget["max_sources"] > 0:
            selected_sources = selected_sources[: int(budget["max_sources"])]
        retrieved_clues = self.clue_retriever.retrieve(
            self.clue_repo.list(),
            query=query,
            intent=intent_payload,
            limit=budget["max_candidate_clues"],
            time_range_hours=self._optional_positive_int(retrieval_filters.get("time_range_hours")),
            allowed_source_types=retrieval_filters.get("source_types") or (),
            allowed_risk_types=retrieval_filters.get("risk_types") or (),
            min_quality_score=self._optional_float(retrieval_filters.get("min_quality_score")),
        )
        retrieved_summary = self._summarize_retrieved_clues(
            retrieved_clues,
            time_range_hours=self._optional_positive_int(retrieval_filters.get("time_range_hours"))
            or self._optional_positive_int(intent_payload.get("time_range_hours")),
            quality_gate=runtime_quality_gate,
        )
        provided_records = [dict(record) if isinstance(record, Mapping) else record for record in records]
        if len(provided_records) > budget["max_raw_records"]:
            provided_records = provided_records[: budget["max_raw_records"]]
        semantic_local_records: list[dict[str, Any]] = []
        semantic_local_traces: list[dict[str, Any]] = []
        semantic_local_clues: list[dict[str, Any]] = []
        semantic_phase_payload: dict[str, Any] | None = None
        semantic_local_summary = {
            "query_limit": 0,
            "hit_count": 0,
            "record_count": 0,
            "clue_count": 0,
            "graph_expanded_count": 0,
        }
        collection_runs: list[dict[str, Any]] = []
        rewrite_traces: list[dict[str, Any]] = []
        should_collect_live, live_collection_reasons = self._should_collect_live_sources(
            config=effective_config,
            intent=intent_payload,
            quality_gate=runtime_quality_gate,
            execution_controls=plan_execution_controls,
            selected_sources=selected_sources,
            retrieved_summary=retrieved_summary,
            retrieval_filters=retrieval_filters,
            collect_source_records=collect_source_records,
            has_provided_records=bool(provided_records),
        )
        if should_collect_live and not provided_records:
            semantic_local_limit = self._semantic_local_limit(budget=budget)
            semantic_local_summary["query_limit"] = semantic_local_limit
            semantic_local_records, semantic_local_traces = self._collect_semantic_local_records(
                query=query,
                limit=semantic_local_limit,
            )
            semantic_local_summary["hit_count"] = sum(
                1 for item in semantic_local_traces if item.get("stage") == "semantic_local_retrieval"
            )
            semantic_local_summary["record_count"] = len(semantic_local_records)
            semantic_local_summary["graph_expanded_count"] = sum(
                1 for item in semantic_local_traces if item.get("stage") == "semantic_graph_expansion"
            )
            if semantic_local_records:
                semantic_local_build = self.offline_builder.build(
                    semantic_local_records,
                    prompt_text=query,
                    source_candidates=selected_sources or available_sources_list,
                    quality_profile=intent.quality_profile,
                    require_cross_source=intent.require_cross_source,
                    require_evidence_chain=intent.require_evidence_chain,
                )
                semantic_phase_payload = _as_investigation_processing_summary(semantic_local_build.execution_summary)
                semantic_local_clues = semantic_local_build.clues
                semantic_local_summary["clue_count"] = len(semantic_local_clues)
                semantic_retrieved_summary = self._summarize_retrieved_clues(
                    semantic_local_clues,
                    time_range_hours=self._optional_positive_int(retrieval_filters.get("time_range_hours"))
                    or self._optional_positive_int(intent_payload.get("time_range_hours")),
                    quality_gate=runtime_quality_gate,
                )
                merged_retrieved_summary = self._merge_retrieved_summary(retrieved_summary, semantic_retrieved_summary)
                should_collect_live, live_collection_reasons = self._should_collect_live_sources(
                    config=effective_config,
                    intent=intent_payload,
                    quality_gate=runtime_quality_gate,
                    execution_controls=plan_execution_controls,
                    selected_sources=selected_sources,
                    retrieved_summary=merged_retrieved_summary,
                    retrieval_filters=retrieval_filters,
                    collect_source_records=collect_source_records,
                    has_provided_records=bool(provided_records),
                )
                if should_collect_live and int(semantic_retrieved_summary.get("high_quality_count") or 0) > 0:
                    if set(live_collection_reasons).issubset({"insufficient_high_quality_pool_clues"}):
                        should_collect_live = False
                        live_collection_reasons = ["semantic_local_high_quality_satisfied"]
        live_records: list[dict[str, Any]] = []
        if should_collect_live and collect_source_records is not None:
            collection_deadline_at = deadline_at
            planning_exhausted_before_first_collection = (
                self._deadline_exhausted(collection_deadline_at)
                and not provided_records
                and not semantic_local_records
                and not retrieved_clues
            )
            if planning_exhausted_before_first_collection:
                live_collection_reasons.append("elapsed_budget_reset_for_first_live_collection")
                collection_deadline_at = self._deadline_at(time.perf_counter(), budget["max_elapsed_seconds"])

            if self._deadline_exhausted(collection_deadline_at):
                live_collection_reasons.append("elapsed_budget_exhausted_before_live_collection")
            else:
                selected_sources = self._cap_live_sources(
                    selected_sources,
                    retrieved_summary=retrieved_summary,
                    config=effective_config,
                )
                if plan_execution_controls.query_rewrite_policy == "off" or planning_exhausted_before_first_collection:
                    rewrite_traces = self._query_rewrite_skipped_traces(
                        selected_sources,
                        reason=(
                            "elapsed_budget_exhausted_before_query_rewrite"
                            if planning_exhausted_before_first_collection
                            else "plan_query_rewrite_disabled"
                        ),
                    )
                else:
                    selected_sources, rewrite_traces = self._rewrite_selected_sources(
                        selected_sources,
                        query=query,
                        intent=intent_payload,
                        plan=plan_payload,
                        runtime_context=self.phase_engine.runtime_prompt_context(
                            label=self._runtime_context_label(intent_payload),
                            include_candidates=True,
                        ),
                    )
                    if (
                        self._deadline_exhausted(collection_deadline_at)
                        and not provided_records
                        and not semantic_local_records
                        and not retrieved_clues
                    ):
                        live_collection_reasons.append("elapsed_budget_reset_after_query_rewrite_for_first_live_collection")
                        collection_deadline_at = self._deadline_at(time.perf_counter(), budget["max_elapsed_seconds"])
                live_records, collection_runs = self._collect_records_from_sources(
                    selected_sources,
                    collect_source_records=collect_source_records,
                    max_raw_records=budget["max_raw_records"],
                    max_concurrent_sources=max_concurrent_sources,
                    deadline_at=collection_deadline_at,
                )
        fresh_records = provided_records if provided_records else (live_records or semantic_local_records)
        built_clues: list[dict[str, Any]] = []
        if live_records or provided_records:
            build_result = self.offline_builder.build(
                fresh_records,
                prompt_text=query,
                source_candidates=selected_sources or available_sources_list,
                quality_profile=intent.quality_profile,
                require_cross_source=intent.require_cross_source,
                require_evidence_chain=intent.require_evidence_chain,
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
        if semantic_phase_payload is not None and not (live_records or provided_records):
            phase_payload = semantic_phase_payload
        pool_clues_for_merge = (
            retrieved_clues
            if not fresh_records
            else [
                dict(clue)
                for clue in retrieved_clues
                if float(clue.get("retrieval_score") or 0.0) >= effective_config.retrieval_score_threshold_for_pool_merge
            ]
        )
        merged_candidates = self._merge_candidate_clues(
            pool_clues=pool_clues_for_merge,
            fresh_clues=[*semantic_local_clues, *built_clues],
        )
        if not merged_candidates and retrieved_clues and not fresh_records:
            merged_candidates = [dict(clue) for clue in retrieved_clues]
        requested_max_refine = 0 if plan_execution_controls.refine_policy == "off" else int(budget["max_llm_refine_clues"] or 0)
        effective_max_refine = max(0, requested_max_refine)
        refine_budget_reasons: list[str] = []
        refined_high_quality, refined_candidates, refine_traces = self._refine_retrieved_clues(
            merged_candidates,
            query=query,
            intent=intent_payload,
            quality_gate=runtime_quality_gate,
            max_refine=effective_max_refine,
            deadline_at=deadline_at,
        )
        high_quality_clues = refined_high_quality
        candidate_clues = refined_candidates
        actual_refined_count = len(refine_traces)
        exploration_hypotheses = self._build_exploration_hypotheses(
            query=query,
            processed_records=fresh_records,
            candidate_clues=refined_candidates,
            high_quality_clues=refined_high_quality,
            runtime_quality_gate=runtime_quality_gate,
        )
        orchestration_route = self._orchestration_route(
            used_clue_pool=bool(pool_clues_for_merge or (retrieved_clues and not fresh_records)),
            used_fresh_processing=bool(fresh_records),
            used_live_collection=bool(live_records),
            used_provided_records=bool(provided_records),
            used_semantic_local=bool(semantic_local_records),
        )
        execution_summary = {
            **phase_payload,
            "status": "completed" if (fresh_records or semantic_local_clues) else "retrieved_from_clue_pool",
            "mode": self._execution_mode(
                used_clue_pool=bool(pool_clues_for_merge or (retrieved_clues and not fresh_records)),
                used_fresh_processing=bool(fresh_records or semantic_local_clues),
            ),
            "budget": budget,
            "refined_clue_count": actual_refined_count,
            "query_rewrite_count": sum(1 for item in selected_sources if item.get("query_rewrite_applied")),
            "query_rewrite_fallback_count": sum(1 for item in selected_sources if item.get("query_rewrite_used_fallback")),
            "candidate_clue_hits": len(retrieved_clues),
            "fresh_candidate_count": len(built_clues) + len(semantic_local_clues),
            "live_fresh_candidate_count": len(built_clues),
            "semantic_local_candidate_count": len(semantic_local_clues),
            "merged_candidate_count": len(merged_candidates),
            "used_clue_pool": bool(pool_clues_for_merge or (retrieved_clues and not fresh_records)),
            "used_live_collection": bool(live_records),
            "used_provided_records": bool(provided_records),
            "used_semantic_local_retrieval": bool(semantic_local_records),
            "semantic_local_summary": semantic_local_summary,
            "orchestration_route": orchestration_route,
            "live_collection_reasons": live_collection_reasons,
            "elapsed_budget_exhausted": self._deadline_exhausted(deadline_at),
            "runtime_quality_gate": runtime_quality_gate.model_dump(),
            "plan_execution_controls": plan_execution_controls.model_dump(),
            "requested_max_llm_refine_clues": requested_max_refine,
            "effective_max_llm_refine_clues": effective_max_refine,
            "refine_budget_reasons": refine_budget_reasons,
            "exploration_hypothesis_count": len(exploration_hypotheses),
            "collection_layers_executed": [str(item.get("collection_layer") or "") for item in collection_runs if item.get("fetched_count", 0) > 0],
        }
        if effective_config.telemetry_enabled:
            execution_summary["telemetry"] = self._build_telemetry(
                started_at=started_at,
                budget=budget,
                requested_max_llm_refine_clues=requested_max_refine,
                effective_max_llm_refine_clues=effective_max_refine,
                selected_source_count=len(selected_sources),
                collected_record_count=len(live_records),
                provided_record_count=len(provided_records),
                retrieved_clue_count=len(retrieved_clues),
                merged_candidate_count=len(merged_candidates),
                refined_clue_count=actual_refined_count,
                rewrite_count=sum(1 for item in selected_sources if item.get("query_rewrite_applied")),
                used_live_collection=bool(live_records),
                used_clue_pool=bool(pool_clues_for_merge or (retrieved_clues and not fresh_records)),
                elapsed_budget_exhausted=self._deadline_exhausted(deadline_at),
                semantic_local_record_count=len(semantic_local_records),
            )
        execution_summary["routing_profile"] = self._normalize_routing_profile(routing_profile)
        processed_records = provided_records if provided_records else (live_records or semantic_local_records)

        return InvestigationRunResult(
            status="completed" if (fresh_records or semantic_local_clues or retrieved_clues) else "no_data",
            mode="llm_driven_investigation",
            query=query,
            input_count=len(processed_records),
            fetched_count=len(live_records) if live_records else len(processed_records),
            selected_source_count=len(selected_sources),
            high_quality_count=len(high_quality_clues),
            candidate_count=len(candidate_clues),
            intent=intent_payload,
            investigation_plan=plan_payload,
            llm_traces=[intent_trace.model_dump(), plan_trace.model_dump(), *semantic_local_traces, *rewrite_traces, *refine_traces],
            selected_sources=selected_sources,
            collection_runs=collection_runs,
            execution_summary=execution_summary,
            high_quality_clues=high_quality_clues,
            candidate_clues=candidate_clues,
            exploration_hypotheses=exploration_hypotheses,
        )

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
    ) -> dict[str, int | None]:
        raw = plan.get("budget") or {}
        if not isinstance(raw, Mapping):
            raw = {}
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
        budget = {
            "max_sources": resolved_max_sources,
            "max_raw_records": self._positive_int(raw.get("max_raw_records"), 5000),
            "max_candidate_clues": self._positive_int(raw.get("max_candidate_clues"), candidate_budget_default),
            "max_llm_refine_clues": self._positive_int(raw.get("max_llm_refine_clues"), 20),
            "max_elapsed_seconds": self._positive_int(raw.get("max_elapsed_seconds"), 20),
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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rewritten_sources: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        for source in selected_sources:
            rewritten_source, trace = self.query_rewriter.rewrite(
                source,
                query=query,
                intent=intent,
                plan=plan,
                runtime_context=runtime_context,
            )
            rewritten_sources.append(rewritten_source)
            traces.append(trace.model_dump())
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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        refined: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        for index, clue in enumerate(clues):
            item = dict(clue)
            if index < max_refine and not self._deadline_exhausted(deadline_at):
                item, trace = self.clue_refiner.refine(
                    item,
                    query=query,
                    intent=intent,
                    runtime_context=self.phase_engine.runtime_prompt_context(
                        label=self._runtime_context_label(intent),
                        include_candidates=True,
                    ),
                )
                traces.append(trace)
            refined.append(item)
            self.clue_repo.save(item)
        high_quality = [clue for clue in refined if self._passes_runtime_quality_gate(clue, quality_gate=quality_gate)]
        candidates = [clue for clue in refined if clue not in high_quality]
        return high_quality, candidates, traces

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
        }
        return telemetry

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
        if profile == "fast":
            return {
                "live_collection_enabled": True,
                "balanced_min_pool_high_quality_count": 1,
                "high_precision_min_pool_high_quality_count": 1,
                "min_cross_source_count": 2,
                "max_live_sources_when_pool_hit": 1,
                "retrieval_score_threshold_for_pool_merge": 0.25,
            }
        if profile == "high_recall":
            return {
                "live_collection_enabled": True,
                "balanced_min_pool_high_quality_count": 2,
                "high_precision_min_pool_high_quality_count": 3,
                "min_cross_source_count": 3,
                "max_live_sources_when_pool_hit": max(3, self.investigation_config.max_live_sources_when_pool_hit),
                "retrieval_score_threshold_for_pool_merge": 0.0,
            }
        return {}

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

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
