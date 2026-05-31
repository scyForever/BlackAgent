"""Candidate clue ranking for top-K LLM refinement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RankedClue:
    clue: dict[str, Any]
    score: float
    reasons: list[str]


class ClueRanker:
    """Rank clues so LLM budget is spent on high-value ambiguous cards."""

    def rank(self, clues: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        ranked = [self.score(clue) for clue in clues]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return [item.clue for item in ranked]

    def score(self, clue: Mapping[str, Any]) -> RankedClue:
        item = dict(clue)
        quality_score = _float(item.get("quality_score"))
        confidence = _float(item.get("confidence"))
        evidence_count = len({str(value) for value in (item.get("evidence_trace_ids") or []) if str(value).strip()})
        cross_source_count = len({str(value) for value in (item.get("source_names") or []) if str(value).strip()})
        entity_values = [str(value) for value in (item.get("entity_values") or []) if str(value).strip()]
        quality = item.get("quality") if isinstance(item.get("quality"), Mapping) else {}
        already_high_quality = quality_score >= 0.85 and bool(quality.get("pass_threshold", True))
        score = (
            0.35 * quality_score
            + 0.25 * confidence
            + 0.20 * min(evidence_count, 4) / 4.0
            + 0.10 * min(cross_source_count, 3) / 3.0
            + 0.10 * min(len(entity_values), 4) / 4.0
            - (0.20 if already_high_quality and item.get("refinement") else 0.0)
        )
        reasons: list[str] = []
        if evidence_count >= 2:
            reasons.append("evidence_chain")
        if cross_source_count >= 2:
            reasons.append("cross_source")
        if entity_values:
            reasons.append("entities_present")
        if bool(quality.get("review_required")) or item.get("quality_level") != "high":
            reasons.append("reviewable")
        item["refine_priority_score"] = round(max(score, 0.0), 4)
        item["refine_priority_reasons"] = reasons
        return RankedClue(clue=item, score=item["refine_priority_score"], reasons=reasons)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["ClueRanker", "RankedClue"]
