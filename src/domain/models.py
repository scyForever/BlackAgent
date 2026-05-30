"""Canonical domain models used across application, agent, and pipeline code."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from storage.schemas import (
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
    utc_now,
)


class DomainModel(BaseModel):
    """Shared defaults for new domain-only contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RiskClue(DomainModel):
    """Reviewable clue card assembled from classified samples and entities.

    The existing runtime stores clue cards as dictionaries because clue shape is
    still evolving.  This model defines the stable cross-layer contract that new
    code should target without forcing an immediate storage migration.
    """

    clue_id: str = Field(min_length=1)
    clue_type: str = Field(min_length=1)
    risk_category: str = Field(min_length=1)
    risk_score: float = Field(default=0.0, ge=0.0, le=1.0)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    source_names: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    evidence_trace_ids: list[str] = Field(default_factory=list)
    entity_values: list[str] = Field(default_factory=list)
    evidence_summary: str = ""
    review_status: Literal["pending", "approved", "rejected", "archived"] = "pending"
    model_traces: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "AuditEvent",
    "BudgetConsumed",
    "ClassificationResult",
    "CleanedText",
    "DomainModel",
    "EntityExtractionResult",
    "ExplorationHypothesis",
    "HypothesisType",
    "LegalBasis",
    "RawIntelligence",
    "ReviewDecision",
    "RiskClue",
    "utc_now",
]
