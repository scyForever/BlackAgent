"""Domain contracts for BlackAgent.

This package is the project-facing domain boundary.  The current repository
still persists many contracts through ``storage.schemas``; re-exporting them
here lets application and pipeline code depend on a stable domain namespace
while the storage package is split further in later migrations.
"""

from .models import (
    AuditEvent,
    BudgetConsumed,
    ClassificationResult,
    CleanedText,
    EntityExtractionResult,
    ExplorationHypothesis,
    HypothesisType,
    LegalBasis,
    RawIntelligence,
    ReviewDecision,
    RiskClue,
    utc_now,
)

__all__ = [
    "AuditEvent",
    "BudgetConsumed",
    "ClassificationResult",
    "CleanedText",
    "EntityExtractionResult",
    "ExplorationHypothesis",
    "HypothesisType",
    "LegalBasis",
    "RawIntelligence",
    "ReviewDecision",
    "RiskClue",
    "utc_now",
]
