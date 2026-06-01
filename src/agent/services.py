"""Small services that keep investigation orchestration steps explicit."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Callable, Iterable, Mapping

from src.agent.budget_controller import BudgetController, RuntimeBudget
from src.domain import RunPolicyContext
from src.enhancement.llm_clue_refiner import LLMClueRefiner
from src.agent.clue_ranker import ClueRanker
from src.agent.model_router import ModelRouter
from .user_request_parser import _fallback_intent, _fallback_plan


class IntentPlanningService:
    """Owns the high-level intent/plan stage name for future extraction."""

    name = "intent_planning"


class SourceSelectionService:
    """Select and cap authorized source candidates."""

    def cap(self, sources: Iterable[Mapping[str, Any]], limit: int | None) -> list[dict[str, Any]]:
        materialized = [dict(source) for source in sources]
        if isinstance(limit, int) and limit > 0:
            return materialized[:limit]
        return materialized


class ClueMergeService:
    """Merge clue candidates by a stable clue type/key/category tuple."""

    def merge(self, clues: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for clue in clues:
            item = dict(clue)
            key = (
                str(item.get("clue_type") or "").lower(),
                str(item.get("key") or item.get("clue_id") or "").lower(),
                str(item.get("risk_category") or "").lower(),
            )
            if key not in merged:
                merged[key] = item
                continue
            existing = merged[key]
            for field in ("evidence_trace_ids", "source_names", "entity_values", "source_types"):
                values = [str(value) for value in [*(existing.get(field) or []), *(item.get(field) or [])] if str(value).strip()]
                existing[field] = sorted(dict.fromkeys(values))
            existing["quality_score"] = max(float(existing.get("quality_score") or 0.0), float(item.get("quality_score") or 0.0))
            existing["confidence"] = max(float(existing.get("confidence") or 0.0), float(item.get("confidence") or 0.0))
        return list(merged.values())


class InvestigationTelemetryService:
    """Summarize LLM gateway stats by stage."""

    def summarize_llm(self, stats: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        items = [dict(item) for item in stats]
        by_stage = Counter(str(item.get("stage") or "unknown") for item in items)
        return {
            "call_count": len(items),
            "success_count": sum(1 for item in items if bool(item.get("ok"))),
            "failed_count": sum(1 for item in items if not bool(item.get("ok"))),
            "by_stage_count": dict(by_stage),
        }


class RunStatePreparationService:
    """Prepare run state from explicit collaborators."""

    name = "run_state_preparation"

    def __init__(
        self,
        *,
        routing_profiles: Mapping[str, Any],
        intent_parser: Any,
        planner: Any,
        gateway_stats_count: Callable[[], int],
        normalize_policy_override: Callable[..., Any],
        normalize_routing_profile: Callable[[str | None], str],
        routing_profile_config: Callable[[str], dict[str, Any]],
        effective_investigation_config: Callable[..., Any],
        planner_runtime_context: Callable[[], dict[str, Any]],
        profile_budget_defaults: Callable[[Mapping[str, Any]], dict[str, Any]],
        stage_deadline_ms: Callable[..., int],
        disabled_llm_trace: Callable[..., Any],
        runtime_quality_gate: Callable[..., Any],
        apply_profile_execution_controls: Callable[..., Any],
        plan_execution_controls: Callable[[Mapping[str, Any]], Any],
        resolve_budget: Callable[..., dict[str, Any]],
        select_sources: Callable[..., list[dict[str, Any]]],
        deadline_at: Callable[[float, int | None], float | None],
    ) -> None:
        self.routing_profiles = routing_profiles
        self.intent_parser = intent_parser
        self.planner = planner
        self.gateway_stats_count = gateway_stats_count
        self.normalize_policy_override = normalize_policy_override
        self.normalize_routing_profile = normalize_routing_profile
        self.routing_profile_config = routing_profile_config
        self.effective_investigation_config = effective_investigation_config
        self.planner_runtime_context = planner_runtime_context
        self.profile_budget_defaults = profile_budget_defaults
        self.stage_deadline_ms = stage_deadline_ms
        self.disabled_llm_trace = disabled_llm_trace
        self.runtime_quality_gate = runtime_quality_gate
        self.apply_profile_execution_controls = apply_profile_execution_controls
        self.plan_execution_controls = plan_execution_controls
        self.resolve_budget = resolve_budget
        self.select_sources = select_sources
        self.deadline_at = deadline_at

    def prepare(
        self,
        *,
        query: str,
        available_sources: Iterable[Mapping[str, Any]],
        max_sources: int | None,
        retrieval_filters: Mapping[str, Any] | None,
        routing_profile: str | None,
        policy_override: Any | None,
        run_state_type: type[Any],
    ) -> Any:
        started_at = time.perf_counter()
        gateway_stats_start = self.gateway_stats_count()
        normalized_policy_override = self.normalize_policy_override(policy_override)
        profile = self.normalize_routing_profile(routing_profile)
        profile_config = self.routing_profile_config(profile) if (routing_profile is not None or self.routing_profiles) else {}
        effective_config = self.effective_investigation_config(
            routing_profile=routing_profile,
            policy_override=normalized_policy_override,
        )
        initial_runtime_context = self.planner_runtime_context()
        budget_controller = BudgetController(RuntimeBudget.from_mapping(self.profile_budget_defaults(profile_config)))
        if bool(profile_config.get("enable_llm_intent_parse", True)):
            intent, intent_trace = self.intent_parser.parse(
                query,
                runtime_context=initial_runtime_context,
                budget=budget_controller,
                deadline_ms=self.stage_deadline_ms(profile_config, default=1500),
            )
        else:
            intent = _fallback_intent(query, runtime_context=initial_runtime_context)
            intent_trace = self.disabled_llm_trace(
                "intent_parse",
                reason="profile_disabled_llm_intent_parse",
                runtime_context=initial_runtime_context,
            )
        intent_payload = intent.model_dump()
        available_sources_list = [dict(source) for source in available_sources]
        if profile == "fast":
            plan = _fallback_plan(intent, runtime_context=initial_runtime_context)
            plan_trace = self.disabled_llm_trace(
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
                deadline_ms=self.stage_deadline_ms(profile_config, default=2500),
            )
        plan_payload = plan.model_dump()
        runtime_quality_gate = self.runtime_quality_gate(
            intent=intent_payload,
            plan=plan_payload,
            policy_override=normalized_policy_override,
        )
        plan_execution_controls = self.apply_profile_execution_controls(
            self.plan_execution_controls(plan_payload),
            profile_config=profile_config,
            profile=profile,
        )
        budget = self.resolve_budget(
            plan_payload,
            explicit_max_sources=max_sources,
            available_source_count=len(available_sources_list),
            policy_override=normalized_policy_override,
            profile_config=profile_config,
        )
        budget_controller.budget = RuntimeBudget.from_mapping(budget)
        run_policy = RunPolicyContext.from_profile_config(
            routing_profile=profile,
            profile_config=profile_config,
            budget=budget,
            quality_profile=str(intent_payload.get("quality_profile") or "balanced"),
        )
        if normalized_policy_override is not None:
            override_payload = normalized_policy_override.model_dump(exclude_none=True)
            if "enable_llm_record_enrich" in override_payload:
                run_policy = run_policy.model_copy(update={"enable_llm_record_enrich": bool(override_payload["enable_llm_record_enrich"])})
            if "enable_llm_clue_refine" in override_payload:
                run_policy = run_policy.model_copy(update={"enable_llm_clue_refine": bool(override_payload["enable_llm_clue_refine"])})
            run_policy = run_policy.model_copy(
                update={
                    "llm_stage_policy": {
                        **run_policy.llm_stage_policy,
                        "record_enrich": run_policy.enable_llm_record_enrich,
                        "clue_refine": run_policy.enable_llm_clue_refine,
                    }
                }
            )
        selected_sources = self.select_sources(
            plan_payload,
            available_sources_list,
            max_sources=budget["max_sources"],
            risk_types=intent.risk_types,
        )
        if isinstance(budget.get("max_sources"), int) and budget["max_sources"] > 0:
            selected_sources = selected_sources[: int(budget["max_sources"])]
        return run_state_type(
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
            run_policy=run_policy,
            deadline_at=self.deadline_at(started_at, budget["max_elapsed_seconds"]),
            gateway_stats_start=gateway_stats_start,
            available_sources_list=available_sources_list,
            retrieval_filters=dict(retrieval_filters or {}),
            selected_sources=selected_sources,
            llm_gateway=getattr(self.intent_parser, "llm_gateway", None),
        )


class InitialCandidateRetrievalService:
    """Retrieve and cap pool/provided candidates before live collection."""

    name = "initial_candidate_retrieval"

    def __init__(
        self,
        *,
        clue_retriever: Any,
        clue_repo: Any,
        optional_positive_int: Callable[[Any], int | None],
        optional_float: Callable[[Any], float | None],
        summarize_retrieved_clues: Callable[..., dict[str, int]],
        entity_graph: Any | None = None,
    ) -> None:
        self.clue_retriever = clue_retriever
        self.clue_repo = clue_repo
        self.optional_positive_int = optional_positive_int
        self.optional_float = optional_float
        self.summarize_retrieved_clues = summarize_retrieved_clues
        self.entity_graph = entity_graph

    def retrieve(
        self,
        *,
        query: str,
        records: Iterable[Mapping[str, Any] | Any],
        run_state: Any,
        retrieval_state_type: type[Any],
    ) -> Any:
        retrieved_clues = self.clue_retriever.retrieve(
            self.clue_repo.list(),
            query=query,
            intent=run_state.intent_payload,
            limit=run_state.budget["max_candidate_clues"],
            time_range_hours=self.optional_positive_int(run_state.retrieval_filters.get("time_range_hours")),
            allowed_source_types=run_state.retrieval_filters.get("source_types") or (),
            allowed_risk_types=run_state.retrieval_filters.get("risk_types") or (),
            min_quality_score=self.optional_float(run_state.retrieval_filters.get("min_quality_score")),
            entity_graph=self.entity_graph,
        )
        retrieved_summary = self.summarize_retrieved_clues(
            retrieved_clues,
            time_range_hours=self.optional_positive_int(run_state.retrieval_filters.get("time_range_hours"))
            or self.optional_positive_int(run_state.intent_payload.get("time_range_hours")),
            quality_gate=run_state.runtime_quality_gate,
        )
        provided_records = [dict(record) if isinstance(record, Mapping) else record for record in records]
        if len(provided_records) > run_state.budget["max_raw_records"]:
            provided_records = provided_records[: run_state.budget["max_raw_records"]]
        return retrieval_state_type(
            retrieved_clues=retrieved_clues,
            retrieved_summary=retrieved_summary,
            provided_records=provided_records,
        )


class ClueRefinementService:
    """Rank, budget, batch-refine, and persist candidate clues."""

    name = "clue_refinement"

    def __init__(
        self,
        *,
        clue_refiner: LLMClueRefiner,
        clue_ranker: ClueRanker,
        model_router: ModelRouter,
        clue_repo: Any,
        runtime_context_factory: Any,
        quality_gate_checker: Any,
        deadline_checker: Any,
    ) -> None:
        self.clue_refiner = clue_refiner
        self.clue_ranker = clue_ranker
        self.model_router = model_router
        self.clue_repo = clue_repo
        self.runtime_context_factory = runtime_context_factory
        self.quality_gate_checker = quality_gate_checker
        self.deadline_checker = deadline_checker

    def refine(
        self,
        clues: list[dict[str, Any]],
        *,
        query: str,
        intent: Mapping[str, Any],
        quality_gate: Any,
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
                and not self.deadline_checker(deadline_at)
                and _budget_peek(
                    active_budget_controller,
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
                    if self.deadline_checker(deadline_at)
                    else "llm_refine_budget_exhausted"
                    if len(pending_refine) >= max_refine
                    else "budget_controller_denied"
                )
            refined.append(item)
        if pending_refine:
            max_tokens = sum(int(meta.get("max_tokens") or 0) for meta in pending_meta) or 900
            deadline_ms = max(int(meta.get("deadline_ms") or 0) for meta in pending_meta) or None
            runtime_context = self.runtime_context_factory(intent)
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
                if meta:
                    trace["model_route_reason"] = meta.get("reason")
                    trace["model_route_priority"] = meta.get("priority")
                    trace["max_tokens_budgeted"] = meta.get("max_tokens")
            traces.extend(batch_traces)
        for item in refined:
            self.clue_repo.save(item)
        high_quality = [clue for clue in refined if self.quality_gate_checker(clue, quality_gate=quality_gate)]
        candidates = [clue for clue in refined if clue not in high_quality]
        return high_quality, candidates, traces, model_route_traces, active_budget_controller.snapshot()


__all__ = [
    "ClueMergeService",
    "ClueRefinementService",
    "InitialCandidateRetrievalService",
    "IntentPlanningService",
    "InvestigationTelemetryService",
    "RunStatePreparationService",
    "SourceSelectionService",
]


def _budget_peek(budget: BudgetController, *, stage: str, estimated_tokens: int, item_count: int = 1) -> bool:
    if hasattr(budget, "peek"):
        return bool(budget.peek(stage=stage, estimated_tokens=estimated_tokens, item_count=item_count))
    return bool(budget.allow_llm_call(stage=stage, estimated_tokens=estimated_tokens, item_count=item_count))
