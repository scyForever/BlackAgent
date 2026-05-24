"""Cleaner package for BlackAgent deterministic backbone."""

from .pipeline import CleanerBatchResult, CleanerPipeline
from .text_filter import (
    DedupIndex,
    DroppedRecord,
    FallbackCleanedText,
    calculate_noise_score,
    is_blank_or_garbled,
    normalize_text,
    text_similarity,
)

__all__ = [
    "CleanerBatchResult",
    "CleanerPipeline",
    "DedupIndex",
    "DroppedRecord",
    "FallbackCleanedText",
    "calculate_noise_score",
    "is_blank_or_garbled",
    "normalize_text",
    "text_similarity",
]

