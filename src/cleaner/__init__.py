"""Cleaner package for BlackAgent deterministic backbone."""

from .pipeline import CleanerBatchResult, CleanerPipeline
from .text_filter import (
    DedupIndex,
    DroppedRecord,
    FallbackCleanedText,
    RiskSignalProfile,
    calculate_quality_score,
    calculate_noise_score,
    detect_noise_reason,
    detect_risk_signal_profile,
    is_blank_or_garbled,
    normalize_text,
    shannon_entropy,
    text_similarity,
)

__all__ = [
    "CleanerBatchResult",
    "CleanerPipeline",
    "DedupIndex",
    "DroppedRecord",
    "FallbackCleanedText",
    "RiskSignalProfile",
    "calculate_quality_score",
    "calculate_noise_score",
    "detect_noise_reason",
    "detect_risk_signal_profile",
    "is_blank_or_garbled",
    "normalize_text",
    "shannon_entropy",
    "text_similarity",
]
