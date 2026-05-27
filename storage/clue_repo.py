"""In-memory repository for candidate clue pool records."""

from __future__ import annotations

from threading import RLock
from typing import Any, Mapping


class InMemoryClueRepo:
    """Store candidate clues for investigation-time retrieval."""

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def save(self, clue: Mapping[str, Any] | Any) -> dict[str, Any]:
        data = _normalize(clue)
        clue_id = str(data.get("clue_id") or "")
        if not clue_id:
            raise ValueError("candidate clue requires clue_id")
        with self._lock:
            self._by_id[clue_id] = data
        return dict(data)

    def get(self, clue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._by_id.get(str(clue_id))
            return dict(row) if row else None

    def list(self, *, risk_category: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._by_id.values())
        if risk_category is not None:
            expected = str(risk_category).strip().lower()
            rows = [row for row in rows if str(row.get("risk_category") or "").strip().lower() == expected]
        rows.sort(key=lambda item: (-float(item.get("quality_score") or 0.0), -float(item.get("confidence") or 0.0), str(item.get("clue_id") or "")))
        if limit is not None:
            rows = rows[:limit]
        return [dict(row) for row in rows]

    def clear(self) -> None:
        with self._lock:
            self._by_id.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)


ClueRepo = InMemoryClueRepo


def _normalize(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError("unsupported clue payload")


__all__ = ["ClueRepo", "InMemoryClueRepo"]
