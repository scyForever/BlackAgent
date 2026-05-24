"""CleanerPipeline for BlackAgent deterministic backbone."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.collector.base_collector import get_record_field

from .text_filter import (
    MAX_CLEAN_TEXT_CHARS,
    DedupIndex,
    DroppedRecord,
    build_cleaned_text,
    calculate_noise_score,
    is_blank_or_garbled,
    normalize_text,
)


@dataclass
class CleanerBatchResult:
    cleaned: list[Any] = field(default_factory=list)
    dropped: list[DroppedRecord] = field(default_factory=list)
    dedup_groups: dict[str, list[str]] = field(default_factory=dict)


class CleanerPipeline:
    """Normalize, drop garbage, group duplicates, and cap text length."""

    def __init__(
        self,
        *,
        max_chars: int = MAX_CLEAN_TEXT_CHARS,
        near_duplicate_threshold: float = 0.92,
        keep_duplicates: bool = False,
    ) -> None:
        self.max_chars = max_chars
        self.keep_duplicates = keep_duplicates
        self._dedup_index = DedupIndex(threshold=near_duplicate_threshold)

    def clean(self, raw_items: Iterable[Any]) -> CleanerBatchResult:
        groups: defaultdict[str, list[str]] = defaultdict(list)
        result = CleanerBatchResult(dedup_groups={})

        for raw in raw_items:
            source_trace_id = str(
                get_record_field(raw, "source_trace_id")
                or get_record_field(raw, "trace_id")
                or get_record_field(raw, "hash_id")
                or ""
            )
            content_text = str(
                get_record_field(raw, "content_text")
                or get_record_field(raw, "clean_text")
                or get_record_field(raw, "text")
                or ""
            )
            normalized = normalize_text(content_text)
            noise_score = calculate_noise_score(normalized)

            if is_blank_or_garbled(normalized):
                result.dropped.append(
                    DroppedRecord(
                        source_trace_id=source_trace_id,
                        reason="blank_or_garbled",
                        noise_score=noise_score,
                    )
                )
                continue

            dedup_group_id, is_duplicate, similarity = self._dedup_index.assign(normalized)
            groups[dedup_group_id].append(source_trace_id)
            if is_duplicate and not self.keep_duplicates:
                result.dropped.append(
                    DroppedRecord(
                        source_trace_id=source_trace_id,
                        reason="duplicate",
                        noise_score=noise_score,
                        dedup_group_id=dedup_group_id,
                        similarity=similarity,
                    )
                )
                continue

            truncated = normalized[: self.max_chars]
            result.cleaned.append(
                build_cleaned_text(
                    source_trace_id=source_trace_id,
                    clean_text=truncated,
                    noise_score=noise_score,
                    dedup_group_id=dedup_group_id,
                )
            )

        result.dedup_groups = dict(groups)
        return result

    def clean_records(self, raw_items: Iterable[Any]) -> CleanerBatchResult:
        return self.clean(raw_items)

    def run(self, raw_items: Iterable[Any]) -> CleanerBatchResult:
        return self.clean(raw_items)


__all__ = ["CleanerBatchResult", "CleanerPipeline"]

