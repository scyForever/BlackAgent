"""Graph clue generation facade for entity-first risk reasoning."""

from __future__ import annotations

from typing import Any

from storage.entity_graph import EntityGraphStore


class GraphClueGenerator:
    """Generate candidate clues from persisted entity graph facts."""

    def generate(self, graph: EntityGraphStore) -> list[dict[str, Any]]:
        return graph.generate_clues()


__all__ = ["GraphClueGenerator"]
