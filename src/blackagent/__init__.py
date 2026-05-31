"""Product package namespace for BlackAgent.

The legacy repository still exposes modules under ``src.*`` during migration;
new code should import from this namespace where practical.
"""

from src.pipeline import IntelligencePipeline, OfflineClueBuilder, PipelineResult
from src.domain import (
    CleanedRecord,
    ExtractedEntity,
    IntelRecord,
    PipelineItem,
    RiskClassification,
    RiskClue,
    RoutedRecord,
    RunPolicyContext,
)

__all__ = [
    "CleanedRecord",
    "ExtractedEntity",
    "IntelRecord",
    "IntelligencePipeline",
    "OfflineClueBuilder",
    "PipelineItem",
    "PipelineResult",
    "RiskClassification",
    "RiskClue",
    "RoutedRecord",
    "RunPolicyContext",
]
