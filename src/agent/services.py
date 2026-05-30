"""Small services that keep investigation orchestration steps explicit."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


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


__all__ = ["ClueMergeService", "IntentPlanningService", "InvestigationTelemetryService", "SourceSelectionService"]
