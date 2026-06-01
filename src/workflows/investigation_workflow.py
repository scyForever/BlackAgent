"""Thin workflow runner that owns the investigation step sequence."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.config_loader import InvestigationPolicyOverride
from .workflow_context import WorkflowContext
from .workflow_result import WorkflowResult


class InvestigationWorkflow:
    """Sequence the investigation services without embedding business logic."""

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
        if records_list:
            return self._build_planned_context(
                query,
                records=records_list,
                available_sources=available_sources_list,
                collect_source_records=collect_source_records,
                max_sources=max_sources,
                retrieval_filters=retrieval_filters,
                max_concurrent_sources=max_concurrent_sources,
                routing_profile=routing_profile,
                policy_override=policy_override,
            )
        run_state = self.run_state_preparation.prepare(
            query=query,
            available_sources=available_sources_list,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            run_state_type=self.run_state_type,
            planning_mode="preflight",
        )
        retrieval_state = self.initial_candidate_retrieval.retrieve(
            query=query,
            records=records_list,
            run_state=run_state,
            retrieval_state_type=self.retrieval_state_type,
        )
        context = WorkflowContext(query=query, run_state=run_state, retrieval_state=retrieval_state)
        context.semantic_state = self.semantic_local_retrieval.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        if not self._preflight_satisfied(retrieval_state=retrieval_state, semantic_state=context.semantic_state):
            evidence_gap = getattr(context.semantic_state, "evidence_gap", None)
            flow_decision_traces = [
                {
                    "stage": "preflight_evidence_gap",
                    "next_action": "run_conditional_planning",
                    "reason": "asset_retrieval_insufficient_for_direct_report",
                    "evidence_gap": evidence_gap.model_dump() if hasattr(evidence_gap, "model_dump") else evidence_gap,
                }
            ]
            run_state = self.run_state_preparation.prepare(
                query=query,
                available_sources=available_sources_list,
                max_sources=max_sources,
                retrieval_filters=retrieval_filters,
                routing_profile=routing_profile,
                policy_override=policy_override,
                run_state_type=self.run_state_type,
                evidence_gap=evidence_gap.model_dump() if hasattr(evidence_gap, "model_dump") else evidence_gap,
                flow_decision_traces=flow_decision_traces,
            )
            retrieval_state = self.initial_candidate_retrieval.retrieve(
                query=query,
                records=records_list,
                run_state=run_state,
                retrieval_state_type=self.retrieval_state_type,
            )
            context = WorkflowContext(query=query, run_state=run_state, retrieval_state=retrieval_state)
            context.semantic_state = self.semantic_local_retrieval.run(
                query=query,
                run_state=run_state,
                retrieval_state=retrieval_state,
                collect_source_records=collect_source_records,
            )
        else:
            run_state.flow_decision_traces.append(
                {
                    "stage": "preflight_evidence_gap",
                    "next_action": "skip_conditional_planning",
                    "reason": "asset_retrieval_satisfied_evidence_gap",
                    "evidence_gap": context.semantic_state.evidence_gap.model_dump(),
                }
            )
        context.live_state = self.live_collection_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
        )
        context.fresh_state = self.fresh_processing_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
        )
        context.refinement_state = self.refinement_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
        )
        self._append_execution_flow_decisions(context)
        context.execution_summary = self.execution_summary_service.build(
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
            refinement_state=context.refinement_state,
        )
        return context

    def _build_planned_context(
        self,
        query: str,
        *,
        records: list[Mapping[str, Any] | Any],
        available_sources: Iterable[Mapping[str, Any]],
        collect_source_records: Any | None,
        max_sources: int | None,
        retrieval_filters: Mapping[str, Any] | None,
        max_concurrent_sources: int,
        routing_profile: str | None,
        policy_override: InvestigationPolicyOverride | Mapping[str, Any] | None,
    ) -> WorkflowContext:
        run_state = self.run_state_preparation.prepare(
            query=query,
            available_sources=available_sources,
            max_sources=max_sources,
            retrieval_filters=retrieval_filters,
            routing_profile=routing_profile,
            policy_override=policy_override,
            run_state_type=self.run_state_type,
        )
        retrieval_state = self.initial_candidate_retrieval.retrieve(
            query=query,
            records=records,
            run_state=run_state,
            retrieval_state_type=self.retrieval_state_type,
        )
        context = WorkflowContext(query=query, run_state=run_state, retrieval_state=retrieval_state)
        context.semantic_state = self.semantic_local_retrieval.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            collect_source_records=collect_source_records,
        )
        context.live_state = self.live_collection_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            collect_source_records=collect_source_records,
            max_concurrent_sources=max_concurrent_sources,
        )
        context.fresh_state = self.fresh_processing_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
        )
        context.refinement_state = self.refinement_service.run(
            query=query,
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
        )
        self._append_execution_flow_decisions(context)
        context.execution_summary = self.execution_summary_service.build(
            run_state=run_state,
            retrieval_state=retrieval_state,
            semantic_state=context.semantic_state,
            live_state=context.live_state,
            fresh_state=context.fresh_state,
            refinement_state=context.refinement_state,
        )
        return context

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


__all__ = ["InvestigationWorkflow"]
