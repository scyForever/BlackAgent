"""In-memory append-only audit repository for MVP verification."""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from .schemas import AuditEvent


class InMemoryAuditRepo:
    """Append and inspect immutable audit events."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = RLock()

    def append(self, event: AuditEvent) -> AuditEvent:
        record = event.model_copy(deep=True)
        with self._lock:
            self._events.append(record)
        return record.model_copy(deep=True)

    def get(self, event_id: str | UUID) -> AuditEvent | None:
        key = str(event_id)
        with self._lock:
            for event in self._events:
                if str(event.event_id) == key:
                    return event.model_copy(deep=True)
            return None

    def list(self, event_type: str | None = None) -> list[AuditEvent]:
        with self._lock:
            records = [
                event
                for event in self._events
                if event_type is None or event.event_type == event_type
            ]
            return [event.model_copy(deep=True) for event in records]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


AuditRepo = InMemoryAuditRepo

__all__ = ["AuditRepo", "InMemoryAuditRepo"]
