"""Quality scoring for aggregated risk clues."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from src.collector.base_collector import get_record_field


@dataclass(frozen=True)
class ClueQualityAssessment:
    clue_id: str
    quality_score: float
    quality_level: str
    pass_threshold: bool
    review_required: bool
    cross_source_count: int
    evidence_count: int
    avg_classification_confidence: float
    critical_entity_count: int
    quality_reasons: list[str]

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class ClueQualityEvaluator:
    """Score clue usability against user-intent quality expectations."""

    CRITICAL_ENTITY_TYPES = {"contact", "account", "url", "domain"}

    def evaluate_many(
        self,
        clues: Iterable[Mapping[str, Any] | Any],
        *,
        classifications: Iterable[Mapping[str, Any] | Any],
        entities: Iterable[Mapping[str, Any] | Any],
        quality_profile: str,
        require_cross_source: bool,
        require_evidence_chain: bool,
    ) -> list[ClueQualityAssessment]:
        conf_by_trace = {
            str(get_record_field(item, "source_trace_id") or ""): float(get_record_field(item, "confidence") or 0.0)
            for item in classifications
        }
        entities_by_trace: dict[str, list[Any]] = {}
        for entity in entities:
            trace_id = str(get_record_field(entity, "source_trace_id") or "")
            entities_by_trace.setdefault(trace_id, []).append(entity)

        return [
            self.evaluate_one(
                clue,
                conf_by_trace=conf_by_trace,
                entities_by_trace=entities_by_trace,
                quality_profile=quality_profile,
                require_cross_source=require_cross_source,
                require_evidence_chain=require_evidence_chain,
            )
            for clue in clues
        ]

    def evaluate_one(
        self,
        clue: Mapping[str, Any] | Any,
        *,
        conf_by_trace: Mapping[str, float],
        entities_by_trace: Mapping[str, list[Any]],
        quality_profile: str,
        require_cross_source: bool,
        require_evidence_chain: bool,
    ) -> ClueQualityAssessment:
        clue_id = str(get_record_field(clue, "clue_id") or "unknown_clue")
        evidence_trace_ids = [str(item) for item in (get_record_field(clue, "evidence_trace_ids") or [])]
        source_names = [str(item) for item in (get_record_field(clue, "source_names") or [])]
        base_confidence = float(get_record_field(clue, "confidence") or 0.0)
        avg_confidence = 0.0
        if evidence_trace_ids:
            avg_confidence = round(sum(conf_by_trace.get(trace_id, 0.0) for trace_id in evidence_trace_ids) / len(evidence_trace_ids), 4)

        critical_entity_count = 0
        for trace_id in evidence_trace_ids:
            for entity in entities_by_trace.get(trace_id, []):
                entity_type = str(get_record_field(entity, "entity_type") or "").lower()
                if entity_type in self.CRITICAL_ENTITY_TYPES:
                    critical_entity_count += 1

        cross_source_count = len(set(source_names))
        evidence_count = len(set(evidence_trace_ids))
        score = (
            base_confidence * 0.45
            + min(evidence_count, 4) / 4.0 * 0.2
            + min(cross_source_count, 3) / 3.0 * 0.2
            + avg_confidence * 0.15
        )
        reasons: list[str] = []
        if cross_source_count >= 2:
            reasons.append("cross_source_confirmed")
        if evidence_count >= 3:
            reasons.append("sufficient_evidence_samples")
        if critical_entity_count > 0:
            reasons.append("critical_entities_present")
        if avg_confidence >= 0.75:
            reasons.append("classification_confidence_stable")

        if require_cross_source and cross_source_count < 2:
            score -= 0.2
            reasons.append("missing_required_cross_source")
        if require_evidence_chain and evidence_count < 2:
            score -= 0.15
            reasons.append("weak_evidence_chain")
        if critical_entity_count == 0:
            score -= 0.15
            reasons.append("missing_critical_entities")

        score = round(max(0.0, min(score, 0.99)), 4)
        threshold = _quality_threshold(quality_profile)
        passed = score >= threshold
        level = "high" if score >= max(threshold, 0.82) else ("medium" if score >= threshold else "low")
        return ClueQualityAssessment(
            clue_id=clue_id,
            quality_score=score,
            quality_level=level,
            pass_threshold=passed,
            review_required=(not passed) or level != "high",
            cross_source_count=cross_source_count,
            evidence_count=evidence_count,
            avg_classification_confidence=avg_confidence,
            critical_entity_count=critical_entity_count,
            quality_reasons=reasons,
        )


def _quality_threshold(profile: str) -> float:
    normalized = str(profile or "").strip().lower()
    if normalized == "high_precision":
        return 0.78
    if normalized == "high_recall":
        return 0.52
    return 0.65


__all__ = ["ClueQualityAssessment", "ClueQualityEvaluator"]
