"""Source selection and collection helpers for investigation runtime."""


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







def _normalize_source_pref(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"telegram", "tg", "电报"}:
        return "telegram"
    if text in {"forum", "论坛", "贴吧"}:
        return "forum"
    if text in {"im", "chat", "群", "私聊"}:
        return "im"
    if text in {"threat_intel", "threat-intel", "feed", "intel", "情报源"}:
        return "threat_intel"
    return text


def _source_diversity_class(source: Mapping[str, Any]) -> str:
    source_type = _normalize_source_pref(source.get("source_type"))
    platform = _normalize_source_pref(source.get("platform"))
    text = {source_type, platform}
    if text.intersection({"telegram", "im", "group"}):
        return "im_or_group"
    if text.intersection({"forum", "social", "x", "twitter", "news", "blog"}):
        return "social_or_forum"
    if text.intersection({"vertical", "technical", "techforum", "threat_intel"}):
        return "vertical_or_technical"
    return "other_authorized"


def _source_identity(source: Mapping[str, Any]) -> str:
    return "|".join(
        str(source.get(field) or "").strip().lower()
        for field in ("source_name", "source_url", "search_query")
    )


def _available_class_count(scored_sources: Iterable[tuple[int, Mapping[str, Any]]]) -> int:
    return len({_source_diversity_class(source) for _score, source in scored_sources})


class InvestigationCollectionMixin:
    """Extracted helper group; state is supplied by InvestigationRuntime."""

    def _select_sources(
            self,
            plan: Mapping[str, Any],
            available_sources: list[dict[str, Any]],
            *,
            max_sources: int | None,
            risk_types: Iterable[str] = (),
            evidence_gap: Mapping[str, Any] | EvidenceGap | None = None,
        ) -> list[dict[str, Any]]:
            if not available_sources:
                return []
            gap = evidence_gap if isinstance(evidence_gap, EvidenceGap) else EvidenceGap.from_mapping(evidence_gap)
            selected_names = {item.lower() for item in (plan.get("selected_source_names") or []) if str(item).strip()}
            strategy = plan.get("source_selection_strategy") or {}
            preferred_types = {
                _normalize_source_pref(item)
                for item in (strategy.get("preferred_source_types") or [])
                if _normalize_source_pref(item)
            }
            gap_preferred_types = {
                _normalize_source_pref(item)
                for item in [*gap.preferred_source_types, *gap.need_specific_source_types]
                if _normalize_source_pref(item)
            }
            if gap_preferred_types:
                preferred_types = preferred_types.union(gap_preferred_types)
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
                if gap.need_cross_source_support and source_type in {"im", "forum", "threat_intel", "telegram"}:
                    score += 2
                if gap.missing_entity_types:
                    score += self._source_gap_score(source, gap)
                if any(keyword in source_theme or keyword in source_query or keyword in source_name.lower() for keyword in match_keywords):
                    score += 1
                scored.append((score, source))
    
            scored.sort(key=lambda item: (item[0], str(item[1].get("source_name") or "")), reverse=True)
            fallback = [dict(source) for _, source in scored]
            if max_sources is None or max_sources >= len(fallback):
                selected = fallback
            else:
                chosen = self._diverse_source_slice(
                    [(score, source) for score, source in scored if score > 0],
                    max_sources=max_sources,
                )
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


    @staticmethod
    def _diverse_source_slice(scored_sources: list[tuple[int, Mapping[str, Any]]], *, max_sources: int | None) -> list[dict[str, Any]]:
            if max_sources is None or max_sources <= 0:
                return [dict(source) for _score, source in scored_sources]
            limit = int(max_sources)
            selected: list[dict[str, Any]] = []
            selected_keys: set[str] = set()
            selected_classes: set[str] = set()
    
            def add(source: Mapping[str, Any]) -> None:
                key = _source_identity(source)
                if key in selected_keys or len(selected) >= limit:
                    return
                selected.append(dict(source))
                selected_keys.add(key)
                selected_classes.add(_source_diversity_class(source))
    
            # First pass: keep the score order, but reserve slots for distinct
            # source classes so a Telegram-heavy catalog does not crowd out
            # forum/vertical evidence when the budget allows more than one source.
            for _score, source in scored_sources:
                source_class = _source_diversity_class(source)
                if source_class in selected_classes and len(selected_classes) < _available_class_count(scored_sources):
                    continue
                add(source)
                if len(selected) >= limit:
                    return selected
    
            # Second pass: fill the remaining budget by the original score order.
            for _score, source in scored_sources:
                add(source)
                if len(selected) >= limit:
                    break
            return selected


    @staticmethod
    def _source_diversity_class(source: Mapping[str, Any]) -> str:
            return _source_diversity_class(source)


    @staticmethod
    def _source_gap_score(source: Mapping[str, Any], gap: EvidenceGap) -> int:
            text = " ".join(
                str(source.get(field) or "").lower()
                for field in ("source_name", "source_type", "query_theme", "search_query", "source_url", "entity_focus")
            )
            missing = {str(item).lower() for item in gap.missing_entity_types}
            score = 0
            if {"contact", "account"}.intersection(missing) and any(token in text for token in ("tg", "telegram", "im", "群", "contact")):
                score += 2
            if {"url", "domain"}.intersection(missing) and any(token in text for token in ("http", "domain", "url", "site", "forum")):
                score += 2
            if "tool_name" in missing and any(token in text for token in ("tool", "脚本", "群控", "工具")):
                score += 2
            return score


    def _collect_records_from_sources(
            self,
            selected_sources: list[dict[str, Any]],
            *,
            collect_source_records: SourceCollector,
            max_raw_records: int,
            max_concurrent_sources: int,
            deadline_at: float | None = None,
            layer_recheck: Any | None = None,
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            collection_runs: list[dict[str, Any]] = []
            collected_records: list[dict[str, Any]] = []
            if not selected_sources or max_raw_records <= 0:
                return collected_records, collection_runs
    
            grouped_sources = []
            for layer_name, layer_sources in group_sources_by_collection_layer(selected_sources):
                allowed_sources, blocked_runs = self._filter_sources_for_collection(layer_sources)
                collection_runs.extend(blocked_runs)
                grouped_sources.append((layer_name, allowed_sources))
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
                if layer_recheck is not None and layer_runs:
                    should_stop, stop_reason, gap_payload = layer_recheck(collected_records)
                    for run in layer_runs:
                        run["layer_stop_reason"] = stop_reason
                        run["evidence_gap_after_layer"] = gap_payload
                    if should_stop:
                        break
                if len(collected_records) >= max_raw_records:
                    for run in layer_runs:
                        run.setdefault("layer_stop_reason", "max_raw_records_reached")
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


    def _collect_one_source(
            self,
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
                self.source_policy_guard.validate_for_collection(source_payload)
            except Exception as exc:
                run["status"] = "blocked_by_source_policy"
                run["error"] = str(exc)
                run["reason"] = getattr(exc, "rule", None) or str(exc)
                return [], run
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
            run["status"] = "completed"
            return records, run


    def _filter_sources_for_collection(
            self,
            selected_sources: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            allowed: list[dict[str, Any]] = []
            blocked: list[dict[str, Any]] = []
            for source in selected_sources:
                payload = dict(source)
                decision = self.source_policy_guard.check(payload)
                if decision.allowed:
                    allowed.append(payload)
                    continue
                blocked.append(
                    {
                        "source_name": str(payload.get("source_name") or "unknown_source"),
                        "source_type": str(payload.get("source_type") or ""),
                        "collection_layer": str(payload.get("collection_layer") or ""),
                        "fetched_count": 0,
                        "status": "blocked_by_source_policy",
                        "reason": decision.reason,
                        "error": decision.reason,
                    }
                )
            return allowed, blocked


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


__all__ = ["InvestigationCollectionMixin"]
