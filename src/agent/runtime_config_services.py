"""Routing, budget, and config helpers for investigation runtime."""


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





class InvestigationConfigMixin:
    """Extracted helper group; state is supplied by InvestigationRuntime."""

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
            plan_is_fallback = plan.get("llm_ok") is False or plan.get("llm_reason") == "fallback_plan"
            if not plan_is_fallback:
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
                    "enable_llm_record_enrich": False,
                    "enable_llm_clue_refine": True,
                    "llm_stage_policy": {
                        "intent_parse": False,
                        "investigation_plan": False,
                        "source_query_rewrite": False,
                        "record_enrich": False,
                        "clue_refine": True,
                    },
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
                    "enable_llm_record_enrich": True,
                    "enable_llm_clue_refine": True,
                    "llm_stage_policy": {
                        "intent_parse": True,
                        "investigation_plan": True,
                        "source_query_rewrite": True,
                        "record_enrich": True,
                        "clue_refine": True,
                    },
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
                    "enable_llm_record_enrich": True,
                    "enable_llm_clue_refine": True,
                    "llm_stage_policy": {
                        "intent_parse": True,
                        "investigation_plan": True,
                        "source_query_rewrite": True,
                        "record_enrich": True,
                        "clue_refine": True,
                    },
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


__all__ = ["InvestigationConfigMixin"]
