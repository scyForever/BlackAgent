"""Entity risk scoring helpers built on the persistent graph store."""

from __future__ import annotations

from typing import Any

from storage.entity_graph import EntityGraphStore, EntityRiskProfile


class EntityRiskScorer:
    """Read typed entity risk profiles from an EntityGraphStore."""

    def score(self, graph: EntityGraphStore, entity_id: str) -> EntityRiskProfile:
        return graph.risk_profile(entity_id)


class EntityRiskProfileService(EntityRiskScorer):
    """Named service used by preflight graph retrieval and reports."""


__all__ = ["EntityRiskProfileService", "EntityRiskScorer", "EntityRiskProfile"]
