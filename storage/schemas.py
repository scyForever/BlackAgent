"""Pydantic v2 data contracts for the BlackAgent MVP storage boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class SchemaModel(BaseModel):
    """Shared strict-ish defaults for persisted contract objects."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )


class LegalBasis(str, Enum):
    """Allowed compliance basis values for collected intelligence."""

    AUTHORIZED_PARTNER = "AUTHORIZED_PARTNER"
    PUBLIC_COMPLIANT_DATA = "PUBLIC_COMPLIANT_DATA"
    INTERNAL_AUTHORIZED_SOURCE = "INTERNAL_AUTHORIZED_SOURCE"
    THIRD_PARTY_AUTHORIZED_FEED = "THIRD_PARTY_AUTHORIZED_FEED"


class HypothesisType(str, Enum):
    """Controlled exploration output categories."""

    NEW_SLANG_VARIANT = "NEW_SLANG_VARIANT"
    NEW_RISK_PATTERN = "NEW_RISK_PATTERN"
    SUSPECTED_CLUSTER = "SUSPECTED_CLUSTER"


class ReviewDecision(str, Enum):
    """Human workbench decisions allowed for sandbox hypotheses."""

    APPROVED = "APPROVED"
    MISREPORT = "MISREPORT"
    UNCERTAIN = "UNCERTAIN"
    ESCALATE = "ESCALATE"


class BudgetConsumed(SchemaModel):
    """Sandbox exploration cost ledger."""

    rounds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


class RawIntelligence(SchemaModel):
    """Raw intelligence collected from an authorized source."""

    hash_id: str = Field(min_length=1, description="Unique hash of raw content")
    trace_id: UUID = Field(default_factory=uuid4, description="Global trace UUID")
    source_type: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_url: str | None = None
    capture_snapshot_uri: str | None = None
    collector_version: str = Field(default="collector_v1", min_length=1)
    raw_payload_uri: str | None = None
    legal_basis: LegalBasis
    crawl_time: datetime = Field(default_factory=utc_now)
    publish_time: datetime | None = None
    content_text: str = Field(min_length=1)


class CleanedText(SchemaModel):
    """Deterministic cleaned text derived from a raw intelligence item."""

    clean_id: UUID = Field(default_factory=uuid4)
    source_trace_id: str = Field(min_length=1)
    clean_text: str = Field(min_length=1, max_length=4000)
    noise_score: float = Field(ge=0.0, le=1.0)
    dedup_group_id: str | None = None
    cleaning_version: str = Field(default="cleaner_v1", min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class ClassificationResult(SchemaModel):
    """Risk classification output for one cleaned/raw sample."""

    classification_id: UUID = Field(default_factory=uuid4)
    source_trace_id: str = Field(min_length=1)
    risk_category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    review_required: bool = False
    decision_version: str = Field(default="classifier_v1", min_length=1)
    evidence: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class EntityExtractionResult(SchemaModel):
    """Single extracted entity from a classified intelligence sample."""

    entity_id: UUID = Field(default_factory=uuid4)
    source_trace_id: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    entity_value: str = Field(min_length=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    masking_status: str = Field(default="UNMASKED", min_length=1)
    extractor_version: str = Field(default="extractor_v1", min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_offsets(self) -> "EntityExtractionResult":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class ExplorationHypothesis(SchemaModel):
    """Sandbox-only candidate hypothesis that always requires human review."""

    hypothesis_id: UUID = Field(default_factory=uuid4)
    source_trace_id: str = Field(min_length=1)
    hypothesis_type: HypothesisType
    hypothesis_summary: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    suggested_label: str | None = None
    suggested_normalized_term: dict[str, str] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: Literal[True] = True
    budget_consumed: BudgetConsumed = Field(default_factory=BudgetConsumed)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def enforce_review_gate(self) -> "ExplorationHypothesis":
        if self.requires_human_review is not True:
            raise ValueError("ExplorationHypothesis must require human review")
        return self

    def model_copy(
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> "ExplorationHypothesis":
        """Return a validated copy so updates cannot bypass the review gate."""

        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(copied.model_dump(mode="python"))


class AuditEvent(SchemaModel):
    """Append-only audit record for safety-relevant storage actions."""

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(min_length=1)
    actor: str = Field(default="system", min_length=1)
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


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
]
