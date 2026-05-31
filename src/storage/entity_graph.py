"""Entity-first observation graph used by the intelligence pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class EntityAsset:
    entity_id: str
    entity_type: str
    canonical_value_hash: str
    masked_display_value: str
    aliases: list[str] = field(default_factory=list)
    sensitivity_level: str = "normal"
    first_seen: str | None = None
    last_seen: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntityObservation:
    observation_id: str
    entity_id: str
    trace_id: str
    source_name: str | None = None
    source_type: str | None = None
    publish_time: str | None = None
    evidence_span: str = ""
    confidence: float = 0.0

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntityRelation:
    relation_id: str
    src_entity_id: str
    dst_entity_id: str
    relation_type: str
    evidence_trace_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class EntityGraphStore:
    """In-memory entity asset, observation, and relation repository."""

    def __init__(self) -> None:
        self._entities: dict[str, EntityAsset] = {}
        self._observations: dict[str, EntityObservation] = {}
        self._relations: dict[str, EntityRelation] = {}

    def upsert_entity(self, entity: Mapping[str, Any], *, seen_at: str | None = None) -> EntityAsset:
        entity_type = str(entity.get("entity_type") or "unknown").strip().lower()
        value = _canonical_value(entity)
        canonical_hash = _hash(f"{entity_type}:{value}")
        entity_id = f"entity:{entity_type}:{canonical_hash[:16]}"
        existing = self._entities.get(entity_id)
        aliases = sorted({*(existing.aliases if existing else []), str(entity.get("entity_value") or entity.get("raw_value") or value)})
        timestamp = seen_at or _utc_now()
        asset = EntityAsset(
            entity_id=entity_id,
            entity_type=entity_type,
            canonical_value_hash=canonical_hash,
            masked_display_value=str(entity.get("masked_value") or entity.get("normalized_value") or entity.get("entity_value") or value),
            aliases=aliases,
            sensitivity_level=str(entity.get("sensitivity_level") or existing.sensitivity_level if existing else entity.get("sensitivity_level") or "normal"),
            first_seen=min([item for item in [existing.first_seen if existing else None, timestamp] if item]) if existing else timestamp,
            last_seen=max([item for item in [existing.last_seen if existing else None, timestamp] if item]) if existing else timestamp,
        )
        self._entities[entity_id] = asset
        return asset

    def add_observation(
        self,
        entity: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        seen_at: str | None = None,
    ) -> EntityObservation:
        asset = self.upsert_entity(entity, seen_at=seen_at or _record_time(record))
        trace_id = str(entity.get("source_trace_id") or entity.get("trace_id") or record.get("trace_id") or record.get("source_trace_id") or "")
        observation_id = f"obs:{asset.entity_id}:{_hash(trace_id)[:12]}"
        observation = EntityObservation(
            observation_id=observation_id,
            entity_id=asset.entity_id,
            trace_id=trace_id,
            source_name=_optional_str(record.get("source_name")),
            source_type=_optional_str(record.get("source_type")),
            publish_time=_record_time(record),
            evidence_span=str(entity.get("entity_value") or entity.get("raw_value") or entity.get("normalized_value") or ""),
            confidence=float(entity.get("confidence") or 0.0),
        )
        self._observations[observation_id] = observation
        return observation

    def add_relation(
        self,
        src_entity_id: str,
        dst_entity_id: str,
        relation_type: str,
        *,
        evidence_trace_ids: Iterable[str] = (),
        confidence: float = 0.0,
    ) -> EntityRelation:
        ordered = sorted([str(src_entity_id), str(dst_entity_id)])
        relation_id = f"rel:{relation_type}:{_hash('|'.join(ordered))[:16]}"
        existing = self._relations.get(relation_id)
        traces = sorted({*(existing.evidence_trace_ids if existing else []), *[str(item) for item in evidence_trace_ids if str(item).strip()]})
        relation = EntityRelation(
            relation_id=relation_id,
            src_entity_id=ordered[0],
            dst_entity_id=ordered[1],
            relation_type=relation_type,
            evidence_trace_ids=traces,
            confidence=max(float(confidence or 0.0), existing.confidence if existing else 0.0),
        )
        self._relations[relation_id] = relation
        return relation

    def observations_for_entity(self, entity_id: str) -> list[EntityObservation]:
        return [item for item in self._observations.values() if item.entity_id == entity_id]

    def cross_source_entities(self) -> list[EntityAsset]:
        output: list[EntityAsset] = []
        for entity in self._entities.values():
            sources = {item.source_name or item.source_type or item.trace_id for item in self.observations_for_entity(entity.entity_id)}
            if len(sources) >= 2:
                output.append(entity)
        return output

    def snapshot(self) -> dict[str, Any]:
        return {
            "entity_count": len(self._entities),
            "observation_count": len(self._observations),
            "relation_count": len(self._relations),
            "cross_source_entity_count": len(self.cross_source_entities()),
            "entities": [item.model_dump() for item in self._entities.values()],
            "observations": [item.model_dump() for item in self._observations.values()],
            "relations": [item.model_dump() for item in self._relations.values()],
        }


def _canonical_value(entity: Mapping[str, Any]) -> str:
    return str(entity.get("normalized_value") or entity.get("entity_value") or entity.get("raw_value") or "").strip().lower()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _record_time(record: Mapping[str, Any]) -> str | None:
    return _optional_str(record.get("publish_time") or record.get("crawl_time"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "EntityAsset",
    "EntityGraphStore",
    "EntityObservation",
    "EntityRelation",
]
