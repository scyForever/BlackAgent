"""In-memory repository for deterministic classification and entity outputs."""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from .schemas import ClassificationResult, EntityExtractionResult


class InMemoryEntityRepo:
    """Store standard-path outputs that are safe to treat as structured data."""

    def __init__(self) -> None:
        self._entities: dict[str, EntityExtractionResult] = {}
        self._classifications: dict[str, ClassificationResult] = {}
        self._entity_source_index: dict[str, set[str]] = {}
        self._classification_source_index: dict[str, set[str]] = {}
        self._lock = RLock()

    def save_classification(self, result: ClassificationResult) -> ClassificationResult:
        record = result.model_copy(deep=True)
        record_id = str(record.classification_id)
        with self._lock:
            self._classifications[record_id] = record
            self._classification_source_index.setdefault(record.source_trace_id, set()).add(record_id)
        return record.model_copy(deep=True)

    def get_classification(self, classification_id: str | UUID) -> ClassificationResult | None:
        with self._lock:
            record = self._classifications.get(str(classification_id))
            return record.model_copy(deep=True) if record else None

    def list_classifications(self, source_trace_id: str | None = None) -> list[ClassificationResult]:
        with self._lock:
            if source_trace_id is None:
                records = list(self._classifications.values())
            else:
                ids = self._classification_source_index.get(source_trace_id, set())
                records = [self._classifications[item_id] for item_id in ids]
            return [record.model_copy(deep=True) for record in records]

    def save_entity(self, entity: EntityExtractionResult) -> EntityExtractionResult:
        record = entity.model_copy(deep=True)
        record_id = str(record.entity_id)
        with self._lock:
            self._entities[record_id] = record
            self._entity_source_index.setdefault(record.source_trace_id, set()).add(record_id)
        return record.model_copy(deep=True)

    def get_entity(self, entity_id: str | UUID) -> EntityExtractionResult | None:
        with self._lock:
            record = self._entities.get(str(entity_id))
            return record.model_copy(deep=True) if record else None

    def list_entities(self, source_trace_id: str | None = None) -> list[EntityExtractionResult]:
        with self._lock:
            if source_trace_id is None:
                records = list(self._entities.values())
            else:
                ids = self._entity_source_index.get(source_trace_id, set())
                records = [self._entities[item_id] for item_id in ids]
            return [record.model_copy(deep=True) for record in records]

    def clear(self) -> None:
        with self._lock:
            self._entities.clear()
            self._classifications.clear()
            self._entity_source_index.clear()
            self._classification_source_index.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entities)


EntityRepo = InMemoryEntityRepo

__all__ = ["EntityRepo", "InMemoryEntityRepo"]
