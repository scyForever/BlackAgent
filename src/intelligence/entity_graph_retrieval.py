"""Preflight entity-graph retrieval for evidence-gap decisions."""

from __future__ import annotations

from typing import Any, Mapping

from storage.entity_graph import EntityGraphStore


class EntityGraphRetrievalService:
    """Return graph-backed clue candidates before live collection starts."""

    def __init__(self, graph: EntityGraphStore | None = None) -> None:
        self.graph = graph

    def retrieve(
        self,
        *,
        query: str,
        intent: Mapping[str, Any] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if self.graph is None:
            return []
        query_tokens = _tokens(query)
        risk_types = {str(item).lower() for item in ((intent or {}).get("risk_types") or []) if str(item).strip()}
        scored: list[tuple[float, dict[str, Any]]] = []
        for raw in self.graph.generate_clues():
            clue = dict(raw)
            clue["retrieval_source"] = "entity_graph_preflight"
            clue.setdefault("orchestration_origins", [])
            if "entity_graph" not in clue["orchestration_origins"]:
                clue["orchestration_origins"] = [*clue["orchestration_origins"], "entity_graph"]
            text = " ".join(
                [
                    str(clue.get("clue_type") or ""),
                    str(clue.get("risk_category") or ""),
                    str(clue.get("reason") or ""),
                    " ".join(str(item) for item in (clue.get("entity_values") or [])),
                ]
            )
            overlap = len(query_tokens.intersection(_tokens(text)))
            risk_match = any(risk in str(clue.get("risk_category") or "").lower() for risk in risk_types)
            risk_profile = clue.get("risk_profile") if isinstance(clue.get("risk_profile"), Mapping) else {}
            score = 0.22 + min(overlap, 5) * 0.12 + (0.25 if risk_match else 0.0)
            score += min(float(risk_profile.get("risk_score") or clue.get("risk_score") or 0.0) / 100.0, 1.0) * 0.18
            clue["retrieval_score"] = round(score, 4)
            scored.append((score, clue))
        scored.sort(key=lambda item: (item[0], float(item[1].get("confidence") or 0.0)), reverse=True)
        return [clue for _score, clue in scored[: max(0, int(limit or 0))]]


def _tokens(text: str) -> set[str]:
    normalized = "".join(ch.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" else " " for ch in str(text or ""))
    chunks = {chunk for chunk in normalized.split() if chunk}
    chinese_chars = {ch for ch in normalized if "\u4e00" <= ch <= "\u9fff"}
    return chunks.union(chinese_chars)


__all__ = ["EntityGraphRetrievalService"]
