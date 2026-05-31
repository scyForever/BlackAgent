"""Entity-first observation graph used by the intelligence pipeline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4


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


class EntityAssetRepository:
    def upsert(self, asset: EntityAsset) -> None:
        raise NotImplementedError

    def list(self) -> list[EntityAsset]:
        raise NotImplementedError


class EntityObservationRepository:
    def upsert(self, observation: EntityObservation) -> None:
        raise NotImplementedError

    def list(self) -> list[EntityObservation]:
        raise NotImplementedError


class EntityRelationRepository:
    def upsert(self, relation: EntityRelation) -> None:
        raise NotImplementedError

    def list(self) -> list[EntityRelation]:
        raise NotImplementedError


class EntityGraphStore:
    """Entity asset, observation, and relation repository.

    By default it stays in memory for tests and single-run use.  Supplying a
    ``db_path`` persists entity assets/observations/relations across runs.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._entities: dict[str, EntityAsset] = {}
        self._observations: dict[str, EntityObservation] = {}
        self._relations: dict[str, EntityRelation] = {}
        self.db_path = Path(db_path) if db_path else None
        self._conn: sqlite3.Connection | None = None
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._init_db()
            self._load_from_db()

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
        self._persist_entity(asset)
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
        self._persist_observation(observation)
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
        self._persist_relation(relation)
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

    def generate_clues(self) -> list[dict[str, Any]]:
        """Generate graph-view clues from persisted/cross-source entity facts."""

        clues: list[dict[str, Any]] = []
        for asset in self.cross_source_entities():
            observations = self.observations_for_entity(asset.entity_id)
            traces = sorted({item.trace_id for item in observations if item.trace_id})
            sources = sorted({item.source_name or item.source_type or item.trace_id for item in observations})
            if len(traces) < 2:
                continue
            clue_type = "graph_shared_contact_cross_source" if asset.entity_type in {"contact", "account", "invite_code"} else "graph_shared_entity_cross_source"
            clues.append(
                {
                    "clue_id": f"graph_clue_{uuid4().hex[:12]}",
                    "clue_type": clue_type,
                    "key": asset.entity_id,
                    "risk_category": "unknown",
                    "evidence_trace_ids": traces,
                    "source_names": sources,
                    "entity_values": [asset.masked_display_value],
                    "confidence": round(min(0.96, 0.58 + 0.07 * len(traces) + 0.04 * len(sources)), 4),
                    "threshold_reason": "entity_graph_cross_source_observations",
                    "entity_asset_id": asset.entity_id,
                    "entity_observation_refs": [item.observation_id for item in observations],
                    "entity_graph_backend": "entity_graph_store",
                }
            )
        return clues

    def _init_db(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entity_asset (
              entity_id TEXT PRIMARY KEY,
              entity_type TEXT NOT NULL,
              canonical_value_hash TEXT NOT NULL,
              masked_display_value TEXT NOT NULL,
              aliases_json TEXT NOT NULL,
              sensitivity_level TEXT NOT NULL,
              first_seen TEXT,
              last_seen TEXT
            );
            CREATE TABLE IF NOT EXISTS entity_observation (
              observation_id TEXT PRIMARY KEY,
              entity_id TEXT NOT NULL,
              trace_id TEXT NOT NULL,
              source_name TEXT,
              source_type TEXT,
              publish_time TEXT,
              evidence_span TEXT,
              confidence REAL
            );
            CREATE TABLE IF NOT EXISTS entity_relation (
              relation_id TEXT PRIMARY KEY,
              src_entity_id TEXT NOT NULL,
              dst_entity_id TEXT NOT NULL,
              relation_type TEXT NOT NULL,
              evidence_trace_ids_json TEXT NOT NULL,
              confidence REAL
            );
            """
        )
        self._conn.commit()

    def _load_from_db(self) -> None:
        assert self._conn is not None
        for row in self._conn.execute("SELECT * FROM entity_asset"):
            self._entities[row["entity_id"]] = EntityAsset(
                entity_id=row["entity_id"],
                entity_type=row["entity_type"],
                canonical_value_hash=row["canonical_value_hash"],
                masked_display_value=row["masked_display_value"],
                aliases=json.loads(row["aliases_json"] or "[]"),
                sensitivity_level=row["sensitivity_level"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
            )
        for row in self._conn.execute("SELECT * FROM entity_observation"):
            self._observations[row["observation_id"]] = EntityObservation(
                observation_id=row["observation_id"],
                entity_id=row["entity_id"],
                trace_id=row["trace_id"],
                source_name=row["source_name"],
                source_type=row["source_type"],
                publish_time=row["publish_time"],
                evidence_span=row["evidence_span"] or "",
                confidence=float(row["confidence"] or 0.0),
            )
        for row in self._conn.execute("SELECT * FROM entity_relation"):
            self._relations[row["relation_id"]] = EntityRelation(
                relation_id=row["relation_id"],
                src_entity_id=row["src_entity_id"],
                dst_entity_id=row["dst_entity_id"],
                relation_type=row["relation_type"],
                evidence_trace_ids=json.loads(row["evidence_trace_ids_json"] or "[]"),
                confidence=float(row["confidence"] or 0.0),
            )

    def _persist_entity(self, asset: EntityAsset) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO entity_asset
            (entity_id, entity_type, canonical_value_hash, masked_display_value, aliases_json, sensitivity_level, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset.entity_id,
                asset.entity_type,
                asset.canonical_value_hash,
                asset.masked_display_value,
                json.dumps(asset.aliases, ensure_ascii=False),
                asset.sensitivity_level,
                asset.first_seen,
                asset.last_seen,
            ),
        )
        self._conn.commit()

    def _persist_observation(self, observation: EntityObservation) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO entity_observation
            (observation_id, entity_id, trace_id, source_name, source_type, publish_time, evidence_span, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.observation_id,
                observation.entity_id,
                observation.trace_id,
                observation.source_name,
                observation.source_type,
                observation.publish_time,
                observation.evidence_span,
                observation.confidence,
            ),
        )
        self._conn.commit()

    def _persist_relation(self, relation: EntityRelation) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO entity_relation
            (relation_id, src_entity_id, dst_entity_id, relation_type, evidence_trace_ids_json, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                relation.relation_id,
                relation.src_entity_id,
                relation.dst_entity_id,
                relation.relation_type,
                json.dumps(relation.evidence_trace_ids, ensure_ascii=False),
                relation.confidence,
            ),
        )
        self._conn.commit()


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
    "EntityAssetRepository",
    "EntityGraphStore",
    "EntityObservation",
    "EntityObservationRepository",
    "EntityRelation",
    "EntityRelationRepository",
]
