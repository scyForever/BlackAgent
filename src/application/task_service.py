"""Task application service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.backend import TaskBackend


class TaskService:
    """Small façade around the local task backend."""

    def __init__(self, backend: TaskBackend) -> None:
        self.backend = backend

    def submit(self, name: str, payload: Mapping[str, Any], *, handler: Callable[[dict[str, Any]], Any] | None = None) -> Any:
        return self.backend.submit(name, dict(payload), handler=handler)

    def run_pending(self, limit: int | None = None) -> list[Any]:
        return self.backend.run_pending(limit=limit)


__all__ = ["TaskService"]
