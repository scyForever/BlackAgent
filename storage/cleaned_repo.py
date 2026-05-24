"""In-memory repository for cleaned text records."""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from .schemas import CleanedText


class InMemoryCleanedTextRepo:
    """Store cleaned text records keyed by clean_id."""

    def __init__(self) -> None:
        self._items: dict[str, CleanedText] = {}
        self._source_index: dict[str, set[str]] = {}
        self._lock = RLock()

    def save(self, item: CleanedText) -> CleanedText:
        record = item.model_copy(deep=True)
        clean_id = str(record.clean_id)
        with self._lock:
            self._items[clean_id] = record
            self._source_index.setdefault(record.source_trace_id, set()).add(clean_id)
        return record.model_copy(deep=True)

    def get(self, clean_id: str | UUID) -> CleanedText | None:
        with self._lock:
            record = self._items.get(str(clean_id))
            return record.model_copy(deep=True) if record else None

    def list_by_source(self, source_trace_id: str) -> list[CleanedText]:
        with self._lock:
            ids = self._source_index.get(source_trace_id, set())
            return [self._items[item_id].model_copy(deep=True) for item_id in ids]

    def list(self, limit: int | None = None) -> list[CleanedText]:
        with self._lock:
            records = list(self._items.values())
            if limit is not None:
                records = records[:limit]
            return [record.model_copy(deep=True) for record in records]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._source_index.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


CleanedTextRepo = InMemoryCleanedTextRepo

__all__ = ["CleanedTextRepo", "InMemoryCleanedTextRepo"]
