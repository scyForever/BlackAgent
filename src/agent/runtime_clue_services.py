"""Clue merge, telemetry, and exploration helpers for investigation runtime."""


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





class InvestigationClueMixin:
    """Extracted helper group; state is supplied by InvestigationRuntime."""

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


    def _gateway_stats_count(self) -> int:
            if hasattr(self.llm_gateway, "stats_count"):
                try:
                    return int(self.llm_gateway.stats_count())
                except (TypeError, ValueError):
                    return 0
            return len(self._gateway_stats())


    def _gateway_stats_since(self, start_index: int) -> list[dict[str, Any]]:
            if hasattr(self.llm_gateway, "stats_since"):
                return [dict(item) for item in self.llm_gateway.stats_since(start_index)]
            return self._gateway_stats()[max(0, int(start_index or 0)) :]


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


__all__ = ["InvestigationClueMixin"]
