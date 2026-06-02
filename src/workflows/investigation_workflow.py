"""Five-stage workflow runner for investigation orchestration."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.agent.investigation_contracts import _FreshProcessingState, _LiveCollectionState
from src.config_loader import InvestigationPolicyOverride

from .workflow_context import WorkflowContext
from .workflow_result import WorkflowResult


_MAIN_STAGE_ORDER = (
    "input_task",
    "route_and_guard",
    "asset_retrieval",
    "intelligence_pipeline",
    "clue_generation_report",
)


class InvestigationWorkflow:
    """Run the public five-stage investigation flow over internal services."""

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
        records_list = list(records)
        available_sources_list = list(available_sources)
        context = self._route_and_retrieve_assets(
            query,
            records=records_list,
            available_sources=available_sources_list,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            collect_source_records=collect_source_records,
            planning_mode="full" if records_list else "preflight",
        )
        self._record_input_stage(context, records_list, available_sources_list)
        if not records_list and not self._assets_satisfied(context):
            context = self._reroute_with_evidence_gap(
                context,
                query=query,
                records=records_list,
                available_sources=available_sources_list,
                max_sources=max_sources,
                retrieval_filters=retrieval_filters,
                routing_profile=routing_profile,
                policy_override=policy_override,
                collect_source_records=collect_source_records,
            )
        self._decide_and_run_fresh_path(
            context,
            query=query,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
            has_provided_records=bool(records_list),
        )
        self._generate_clues_and_report(context, query=query)
        return context

    def _route_and_retrieve_assets(
        self,
        query: str,
        *,
        records: list[Mapping[str, Any] | Any],
        available_sources: list[Mapping[str, Any]],
        max_sources: int | None,
        retrieval_filters: Mapping[str, Any] | None,
        routing_profile: str | None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None,
        collect_source_records: Any | None,
        planning_mode: str,
        evidence_gap: Any | None = None,
        flow_decision_traces: list[dict[str, Any]] | None = None,
        main_flow_stages: list[dict[str, Any]] | None = None,
    ) -> WorkflowContext:
        run_state = self.run_state_preparation.prepare(
            query=query,
            available_sources=available_sources,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            run_state_type=self.run_state_type,
            planning_mode=planning_mode,
            evidence_gap=evidence_gap,
            flow_decision_traces=flow_decision_traces,
        )
        retrieval_state = self.initial_candidate_retrieval.retrieve(
            query=query,
            records=records,
            run_state=run_state,
            retrieval_state_type=self.retrieval_state_type,
        )
        context = WorkflowContext(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            main_flow_stages=[dict(item) for item in (main_flow_stages or [])],
        )
        context.semantic_state = self.semantic_local_retrieval.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        self._set_main_stage(
            context,
            "route_and_guard",
            status="completed",
            planning_mode=getattr(run_state, "planning_mode", planning_mode),
            routing_profile=getattr(run_state, "profile", None),
            selected_source_count=_safe_len(getattr(run_state, "selected_sources", [])),
        )
        self._set_main_stage(
            context,
            "asset_retrieval",
            status="completed",
            clue_pool_hits=_safe_len(getattr(retrieval_state, "retrieved_clues", [])),
            semantic_record_count=_safe_len(getattr(context.semantic_state, "records", [])),
            semantic_clue_count=_safe_len(getattr(context.semantic_state, "clues", [])),
        )
        return context

    def _reroute_with_evidence_gap(
        self,
        context: WorkflowContext,
        *,
        query: str,
        records: list[Mapping[str, Any] | Any],
        available_sources: list[Mapping[str, Any]],
        max_sources: int | None,
        retrieval_filters: Mapping[str, Any] | None,
        routing_profile: str | None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None,
        collect_source_records: Any | None,
    ) -> WorkflowContext:
        evidence_gap = getattr(context.semantic_state, "evidence_gap", None)
        evidence_payload = evidence_gap.model_dump() if hasattr(evidence_gap, "model_dump") else evidence_gap
        traces = [
            {
                "stage": "preflight_evidence_gap",
                "next_action": "run_conditional_planning",
                "reason": "asset_retrieval_insufficient_for_direct_report",
                "evidence_gap": evidence_payload,
            }
        ]
        return self._route_and_retrieve_assets(
            query,
            records=records,
            available_sources=available_sources,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            collect_source_records=collect_source_records,
            planning_mode="full",
            evidence_gap=evidence_payload,
            flow_decision_traces=traces,
            main_flow_stages=context.main_flow_stages,
        )

    def _decide_and_run_fresh_path(
        self,
        context: WorkflowContext,
        *,
        query: str,
        collect_source_records: Any | None,
        max_concurrent_sources: int,
        has_provided_records: bool,
    ) -> None:
        needs_fresh = has_provided_records or not self._assets_satisfied(context)
        self._set_main_stage(
            context,
            "asset_retrieval",
            status="completed",
            needs_fresh_data=needs_fresh,
            decision_reason="user_provided_records" if has_provided_records else (
                "asset_retrieval_insufficient" if needs_fresh else "asset_retrieval_sufficient"
            ),
        )
        if not needs_fresh:
            self._record_preflight_satisfied(context)
            self._skip_collection_pipeline(context, reason="asset_retrieval_sufficient")
            return
        context.live_state = self.live_collection_service.run(
            query=query,
            run_state=context.run_state,
            retrieval_state=context.retrieval_state,
            semantic_state=context.semantic_state,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
        )
        context.fresh_state = self.fresh_processing_service.run(
            query=query,
            run_state=context.run_state,
            retrieval_state=context.retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
        )
        live_record_count = _safe_len(getattr(context.live_state, "records", []))
        processed_record_count = _safe_len(getattr(context.fresh_state, "records", []))
        built_clue_count = _safe_len(getattr(context.fresh_state, "built_clues", []))
        self._set_main_stage(
            context,
            "intelligence_pipeline",
            status="completed" if (live_record_count or processed_record_count or built_clue_count) else "skipped",
            mode="collection_or_input_records",
            reason=None if (live_record_count or processed_record_count or built_clue_count) else "fresh_data_requested_but_no_records",
            live_record_count=live_record_count,
            processed_record_count=processed_record_count,
            built_clue_count=built_clue_count,
        )

    def _skip_collection_pipeline(self, context: WorkflowContext, *, reason: str) -> None:
        semantic_state = context.semantic_state
        context.live_state = _LiveCollectionState(
            records=[],
            collection_runs=[],
            rewrite_traces=[],
            selected_sources=[dict(item) for item in getattr(context.run_state, "selected_sources", [])],
            live_collection_reasons=list(getattr(semantic_state, "live_collection_reasons", []) or [reason]),
            evidence_gap=getattr(semantic_state, "evidence_gap", None),
        )
        context.fresh_state = _FreshProcessingState(
            records=[],
            built_clues=[],
            phase_payload=self._skipped_phase_payload(semantic_state),
        )
        self._set_main_stage(
            context,
            "intelligence_pipeline",
            status="skipped",
            mode="history_assets_only",
            reason=reason,
            semantic_clue_count=_safe_len(getattr(semantic_state, "clues", [])),
        )

    def _generate_clues_and_report(self, context: WorkflowContext, *, query: str) -> None:
        context.refinement_state = self.refinement_service.run(
            query=query,
            run_state=context.run_state,
            retrieval_state=context.retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
        )
        self._append_execution_flow_decisions(context)
        self._set_main_stage(
            context,
            "clue_generation_report",
            status="completed",
            high_quality_count=_safe_len(getattr(context.refinement_state, "high_quality_clues", [])),
            candidate_count=_safe_len(getattr(context.refinement_state, "candidate_clues", [])),
            refined_count=int(getattr(context.refinement_state, "actual_refined_count", 0) or 0),
        )
        context.execution_summary = self.execution_summary_service.build(
            run_state=context.run_state,
            retrieval_state=context.retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
            refinement_state=context.refinement_state,
        )
        context.execution_summary["main_flow_stages"] = self._ordered_main_stages(context)
        context.execution_summary["main_flow_stage_count"] = len(_MAIN_STAGE_ORDER)

    def _record_input_stage(
        self,
        context: WorkflowContext,
        records: list[Mapping[str, Any] | Any],
        available_sources: list[Mapping[str, Any]],
    ) -> None:
        self._set_main_stage(
            context,
            "input_task",
            status="accepted",
            query_present=bool(context.query.strip()),
            provided_record_count=len(records),
            available_source_count=len(available_sources),
        )

    @staticmethod
    def _assets_satisfied(context: WorkflowContext) -> bool:
        return InvestigationWorkflow._preflight_satisfied(
            retrieval_state=context.retrieval_state,
            semantic_state=context.semantic_state,
        )

    @staticmethod
    def _record_preflight_satisfied(context: WorkflowContext) -> None:
        run_state = context.run_state
        if not hasattr(run_state, "flow_decision_traces"):
            return
        evidence_gap = getattr(context.semantic_state, "evidence_gap", None)
        run_state.flow_decision_traces.append(
            {
                "stage": "preflight_evidence_gap",
                "next_action": "skip_conditional_planning",
                "reason": "asset_retrieval_satisfied_evidence_gap",
                "evidence_gap": evidence_gap.model_dump() if hasattr(evidence_gap, "model_dump") else evidence_gap,
            }
        )

    @staticmethod
    def _skipped_phase_payload(semantic_state: Any) -> dict[str, Any]:
        phase_payload = getattr(semantic_state, "phase_payload", None)
        if isinstance(phase_payload, Mapping):
            payload = dict(phase_payload)
            payload["pipeline_skipped_reason"] = "history_assets_sufficient"
            return payload
        return {
            "status": "completed",
            "mode": "history_asset_retrieval",
            "input_count": 0,
            "accepted_count": 0,
            "dropped_count": 0,
            "classification_count": 0,
            "entity_count": 0,
            "cluster_count": 0,
            "risk_clue_count": 0,
            "playbook_count": 0,
            "strategy_count": 0,
            "pipeline_skipped_reason": "history_assets_sufficient",
        }

    @staticmethod
    def _set_main_stage(context: WorkflowContext, stage: str, **payload: Any) -> None:
        normalized = {"stage": stage, **{key: value for key, value in payload.items() if value is not None}}
        for index, item in enumerate(context.main_flow_stages):
            if item.get("stage") == stage:
                context.main_flow_stages[index] = {**item, **normalized}
                return
        context.main_flow_stages.append(normalized)

    @staticmethod
    def _ordered_main_stages(context: WorkflowContext) -> list[dict[str, Any]]:
        by_stage = {str(item.get("stage")): dict(item) for item in context.main_flow_stages}
        return [by_stage[stage] for stage in _MAIN_STAGE_ORDER if stage in by_stage]

    @staticmethod
    def _preflight_satisfied(*, retrieval_state: Any, semantic_state: Any) -> bool:
        if bool(getattr(retrieval_state, "provided_records", [])):
            return False
        has_assets = bool(getattr(retrieval_state, "retrieved_clues", [])) or bool(getattr(semantic_state, "clues", []))
        gap = getattr(semantic_state, "evidence_gap", None)
        return bool(has_assets and getattr(gap, "is_sufficient", False))

    @staticmethod
    def _append_execution_flow_decisions(context: WorkflowContext) -> None:
        run_state = context.run_state
        retrieval_state = context.retrieval_state
        semantic_state = context.semantic_state
        live_state = context.live_state
        fresh_state = context.fresh_state
        refinement_state = context.refinement_state
        if not hasattr(run_state, "flow_decision_traces"):
            return

        def append(stage: str, next_action: str, reason: str, **extra: Any) -> None:
            trace = {"stage": stage, "next_action": next_action, "reason": reason, **extra}
            key = (trace["stage"], trace["next_action"], trace["reason"])
            existing = {
                (item.get("stage"), item.get("next_action"), item.get("reason"))
                for item in run_state.flow_decision_traces
            }
            if key not in existing:
                run_state.flow_decision_traces.append(trace)

        provided_records = list(getattr(retrieval_state, "provided_records", []) or [])
        if provided_records:
            append(
                "provided_records",
                "run_fresh_processing",
                "user_provided_records_bypass_asset_preflight",
                provided_record_count=len(provided_records),
            )

        semantic_records = list(getattr(semantic_state, "records", []) or [])
        semantic_clues = list(getattr(semantic_state, "clues", []) or [])
        if semantic_records or semantic_clues:
            append(
                "semantic_local_satisfied",
                "merge_or_process_local_evidence",
                "semantic_local_records_or_clues_available",
                semantic_record_count=len(semantic_records),
                semantic_clue_count=len(semantic_clues),
            )
        else:
            append("semantic_local_empty", "consider_live_collection", "no_semantic_local_records_or_clues")

        live_records = list(getattr(live_state, "records", []) or [])
        if live_records:
            append(
                "live_collection_started",
                "run_fresh_processing",
                "live_collection_returned_records",
                live_record_count=len(live_records),
            )
        else:
            append(
                "live_collection_disabled",
                "run_fresh_processing" if provided_records else "continue_without_live_collection",
                "provided_records_bypass_live_collection" if provided_records else "live_collection_disabled_or_not_required",
                live_collection_reasons=list(getattr(live_state, "live_collection_reasons", []) or []),
            )

        fresh_records = list(getattr(fresh_state, "records", []) or [])
        built_clues = list(getattr(fresh_state, "built_clues", []) or [])
        if fresh_records or built_clues:
            append(
                "fresh_processing_started",
                "refine_candidates",
                "fresh_records_or_built_clues_available",
                fresh_record_count=len(fresh_records),
                built_clue_count=len(built_clues),
            )
        else:
            append("fresh_processing_skipped", "refine_candidates", "no_fresh_records_or_built_clues")

        refined_count = int(getattr(refinement_state, "actual_refined_count", 0) or 0)
        requested_refine = int(getattr(refinement_state, "requested_max_refine", 0) or 0)
        effective_refine = int(getattr(refinement_state, "effective_max_refine", 0) or 0)
        merged_candidates = list(getattr(refinement_state, "merged_candidates", []) or [])
        if refined_count > 0:
            append("clue_refine_started", "finish_report", "llm_refine_applied", refined_clue_count=refined_count)
        else:
            reason = (
                "policy_or_budget_disabled_refine"
                if requested_refine <= 0 or effective_refine <= 0
                else "no_refine_candidates"
                if not merged_candidates
                else "deterministic_candidates_did_not_require_refine"
            )
            append("refine_skipped", "finish_report", reason, merged_candidate_count=len(merged_candidates))


def _safe_len(value: Any) -> int:
    try:
        return len(value or [])
    except TypeError:
        return 0


__all__ = ["InvestigationWorkflow"]
