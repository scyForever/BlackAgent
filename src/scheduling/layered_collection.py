"""Layered scheduling helpers for BlackAgent collection and clue batching."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from src.pipeline import OfflineClueBuildResult, OfflineClueBuilder


LAYER_FAST = "fast"
LAYER_SLOW = "slow"
LAYER_CLUE_BUILD = "clue_build"
LAYER_ORDER = (LAYER_FAST, LAYER_SLOW, LAYER_CLUE_BUILD)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LayeredIntervalConfig:
    fast_interval_seconds: int = 60
    slow_interval_seconds: int = 600
    clue_build_interval_seconds: int = 180

    def interval_for(self, layer: str) -> int:
        normalized = str(layer or "").strip().lower()
        if normalized == LAYER_FAST:
            return _positive_int(self.fast_interval_seconds, default=60)
        if normalized == LAYER_SLOW:
            return _positive_int(self.slow_interval_seconds, default=600)
        if normalized == LAYER_CLUE_BUILD:
            return _positive_int(self.clue_build_interval_seconds, default=180)
        raise KeyError(f"unknown layer: {layer}")


@dataclass
class LayeredRunPlanner:
    config: LayeredIntervalConfig = field(default_factory=LayeredIntervalConfig)
    start_immediately: bool = True
    last_run_at: dict[str, datetime | None] = field(
        default_factory=lambda: {layer: None for layer in LAYER_ORDER}
    )

    def is_due(self, layer: str, *, now: datetime | None = None) -> bool:
        normalized = _normalize_layer(layer)
        current = now or utc_now()
        last = self.last_run_at.get(normalized)
        if last is None:
            return self.start_immediately
        elapsed = (current - last).total_seconds()
        return elapsed >= self.config.interval_for(normalized)

    def due_layers(self, *, now: datetime | None = None) -> list[str]:
        current = now or utc_now()
        return [layer for layer in LAYER_ORDER if self.is_due(layer, now=current)]

    def mark_ran(self, layer: str, *, when: datetime | None = None) -> None:
        normalized = _normalize_layer(layer)
        self.last_run_at[normalized] = when or utc_now()

    def snapshot(self) -> dict[str, str | None]:
        return {
            layer: value.isoformat() if value is not None else None
            for layer, value in self.last_run_at.items()
        }


@dataclass
class PendingClueBatch:
    """Keep newly collected raw rows until clue build runs."""

    rows_by_trace_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_rows(self, rows: Iterable[Mapping[str, Any] | Any]) -> int:
        added = 0
        for row in rows:
            data = _normalize_row(row)
            trace_id = str(data.get("trace_id") or data.get("hash_id") or "").strip()
            if not trace_id:
                continue
            if trace_id not in self.rows_by_trace_id:
                added += 1
            self.rows_by_trace_id[trace_id] = data
        return added

    def count(self) -> int:
        return len(self.rows_by_trace_id)

    def drain(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        keys = list(self.rows_by_trace_id.keys())
        if limit is not None:
            keys = keys[: max(0, int(limit))]
        rows = [self.rows_by_trace_id.pop(key) for key in keys]
        return rows

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows_by_trace_id.values()]


def should_run_clue_build(
    *,
    pending_count: int,
    collection_layer_ran: bool,
    clue_layer_due: bool,
) -> bool:
    if pending_count <= 0:
        return False
    return collection_layer_ran or clue_layer_due


def source_candidates_from_rows(rows: Iterable[Mapping[str, Any] | Any]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        data = _normalize_row(row)
        source_name = str(data.get("source_name") or "").strip()
        source_type = str(data.get("source_type") or "").strip()
        legal_basis = str(data.get("legal_basis") or "").strip()
        source_url = str(data.get("source_url") or "").strip()
        key = (source_name, source_type, legal_basis, source_url)
        if key in seen or not any(key):
            continue
        seen.add(key)
        candidates.append(
            {
                "source_name": source_name,
                "source_type": source_type,
                "legal_basis": legal_basis,
                "source_url": source_url,
            }
        )
    return candidates


def build_candidate_clues_from_raw_rows(
    rows: Iterable[Mapping[str, Any] | Any],
    *,
    quality_profile: str = "high_precision",
    require_cross_source: bool = True,
    require_evidence_chain: bool = True,
) -> OfflineClueBuildResult:
    materialized = [_normalize_row(row) for row in rows]
    return OfflineClueBuilder().build(
        materialized,
        source_candidates=source_candidates_from_rows(materialized),
        quality_profile=quality_profile,
        require_cross_source=require_cross_source,
        require_evidence_chain=require_evidence_chain,
    )


def _normalize_layer(layer: str) -> str:
    normalized = str(layer or "").strip().lower()
    if normalized not in LAYER_ORDER:
        raise KeyError(f"unknown layer: {layer}")
    return normalized


def _normalize_row(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError("unsupported row payload")


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


__all__ = [
    "LAYER_CLUE_BUILD",
    "LAYER_FAST",
    "LAYER_ORDER",
    "LAYER_SLOW",
    "LayeredIntervalConfig",
    "LayeredRunPlanner",
    "PendingClueBatch",
    "build_candidate_clues_from_raw_rows",
    "should_run_clue_build",
    "source_candidates_from_rows",
    "utc_now",
]
