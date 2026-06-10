"""Offline candidate clue builder over the canonical intelligence pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.enhancement.clue_quality import ClueQualityEvaluator, build_evidence_reviewability
from src.enhancement.engine import PhaseTwoThreeEngine
from src.enhancement.strategy import RiskClue
from src.domain import RunPolicyContext
from src.pipeline.intelligence_pipeline import IntelligencePipeline
from src.pipeline.stages import CorrelateStage
from storage.entity_graph import EntityGraphStore
from storage import ClueRepo, InMemoryClueRepo


@dataclass
class OfflineClueBuildResult:
    status: str
    input_count: int
    saved_clue_count: int
    high_quality_count: int
    candidate_count: int
    execution_summary: dict[str, Any] = field(default_factory=dict)
    clues: list[dict[str, Any]] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class OfflineClueBuilder:
    """Build and persist candidate clues from raw records for later retrieval."""

    def __init__(
        self,
        *,
        phase_engine: PhaseTwoThreeEngine | None = None,
        quality_evaluator: ClueQualityEvaluator | None = None,
        clue_repo: ClueRepo | None = None,
        llm_gateway: Any | None = None,
        budget_controller: Any | None = None,
        entity_graph: EntityGraphStore | None = None,
    ) -> None:
        self.phase_engine = phase_engine or PhaseTwoThreeEngine()
        self.quality_evaluator = quality_evaluator or ClueQualityEvaluator()
        self.clue_repo = clue_repo if clue_repo is not None else InMemoryClueRepo()
        self.entity_graph = entity_graph
        self.run_policy = RunPolicyContext()
        self.intelligence_pipeline = IntelligencePipeline(
            llm_gateway=llm_gateway,
            budget_controller=budget_controller,
            policy=self.run_policy,
            correlate_stage=CorrelateStage(entity_graph=self.entity_graph) if self.entity_graph is not None else None,
        )

    def set_runtime_controls(
        self,
        *,
        llm_gateway: Any | None = None,
        budget_controller: Any | None = None,
        policy: RunPolicyContext | Mapping[str, Any] | None = None,
    ) -> None:
        """Refresh per-run LLM/budget controls before building fresh clues."""

        if policy is not None:
            self.run_policy = policy if isinstance(policy, RunPolicyContext) else RunPolicyContext.model_validate(dict(policy))
        if llm_gateway is not None or budget_controller is not None or policy is not None:
            self.intelligence_pipeline = IntelligencePipeline(
                llm_gateway=llm_gateway,
                budget_controller=budget_controller,
                policy=self.run_policy,
                correlate_stage=CorrelateStage(entity_graph=self.entity_graph) if self.entity_graph is not None else None,
            )

    def build(
        self,
        records: Iterable[Mapping[str, Any] | Any],
        *,
        prompt_text: str | None = None,
        source_candidates: Iterable[Mapping[str, Any] | Any] = (),
        quality_profile: str = "balanced",
        require_cross_source: bool = False,
        require_evidence_chain: bool = True,
        policy: RunPolicyContext | Mapping[str, Any] | None = None,
    ) -> OfflineClueBuildResult:
        if policy is not None:
            self.set_runtime_controls(policy=policy)
        materialized_records = [dict(record) if isinstance(record, Mapping) else record for record in records]
        record_by_trace = {
            str(record.get("source_trace_id") or record.get("trace_id") or record.get("hash_id") or ""): record
            for record in materialized_records
            if isinstance(record, Mapping)
        }
        pipeline_result = self.intelligence_pipeline.run(
            materialized_records,
            context={
                "quality_profile": quality_profile,
                "require_cross_source": require_cross_source,
                "require_evidence_chain": require_evidence_chain,
                "policy": self.run_policy.model_dump(),
            },
        )
        actionable_clues = [_dump_mapping(item) for item in pipeline_result.clues]
        archived_weak_clues = [_dump_mapping(item) for item in getattr(pipeline_result, "archived_weak_clues", [])]
        for clue in actionable_clues:
            clue.setdefault("clue_stage", "actionable")
        for clue in archived_weak_clues:
            clue.setdefault("clue_stage", "archived_weak")
        review_pool_clues = _dedupe_clues_by_id([*actionable_clues, *archived_weak_clues])
        payload = {
            "status": "completed",
            "mode": "intelligence_pipeline",
            "pipeline_backend": "intelligence_pipeline",
            "fallback_backend": None,
            "input_count": len(materialized_records),
            "accepted_count": pipeline_result.execution_summary.get("cleaned_count", 0),
            "dropped_count": max(0, len(materialized_records) - int(pipeline_result.execution_summary.get("cleaned_count", 0) or 0)),
            "classification_count": pipeline_result.execution_summary.get("classified_count", 0),
            "entity_count": pipeline_result.execution_summary.get("entity_count", 0),
            "cluster_count": 0,
            "risk_clue_count": pipeline_result.execution_summary.get("clue_count", 0),
            "playbook_count": 0,
            "strategy_count": 0,
            "classifications": pipeline_result.classified,
            "entities": pipeline_result.entities,
            "risk_clues": actionable_clues,
            "archived_weak_clues": archived_weak_clues,
            "review_pool_clues": review_pool_clues,
            "pipeline_summary": pipeline_result.execution_summary.model_dump(),
            "no_clue_reason": None if pipeline_result.clues else "aggregation_threshold_not_met",
        }
        risk_clues = _to_risk_clues(payload["risk_clues"])
        playbooks = self.phase_engine.playbook_builder.build(risk_clues, materialized_records)
        strategies = self.phase_engine.strategy_planner.plan(risk_clues, playbooks)
        payload["playbooks"] = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in playbooks]
        payload["strategies"] = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in strategies]
        payload["playbook_count"] = len(playbooks)
        payload["strategy_count"] = len(strategies)
        self.phase_engine._last_run_payload = payload
        assessments = self.quality_evaluator.evaluate_many(
            payload.get("risk_clues", []),
            classifications=payload.get("classifications", []),
            entities=payload.get("entities", []),
            quality_profile=quality_profile,
            require_cross_source=require_cross_source,
            require_evidence_chain=require_evidence_chain,
        )
        by_id = {item.clue_id: item for item in assessments}
        saved: list[dict[str, Any]] = []
        high_quality_count = 0
        actionable_ids = {str(clue.get("clue_id") or "") for clue in payload.get("risk_clues", [])}
        for clue in payload.get("review_pool_clues", []):
            enriched = dict(clue)
            assessment = by_id.get(str(clue.get("clue_id") or ""))
            if assessment is not None:
                enriched["quality"] = assessment.model_dump()
                enriched["quality_score"] = assessment.quality_score
                enriched["quality_level"] = assessment.quality_level
            evidence_trace_ids = [str(item) for item in (clue.get("evidence_trace_ids") or [])]
            source_types = sorted(
                {
                    str((record_by_trace.get(trace_id) or {}).get("source_type") or "").strip()
                    for trace_id in evidence_trace_ids
                    if str((record_by_trace.get(trace_id) or {}).get("source_type") or "").strip()
                }
            )
            publish_times = [
                str((record_by_trace.get(trace_id) or {}).get("publish_time") or "")
                for trace_id in evidence_trace_ids
                if str((record_by_trace.get(trace_id) or {}).get("publish_time") or "")
            ]
            if source_types:
                enriched["source_types"] = source_types
            if publish_times:
                enriched["first_seen"] = min(publish_times)
                enriched["last_seen"] = max(publish_times)
            enriched["evidence_reviewability"] = build_evidence_reviewability(
                enriched,
                assessment=assessment,
                classifications=payload.get("classifications", []),
                entities=payload.get("entities", []),
                records=materialized_records,
            )
            enriched["evidence_cards"] = enriched["evidence_reviewability"].get("evidence_cards") or []
            self.clue_repo.save(enriched)
            saved.append(enriched)
            if str(clue.get("clue_id") or "") in actionable_ids and assessment is not None and assessment.pass_threshold:
                high_quality_count += 1
        return OfflineClueBuildResult(
            status="completed",
            input_count=len(materialized_records),
            saved_clue_count=len(saved),
            high_quality_count=high_quality_count,
            candidate_count=len(saved) - high_quality_count,
            execution_summary={
                key: value
                for key, value in payload.items()
                if key
                in {
                    "status",
                    "mode",
                    "pipeline_backend",
                    "fallback_backend",
                    "no_clue_reason",
                    "input_count",
                    "accepted_count",
                    "dropped_count",
                    "classification_count",
                    "entity_count",
                    "cluster_count",
                    "risk_clue_count",
                    "playbook_count",
                    "strategy_count",
                }
            },
            clues=saved,
        )


__all__ = ["OfflineClueBuildResult", "OfflineClueBuilder"]


def _to_risk_clues(items: Iterable[Mapping[str, Any] | Any]) -> list[RiskClue]:
    """Convert pipeline clue dictionaries back to strategy-layer dataclasses."""

    risk_clues: list[RiskClue] = []
    for item in items:
        payload = _dump_mapping(item)
        clue_kwargs: dict[str, Any] = {
            "clue_id": str(payload.get("clue_id") or ""),
            "clue_type": str(payload.get("clue_type") or ""),
            "key": str(payload.get("key") or ""),
            "risk_category": str(payload.get("risk_category") or "unknown"),
            "evidence_trace_ids": _string_list(payload.get("evidence_trace_ids")),
            "source_names": _string_list(payload.get("source_names")),
            "entity_values": _string_list(payload.get("entity_values")),
            "confidence": _float(payload.get("confidence"), 0.0),
            "threshold_reason": str(payload.get("threshold_reason") or ""),
        }
        if payload.get("created_at"):
            clue_kwargs["created_at"] = str(payload.get("created_at"))
        risk_clues.append(RiskClue(**clue_kwargs))
    return risk_clues


def _dedupe_clues_by_id(items: Iterable[Mapping[str, Any] | Any]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        clue = _dump_mapping(item)
        clue_id = str(clue.get("clue_id") or "")
        if not clue_id:
            continue
        deduped[clue_id] = clue
    return list(deduped.values())


def _dump_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [str(value)]
    try:
        return [str(item) for item in value if str(item).strip()]
    except TypeError:
        return [str(value)]


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
