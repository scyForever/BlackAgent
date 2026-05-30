"""Base stage primitives for the composable intelligence pipeline."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


class PassThroughStage:
    """Default stage used while legacy processors are wrapped incrementally."""

    def run_batch(self, items: Iterable[Mapping[str, Any]], **_: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in items]


__all__ = ["PassThroughStage"]
