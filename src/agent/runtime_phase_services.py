"""Phase services for the investigation runtime shell."""


from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Mapping

from src.config_loader import InvestigationConfig, InvestigationPolicyOverride
from src.domain import RunPolicyContext
from src.scheduling.layered_collection import (
    group_sources_by_collection_layer,
    prioritize_sources_for_investigation,
)
from src.safety import PIIMasker
from src.workflows import WorkflowContext

from .budget_controller import RuntimeBudget
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
from .user_request_parser import DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS







def _as_investigation_processing_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["mode"] = "investigation_processing"
    return normalized

class InvestigationPhaseMixin:
    """Extracted helper group; state is supplied by InvestigationRuntime."""

    def _render_run_result(self, context: WorkflowContext) -> InvestigationRunResult:
            run_state = context.run_state
            retrieval_state = context.retrieval_state
            semantic_state = context.semantic_state
            live_state = context.live_state
            fresh_state = context.fresh_state
            refinement_state = context.refinement_state
            execution_summary = context.execution_summary
            planning_traces = [
                run_state.intent_trace.model_dump(),
                run_state.plan_trace.model_dump(),
            ]
            semantic_traces = [dict(item) for item in semantic_state.traces]
            rewrite_traces = [dict(item) for item in live_state.rewrite_traces]
            refine_traces = [dict(item) for item in refinement_state.refine_traces]
            llm_call_traces = [
                *[trace for trace in planning_traces if _is_llm_call_trace(trace)],
                *[trace for trace in rewrite_traces if _is_llm_call_trace(trace)],
                *[trace for trace in refine_traces if _is_llm_call_trace(trace)],
            ]
            llm_item_traces = [trace for trace in refine_traces if trace.get("trace_kind") == "llm_item"]
            llm_traces = [*planning_traces, *semantic_traces, *rewrite_traces, *refine_traces]
            return InvestigationRunResult(
                status="completed" if (fresh_state.records or semantic_state.clues or retrieval_state.retrieved_clues) else "no_data",
                mode=_top_level_mode(execution_summary),
                query=context.query,
                input_count=len(fresh_state.records),
                fetched_count=len(live_state.records) if live_state.records else len(fresh_state.records),
                selected_source_count=len(live_state.selected_sources),
                high_quality_count=len(refinement_state.high_quality_clues),
                candidate_count=len(refinement_state.candidate_clues),
                intent=run_state.intent_payload,
                investigation_plan=run_state.plan_payload,
                llm_traces=llm_traces,
                llm_call_traces=llm_call_traces,
                llm_item_traces=llm_item_traces,
                model_route_traces=[dict(item) for item in refinement_state.model_route_traces],
                flow_decision_traces=[dict(item) for item in execution_summary.get("flow_decision_traces", [])],
                safety_traces=[dict(item) for item in execution_summary.get("safety_traces", [])],
                selected_sources=live_state.selected_sources,
                collection_runs=live_state.collection_runs,
                execution_summary=execution_summary,
                high_quality_clues=refinement_state.high_quality_clues,
                candidate_clues=refinement_state.candidate_clues,
                exploration_hypotheses=refinement_state.exploration_hypotheses,
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
            evidence_gap = self._evidence_gap_from_summary(
                config=run_state.effective_config,
                intent=run_state.intent_payload,
                quality_gate=run_state.runtime_quality_gate,
                retrieved_summary=retrieval_state.retrieved_summary,
                retrieval_filters=run_state.retrieval_filters,
                clues=retrieval_state.retrieved_clues,
            )
            records: list[dict[str, Any]] = []
            traces: list[dict[str, Any]] = []
            clues: list[dict[str, Any]] = []
            phase_payload: dict[str, Any] | None = None
            if retrieval_state.provided_records:
                return _SemanticLocalState(records, traces, clues, phase_payload, summary, should_collect_live, live_collection_reasons, evidence_gap)
    
            semantic_local_limit = self._semantic_local_limit(budget=run_state.budget)
            summary["query_limit"] = semantic_local_limit
            records, traces = self._collect_semantic_local_records(query=query, limit=semantic_local_limit)
            summary["hit_count"] = sum(1 for item in traces if item.get("stage") == "semantic_local_retrieval")
            summary["record_count"] = len(records)
            summary["graph_expanded_count"] = sum(1 for item in traces if item.get("stage") == "semantic_graph_expansion")
            if not records:
                return _SemanticLocalState(records, traces, clues, phase_payload, summary, should_collect_live, live_collection_reasons, evidence_gap)
    
            self.offline_builder.set_runtime_controls(
                llm_gateway=getattr(run_state, "llm_gateway", None),
                budget_controller=run_state.budget_controller,
                policy=run_state.run_policy,
            )
            semantic_local_build = self.offline_builder.build(
                records,
                prompt_text=query,
                source_candidates=run_state.selected_sources or run_state.available_sources_list,
                quality_profile=str(run_state.intent_payload.get("quality_profile") or "balanced"),
                require_cross_source=bool(run_state.intent_payload.get("require_cross_source")),
                require_evidence_chain=bool(run_state.intent_payload.get("require_evidence_chain", True)),
                policy=run_state.run_policy,
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
            evidence_gap = self._evidence_gap_from_summary(
                config=run_state.effective_config,
                intent=run_state.intent_payload,
                quality_gate=run_state.runtime_quality_gate,
                retrieved_summary=merged_retrieved_summary,
                retrieval_filters=run_state.retrieval_filters,
                clues=[*retrieval_state.retrieved_clues, *clues],
                reasons=[] if live_collection_reasons == ["semantic_local_high_quality_satisfied"] else None,
            )
            return _SemanticLocalState(records, traces, clues, phase_payload, summary, should_collect_live, live_collection_reasons, evidence_gap)


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
            evidence_gap = semantic_state.evidence_gap
            rewrite_traces: list[dict[str, Any]] = []
            live_records: list[dict[str, Any]] = []
            collection_runs: list[dict[str, Any]] = []
            if not (semantic_state.should_collect_live and collect_source_records is not None):
                return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons, evidence_gap)
    
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
                return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons, evidence_gap)
    
            selected_sources = self._cap_live_sources(
                selected_sources,
                retrieved_summary=retrieval_state.retrieved_summary,
                config=run_state.effective_config,
            )
            selected_sources, blocked_runs = self._filter_sources_for_collection(selected_sources)
            collection_runs.extend(blocked_runs)
            if not selected_sources:
                live_collection_reasons.append("all_sources_blocked_by_source_policy")
                return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons, evidence_gap)
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
                        include_gray=True,
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
                selected_sources, blocked_runs = self._filter_sources_for_collection(selected_sources)
                collection_runs.extend(blocked_runs)
                if not selected_sources:
                    live_collection_reasons.append("all_sources_blocked_by_source_policy")
                    return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons, evidence_gap)
            live_records, collection_runs = self._collect_records_from_sources(
                selected_sources,
                collect_source_records=collect_source_records,
                max_raw_records=run_state.budget["max_raw_records"],
                max_concurrent_sources=max_concurrent_sources,
                deadline_at=collection_deadline_at,
                layer_recheck=self._live_collection_layer_recheck(
                    query=query,
                    run_state=run_state,
                    retrieval_state=retrieval_state,
                    semantic_state=semantic_state,
                ),
            )
            for run in reversed(collection_runs):
                payload = run.get("evidence_gap_after_layer")
                if isinstance(payload, Mapping):
                    evidence_gap = EvidenceGap.from_mapping(payload)
                    break
            return _LiveCollectionState(live_records, collection_runs, rewrite_traces, selected_sources, live_collection_reasons, evidence_gap)


    def _live_collection_layer_recheck(
            self,
            *,
            query: str,
            run_state: _RunPlanningState,
            retrieval_state: _RetrievalState,
            semantic_state: _SemanticLocalState,
        ) -> Any:
            semantic_summary = self._summarize_retrieved_clues(
                semantic_state.clues,
                time_range_hours=self._optional_positive_int(run_state.retrieval_filters.get("time_range_hours"))
                or self._optional_positive_int(run_state.intent_payload.get("time_range_hours")),
                quality_gate=run_state.runtime_quality_gate,
            )
            base_summary = self._merge_retrieved_summary(retrieval_state.retrieved_summary, semantic_summary)

            def recheck(collected_records: list[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
                if not collected_records:
                    return False, "evidence_gap_still_open_after_layer", semantic_state.evidence_gap.model_dump()
                quick_policy = run_state.run_policy.model_copy(
                    update={
                        "enable_llm_record_enrich": False,
                        "llm_stage_policy": {
                            **run_state.run_policy.llm_stage_policy,
                            "record_enrich": False,
                        },
                    }
                )
                self.offline_builder.set_runtime_controls(
                    llm_gateway=getattr(run_state, "llm_gateway", None),
                    budget_controller=run_state.budget_controller,
                    policy=quick_policy,
                )
                build_result = self.offline_builder.build(
                    collected_records,
                    prompt_text=query,
                    source_candidates=run_state.selected_sources or run_state.available_sources_list,
                    quality_profile=str(run_state.intent_payload.get("quality_profile") or "balanced"),
                    require_cross_source=bool(run_state.intent_payload.get("require_cross_source")),
                    require_evidence_chain=bool(run_state.intent_payload.get("require_evidence_chain", True)),
                    policy=quick_policy,
                )
                fresh_summary = self._summarize_retrieved_clues(
                    build_result.clues,
                    time_range_hours=self._optional_positive_int(run_state.retrieval_filters.get("time_range_hours"))
                    or self._optional_positive_int(run_state.intent_payload.get("time_range_hours")),
                    quality_gate=run_state.runtime_quality_gate,
                )
                merged_summary = self._merge_retrieved_summary(base_summary, fresh_summary)
                gap = self._evidence_gap_from_summary(
                    config=run_state.effective_config,
                    intent=run_state.intent_payload,
                    quality_gate=run_state.runtime_quality_gate,
                    retrieved_summary=merged_summary,
                    retrieval_filters=run_state.retrieval_filters,
                    clues=[*retrieval_state.retrieved_clues, *semantic_state.clues, *build_result.clues],
                )
                if gap.is_sufficient and build_result.clues:
                    return True, "evidence_gap_satisfied_after_layer", gap.model_dump()
                return False, "evidence_gap_still_open_after_layer", gap.model_dump()

            return recheck


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
                self.offline_builder.set_runtime_controls(
                    llm_gateway=self.llm_gateway,
                    budget_controller=run_state.budget_controller,
                    policy=run_state.run_policy,
                )
                build_result = self.offline_builder.build(
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
            requested_max_refine = (
                0
                if run_state.plan_execution_controls.refine_policy == "off" or not run_state.run_policy.enable_llm_clue_refine
                else int(run_state.budget["max_llm_refine_clues"] or 0)
            )
            effective_max_refine = max(0, requested_max_refine)
            refine_budget_reasons: list[str] = []
            if not run_state.run_policy.enable_llm_clue_refine:
                refine_budget_reasons.append("policy_disabled_clue_refine")
            refined_high_quality, refined_candidates, refine_traces, model_route_traces, budget_controller_snapshot = self.clue_refinement.refine(
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
                actual_refined_count=sum(
                    1
                    for trace in refine_traces
                    if trace.get("stage") in {"clue_refine", "clue_refine_item"}
                    and trace.get("trace_kind") != "llm_call"
                ),
                refine_target_count=sum(1 for trace in model_route_traces if trace.get("selector_selected")),
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
            used_clue_pool = bool(
                refinement_state.pool_clues_for_merge
                or any("clue_pool" in (clue.get("orchestration_origins") or []) for clue in refinement_state.merged_candidates)
                or (retrieval_state.retrieved_clues and not fresh_state.records)
            )
            route_uses_pool_context = used_clue_pool or (
                bool(retrieval_state.retrieved_summary.get("total_count"))
                and bool(live_state.records)
                and "insufficient_high_quality_pool_clues" in live_state.live_collection_reasons
            )
            orchestration_route = self._orchestration_route(
                used_clue_pool=route_uses_pool_context,
                used_fresh_processing=bool(fresh_state.records),
                used_live_collection=bool(live_state.records),
                used_provided_records=bool(retrieval_state.provided_records),
                used_semantic_local=bool(semantic_state.records),
            )
            query_rewrite_count = sum(1 for item in live_state.selected_sources if item.get("query_rewrite_applied"))
            final_evidence_gap = live_state.evidence_gap if live_state.evidence_gap.reasons or live_state.records else semantic_state.evidence_gap
            execution_summary = {
                **fresh_state.phase_payload,
                "status": "completed" if (fresh_state.records or semantic_state.clues) else "retrieved_from_clue_pool",
                "mode": self._execution_mode(
                    used_clue_pool=used_clue_pool,
                    used_fresh_processing=bool(fresh_state.records or semantic_state.clues),
                ),
                "run_mode": _top_level_mode(
                    {
                        "used_provided_records": bool(retrieval_state.provided_records),
                        "used_live_collection": bool(live_state.records),
                        "used_clue_pool": used_clue_pool,
                        "used_semantic_local_retrieval": bool(semantic_state.records),
                    }
                ),
                "budget": run_state.budget,
                "run_policy": run_state.run_policy.model_dump(),
                "llm_stage_policy": run_state.run_policy.llm_stage_policy,
                "graph_clue_generation_enabled": bool(run_state.run_policy.enable_graph_clue_generation),
                "refined_clue_count": refinement_state.actual_refined_count,
                "refine_target_count": refinement_state.refine_target_count,
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
                "evidence_gap": final_evidence_gap.model_dump(),
                "flow_decision_traces": [dict(item) for item in run_state.flow_decision_traces],
                "orchestration_route": orchestration_route,
                "live_collection_reasons": live_state.live_collection_reasons,
                "selected_source_classes": [
                    self._source_diversity_class(source)
                    for source in live_state.selected_sources
                ],
                "safety_traces": [
                    dict(item)
                    for item in live_state.collection_runs
                    if str(item.get("status") or "").startswith("blocked_by_")
                ],
                "elapsed_budget_exhausted": self._deadline_exhausted(run_state.deadline_at),
                "runtime_quality_gate": run_state.runtime_quality_gate.model_dump(),
                "plan_execution_controls": run_state.plan_execution_controls.model_dump(),
                "requested_max_llm_refine_clues": refinement_state.requested_max_refine,
                "effective_max_llm_refine_clues": refinement_state.effective_max_refine,
                "refine_budget_reasons": refinement_state.refine_budget_reasons,
                "model_route_count": len(refinement_state.model_route_traces),
                "model_route_traces": [dict(trace) for trace in refinement_state.model_route_traces],
                "model_route_summary": self._summarize_model_routes(refinement_state.model_route_traces),
                "budget_controller": refinement_state.budget_controller_snapshot,
                "llm_budget": refinement_state.budget_controller_snapshot.get("llm_budget", {}),
                "llm_gateway": self._summarize_gateway_stats(self._gateway_stats_since(run_state.gateway_stats_start)),
                "llm_cost": self._merge_llm_cost_summary(
                    refinement_state.budget_controller_snapshot,
                    self._summarize_gateway_stats(self._gateway_stats_since(run_state.gateway_stats_start)),
                ),
                "llm_call_traces": [dict(trace) for trace in refinement_state.refine_traces if trace.get("trace_kind") == "llm_call"],
                "llm_item_traces": [dict(trace) for trace in refinement_state.refine_traces if trace.get("trace_kind") == "llm_item"],
                "exploration_hypothesis_count": len(refinement_state.exploration_hypotheses),
                "collection_layers_executed": [
                    str(item.get("collection_layer") or "")
                    for item in live_state.collection_runs
                    if item.get("fetched_count", 0) > 0
                ],
                "collection_source_classes_executed": [
                    self._source_diversity_class(item)
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
                    llm_gateway_stats=self._gateway_stats_since(run_state.gateway_stats_start),
                )
            execution_summary["routing_profile"] = run_state.profile
            return self._mask_execution_summary(execution_summary)


    @staticmethod
    def _merge_llm_cost_summary(budget_snapshot: Mapping[str, Any], gateway_summary: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "budget_reserved_tokens": int(budget_snapshot.get("budget_reserved_tokens") or budget_snapshot.get("estimated_tokens") or 0),
                "gateway_request_estimated_tokens": int(gateway_summary.get("gateway_request_estimated_tokens") or gateway_summary.get("estimated_tokens") or 0),
                "prompt_estimated_tokens": int(gateway_summary.get("prompt_estimated_tokens") or 0),
                "completion_token_limit": int(gateway_summary.get("completion_token_limit") or 0),
                "actual_usage_tokens": gateway_summary.get("actual_usage_tokens"),
                "token_estimation_policy": "budget_reserved_vs_gateway_prompt_plus_completion_estimates",
            }


def _is_llm_call_trace(trace: Mapping[str, Any]) -> bool:
    if trace.get("trace_kind") == "llm_call":
        return True
    if trace.get("trace_kind") == "llm_item":
        return False
    stage = str(trace.get("stage") or "")
    return stage in {"intent_parse", "investigation_plan", "source_query_rewrite", "clue_refine"} and "llm_ok" in trace


def _top_level_mode(summary: Mapping[str, Any]) -> str:
    if bool(summary.get("used_provided_records")):
        return "provided_records_pipeline"
    if bool(summary.get("used_live_collection")):
        return "live_collection_pipeline"
    if bool(summary.get("used_clue_pool")) and bool(summary.get("used_semantic_local_retrieval")):
        return "pool_plus_semantic_local"
    if bool(summary.get("used_clue_pool")):
        return "asset_first_investigation"
    return "hybrid_investigation"


__all__ = ["InvestigationPhaseMixin"]
