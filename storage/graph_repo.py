"""In-memory Neo4j-like entity topology repository for Phase III."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: str
    properties: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    target_id: str
    relation_type: str
    properties: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class InMemoryGraphRepo:
    """Adapter-shaped entity graph for topology queries before Neo4j wiring."""

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []

    def upsert_node(self, node_id: str, node_type: str, properties: Mapping[str, Any] | None = None) -> GraphNode:
        existing = self._nodes.get(node_id)
        merged = dict(existing.properties) if existing else {}
        merged.update(dict(properties or {}))
        node = GraphNode(str(node_id), node_type, merged)
        self._nodes[node.node_id] = node
        return node

    def add_edge(self, source_id: str, target_id: str, relation_type: str, properties: Mapping[str, Any] | None = None) -> GraphEdge:
        edge = GraphEdge(str(source_id), str(target_id), relation_type, dict(properties or {}))
        key = (edge.source_id, edge.target_id, edge.relation_type)
        if not any((item.source_id, item.target_id, item.relation_type) == key for item in self._edges):
            self._edges.append(edge)
        return edge

    def neighbors(self, node_id: str) -> list[GraphNode]:
        ids = {edge.target_id for edge in self._edges if edge.source_id == node_id} | {edge.source_id for edge in self._edges if edge.target_id == node_id}
        return [self._nodes[item_id] for item_id in ids if item_id in self._nodes]

    def nodes(self, node_type: str | None = None) -> list[GraphNode]:
        records = list(self._nodes.values())
        if node_type:
            records = [node for node in records if node.node_type == node_type]
        return records

    def edges(self, relation_type: str | None = None) -> list[GraphEdge]:
        records = list(self._edges)
        if relation_type:
            records = [edge for edge in records if edge.relation_type == relation_type]
        return records

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()


GraphRepo = InMemoryGraphRepo

__all__ = ["GraphEdge", "GraphNode", "GraphRepo", "InMemoryGraphRepo"]
