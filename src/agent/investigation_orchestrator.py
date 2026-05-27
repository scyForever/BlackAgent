"""LLM-driven investigation orchestration over the existing BlackAgent pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from src.backend import LLMGateway
from src.enhancement.clue_quality import ClueQualityEvaluator
from src.enhancement.engine import PhaseTwoThreeEngine
from src.enhancement.llm_clue_refiner import LLMClueRefiner
from src.pipeline import OfflineClueBuilder
from src.retrieval import ClueRetriever
from storage import ClueRepo, InMemoryClueRepo

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
    ) -> None:
        self.llm_gateway = llm_gateway
        self.phase_engine = phase_engine or PhaseTwoThreeEngine()
        self.quality_evaluator = quality_evaluator or ClueQualityEvaluator()
        self.clue_repo = clue_repo if clue_repo is not None else InMemoryClueRepo()
        self.clue_retriever = clue_retriever or ClueRetriever()
        self.clue_refiner = LLMClueRefiner(llm_gateway)
        self.offline_builder = OfflineClueBuilder(
            phase_engine=self.phase_engine,
            quality_evaluator=self.quality_evaluator,
            clue_repo=self.clue_repo,
        )
        self.intent_parser = LLMUserRequestParser(llm_gateway)
        self.planner = LLMInvestigationPlanner(llm_gateway)

    def run(
        self,
        query: str,
        *,
        records: Iterable[Mapping[str, Any] | Any] = (),
        available_sources: Iterable[Mapping[str, Any]] = (),
        collect_source_records: SourceCollector | None = None,
        max_sources: int = 5,
        retrieval_filters: Mapping[str, Any] | None = None,
    ) -> InvestigationRunResult:
        intent, intent_trace = self.intent_parser.parse(query)
        available_sources_list = [dict(source) for source in available_sources]
        plan, plan_trace = self.planner.plan(query, intent, available_sources=available_sources_list)
        budget = self._resolve_budget(plan.model_dump(), explicit_max_sources=max_sources)
        retrieval_filters = dict(retrieval_filters or {})
        selected_sources = self._select_sources(plan.model_dump(), available_sources_list, max_sources=budget["max_sources"])
        retrieved_clues = self.clue_retriever.retrieve(
            self.clue_repo.list(),
            query=query,
            intent=intent.model_dump(),
            limit=budget["max_candidate_clues"],
            time_range_hours=self._optional_positive_int(retrieval_filters.get("time_range_hours")),
            allowed_source_types=retrieval_filters.get("source_types") or (),
            allowed_risk_types=retrieval_filters.get("risk_types") or (),
            min_quality_score=self._optional_float(retrieval_filters.get("min_quality_score")),
        )

        if retrieved_clues:
            refined_high_quality, refined_candidates, refine_traces = self._refine_retrieved_clues(
                retrieved_clues,
                query=query,
                intent=intent.model_dump(),
                max_refine=budget["max_llm_refine_clues"],
            )
            return InvestigationRunResult(
                status="completed",
                mode="llm_driven_investigation",
                query=query,
                input_count=0,
                fetched_count=0,
                selected_source_count=len(selected_sources),
                high_quality_count=len(refined_high_quality),
                candidate_count=len(refined_candidates),
                intent=intent.model_dump(),
                investigation_plan=plan.model_dump(),
                llm_traces=[intent_trace.model_dump(), plan_trace.model_dump(), *refine_traces],
                selected_sources=selected_sources,
                collection_runs=[],
                execution_summary={
                    "status": "retrieved_from_clue_pool",
                    "mode": "candidate_clue_retrieval",
                    "candidate_clue_hits": len(retrieved_clues),
                    "refined_clue_count": min(len(retrieved_clues), budget["max_llm_refine_clues"]),
                    "budget": budget,
                },
                high_quality_clues=refined_high_quality,
                candidate_clues=refined_candidates,
            )

        collected_records = [dict(record) if isinstance(record, Mapping) else record for record in records]
        if len(collected_records) > budget["max_raw_records"]:
            collected_records = collected_records[: budget["max_raw_records"]]
        collection_runs: list[dict[str, Any]] = []
        if not collected_records and collect_source_records is not None:
            for source in selected_sources:
                fetched = collect_source_records(source)
                remaining = budget["max_raw_records"] - len(collected_records)
                if remaining <= 0:
                    break
                trimmed = fetched[:remaining]
                collected_records.extend(trimmed)
                collection_runs.append(
                    {
                        "source_name": source.get("source_name"),
                        "source_type": source.get("source_type"),
                        "fetched_count": len(trimmed),
                    }
                )
                if len(collected_records) >= budget["max_raw_records"]:
                    break

        if collected_records:
            build_result = self.offline_builder.build(
                collected_records,
                prompt_text=query,
                source_candidates=selected_sources or available_sources_list,
                quality_profile=intent.quality_profile,
                require_cross_source=intent.require_cross_source,
                require_evidence_chain=intent.require_evidence_chain,
            )
            phase_payload = build_result.execution_summary
            built_clues = build_result.clues
        else:
            phase_payload = {
                "status": "completed",
                "mode": "phase2_phase3_enhancement",
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
            built_clues = []

        high_quality_clues: list[dict[str, Any]] = []
        candidate_clues: list[dict[str, Any]] = []
        refined_high_quality, refined_candidates, refine_traces = self._refine_retrieved_clues(
            built_clues,
            query=query,
            intent=intent.model_dump(),
            max_refine=budget["max_llm_refine_clues"],
        )
        high_quality_clues.extend(refined_high_quality)
        candidate_clues.extend(refined_candidates)

        return InvestigationRunResult(
            status="completed" if collected_records else "no_data",
            mode="llm_driven_investigation",
            query=query,
            input_count=len(collected_records),
            fetched_count=len(collected_records),
            selected_source_count=len(selected_sources),
            high_quality_count=len(high_quality_clues),
            candidate_count=len(candidate_clues),
            intent=intent.model_dump(),
            investigation_plan=plan.model_dump(),
            llm_traces=[intent_trace.model_dump(), plan_trace.model_dump(), *refine_traces],
            selected_sources=selected_sources,
            collection_runs=collection_runs,
            execution_summary={
                key: value
                for key, value in {**phase_payload, "budget": budget, "refined_clue_count": min(len(built_clues), budget["max_llm_refine_clues"])}.items()
                if key
                in {
                    "status",
                    "mode",
                    "input_count",
                    "accepted_count",
                    "dropped_count",
                    "classification_count",
                    "entity_count",
                    "cluster_count",
                    "risk_clue_count",
                    "playbook_count",
                    "strategy_count",
                    "budget",
                    "refined_clue_count",
                }
            },
            high_quality_clues=high_quality_clues,
            candidate_clues=candidate_clues,
        )

    def _select_sources(
        self,
        plan: Mapping[str, Any],
        available_sources: list[dict[str, Any]],
        *,
        max_sources: int,
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
        chosen = [dict(source) for score, source in scored if score > 0][:max_sources]
        if chosen:
            return chosen
        return [dict(source) for _, source in scored[:max_sources]]

    def _resolve_budget(self, plan: Mapping[str, Any], *, explicit_max_sources: int) -> dict[str, int]:
        raw = plan.get("budget") or {}
        if not isinstance(raw, Mapping):
            raw = {}
        budget = {
            "max_sources": self._positive_int(raw.get("max_sources"), explicit_max_sources),
            "max_raw_records": self._positive_int(raw.get("max_raw_records"), 5000),
            "max_candidate_clues": self._positive_int(raw.get("max_candidate_clues"), max(20, explicit_max_sources * 10)),
            "max_llm_refine_clues": self._positive_int(raw.get("max_llm_refine_clues"), 20),
            "max_elapsed_seconds": self._positive_int(raw.get("max_elapsed_seconds"), 20),
        }
        budget["max_sources"] = min(budget["max_sources"], explicit_max_sources) if explicit_max_sources > 0 else budget["max_sources"]
        budget["max_llm_refine_clues"] = min(budget["max_llm_refine_clues"], budget["max_candidate_clues"])
        return budget

    def _refine_retrieved_clues(
        self,
        clues: list[dict[str, Any]],
        *,
        query: str,
        intent: Mapping[str, Any],
        max_refine: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        refined: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        for index, clue in enumerate(clues):
            item = dict(clue)
            if index < max_refine:
                item, trace = self.clue_refiner.refine(item, query=query, intent=intent)
                traces.append(trace)
            refined.append(item)
            self.clue_repo.save(item)
        high_quality = [clue for clue in refined if bool(((clue.get("quality") or {}).get("pass_threshold")) or float(clue.get("quality_score") or 0.0) >= 0.78)]
        candidates = [clue for clue in refined if clue not in high_quality]
        return high_quality, candidates, traces

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


__all__ = ["InvestigationOrchestrator", "InvestigationRunResult"]
