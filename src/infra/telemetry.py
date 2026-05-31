"""Lightweight telemetry records for runtime observability."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TelemetryEvent:
    stage: str
    ok: bool
    elapsed_ms: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class TelemetryRecorder:
    """In-memory telemetry sink used by tests and local runtime surfaces."""

    def __init__(self) -> None:
        self._events: list[TelemetryEvent] = []

    def record(self, stage: str, *, ok: bool = True, elapsed_ms: float = 0.0, **payload: Any) -> None:
        self._events.append(TelemetryEvent(stage=stage, ok=ok, elapsed_ms=elapsed_ms, payload=payload))

    def list(self) -> list[dict[str, Any]]:
        return [event.model_dump() for event in self._events]


__all__ = ["TelemetryEvent", "TelemetryRecorder"]
