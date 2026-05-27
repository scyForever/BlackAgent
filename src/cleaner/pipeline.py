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
    calculate_quality_score,
    calculate_noise_score,
    detect_noise_reason,
    detect_risk_signal_profile,
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
        min_entropy: float = 1.0,
        max_noise_score: float = 0.82,
    ) -> None:
        self.max_chars = max_chars
        self.keep_duplicates = keep_duplicates
        self.min_entropy = min_entropy
        self.max_noise_score = max_noise_score
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
            matched_keywords = get_record_field(raw, "matched_keywords")
            matched_themes = get_record_field(raw, "matched_themes")
            extra_terms = [
                *(
                    value
                    for value in (matched_keywords if isinstance(matched_keywords, (list, tuple)) else ())
                ),
                *(
                    value
                    for value in (matched_themes if isinstance(matched_themes, (list, tuple)) else ())
                ),
            ]
            risk_profile = detect_risk_signal_profile(normalized, extra_terms=extra_terms)
            noise_reason = detect_noise_reason(
                normalized,
                noise_score=noise_score,
                risk_score=risk_profile.risk_score,
                min_entropy=self.min_entropy,
                max_noise_score=self.max_noise_score,
            )
            if noise_reason is not None:
                result.dropped.append(
                    DroppedRecord(
                        source_trace_id=source_trace_id,
                        reason=noise_reason,
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
            quality_score = calculate_quality_score(
                truncated,
                noise_score=noise_score,
                risk_score=risk_profile.risk_score,
                entropy=risk_profile.text_entropy,
            )
            result.cleaned.append(
                build_cleaned_text(
                    source_trace_id=source_trace_id,
                    clean_text=truncated,
                    noise_score=noise_score,
                    dedup_group_id=dedup_group_id,
                    quality_score=quality_score,
                    risk_score=risk_profile.risk_score,
                    risk_level=risk_profile.risk_level,
                    risk_categories=list(risk_profile.risk_categories),
                    risk_markers=list(risk_profile.risk_markers),
                    text_entropy=risk_profile.text_entropy,
                )
            )

        result.dedup_groups = dict(groups)
        return result

    def clean_records(self, raw_items: Iterable[Any]) -> CleanerBatchResult:
        return self.clean(raw_items)

    def run(self, raw_items: Iterable[Any]) -> CleanerBatchResult:
        return self.clean(raw_items)


__all__ = ["CleanerBatchResult", "CleanerPipeline"]
