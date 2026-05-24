"""In-memory repository for raw intelligence records."""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from .schemas import RawIntelligence


class InMemoryRawIntelligenceRepo:
    """Store raw intelligence by both hash_id and trace_id for MVP tests."""

    def __init__(self) -> None:
        self._by_hash: dict[str, RawIntelligence] = {}
        self._trace_to_hash: dict[str, str] = {}
        self._lock = RLock()

    def save(self, item: RawIntelligence) -> RawIntelligence:
        record = item.model_copy(deep=True)
        with self._lock:
            self._by_hash[record.hash_id] = record
            self._trace_to_hash[str(record.trace_id)] = record.hash_id
        return record.model_copy(deep=True)

    def get_by_hash(self, hash_id: str) -> RawIntelligence | None:
        with self._lock:
            record = self._by_hash.get(hash_id)
            return record.model_copy(deep=True) if record else None

    def get_by_trace_id(self, trace_id: str | UUID) -> RawIntelligence | None:
        with self._lock:
            hash_id = self._trace_to_hash.get(str(trace_id))
            record = self._by_hash.get(hash_id) if hash_id else None
            return record.model_copy(deep=True) if record else None

    def list(self, limit: int | None = None) -> list[RawIntelligence]:
        with self._lock:
            records = list(self._by_hash.values())
            if limit is not None:
                records = records[:limit]
            return [record.model_copy(deep=True) for record in records]

    def delete_by_hash(self, hash_id: str) -> bool:
        with self._lock:
            record = self._by_hash.pop(hash_id, None)
            if record is None:
                return False
            self._trace_to_hash.pop(str(record.trace_id), None)
            return True

    def clear(self) -> None:
        with self._lock:
            self._by_hash.clear()
            self._trace_to_hash.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_hash)


RawIntelligenceRepo = InMemoryRawIntelligenceRepo

__all__ = ["InMemoryRawIntelligenceRepo", "RawIntelligenceRepo"]
