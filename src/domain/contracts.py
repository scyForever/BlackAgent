"""Typed cross-layer contracts for the investigation data plane.

These contracts intentionally sit above storage-specific schemas.  Pipeline
stages may still expose dictionaries for backward compatibility, but every
stage now normalizes through these models so field names and LLM enrichment
boundaries stay stable across the core chain.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .models import DomainModel, RiskClue


class IntelRecord(DomainModel):
    trace_id: str = Field(min_length=1)
    source_name: str | None = None
    source_type: str | None = None
    legal_basis: str | None = None
    content_text: str = Field(min_length=1)
    publish_time: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CleanedRecord(DomainModel):
    trace_id: str = Field(min_length=1)
    raw_text: str = ""
    clean_text: str = Field(min_length=1)
    normalized_text: str = ""
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    noise_score: float = Field(default=0.0, ge=0.0, le=1.0)
    dedup_group_id: str | None = None
    is_duplicate: bool = False
    duplicate_of: str | None = None


class RiskClassification(DomainModel):
    trace_id: str = Field(min_length=1)
    risk_category: str = Field(min_length=1)
    secondary_label: str = "待研判"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    conflict_status: str | None = None
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = False
    classifier_version: str = "unknown"


class ExtractedEntity(DomainModel):
    entity_id: str
    trace_id: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    raw_value: str | None = None
    normalized_value: str = Field(min_length=1)
    masked_value: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sensitivity_level: str = "normal"
    extraction_method: str = "unknown"


class RoutedRecord(DomainModel):
    trace_id: str = Field(min_length=1)
    route_action: str
    route_reason: str
    max_tokens: int = Field(default=0, ge=0)
    deadline_ms: int = Field(default=0, ge=0)
    requires_review: bool = False


class PipelineItem(DomainModel):
    record: IntelRecord
    cleaned: CleanedRecord | None = None
    classification: RiskClassification | None = None
    entities: list[ExtractedEntity] = Field(default_factory=list)
    route: RoutedRecord | None = None
    llm_enrichment: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RunPolicyContext(DomainModel):
    routing_profile: Literal["fast", "balanced", "high_recall"] = "balanced"
    budget: dict[str, Any] = Field(default_factory=dict)
    quality_profile: str = "balanced"
    enable_llm_intent_parse: bool = True
    enable_query_rewrite: bool = True
    enable_live_collection: bool = True
    enable_llm_record_enrich: bool = True
    enable_llm_clue_refine: bool = True
    min_rule_confidence_for_auto_accept: float = Field(default=0.85, ge=0.0, le=1.0)
    llm_stage_policy: dict[str, bool] = Field(default_factory=dict)

    @classmethod
    def from_profile_config(
        cls,
        *,
        routing_profile: str,
        profile_config: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        quality_profile: str = "balanced",
    ) -> "RunPolicyContext":
        config = dict(profile_config or {})
        stage_policy = dict(config.get("llm_stage_policy") or {})
        enable_record = bool(stage_policy.get("record_enrich", config.get("enable_llm_record_enrich", True)))
        enable_refine = bool(stage_policy.get("clue_refine", config.get("enable_llm_clue_refine", True)))
        return cls(
            routing_profile=_normalize_profile(routing_profile),
            budget=dict(budget or {}),
            quality_profile=quality_profile,
            enable_llm_intent_parse=bool(config.get("enable_llm_intent_parse", True)),
            enable_query_rewrite=bool(config.get("enable_query_rewrite", True)),
            enable_live_collection=bool(config.get("enable_live_collection", True)),
            enable_llm_record_enrich=enable_record,
            enable_llm_clue_refine=enable_refine,
            min_rule_confidence_for_auto_accept=float(config.get("min_rule_confidence_for_auto_accept", 0.85)),
            llm_stage_policy={
                "intent_parse": bool(config.get("enable_llm_intent_parse", True)),
                "investigation_plan": _normalize_profile(routing_profile) != "fast",
                "source_query_rewrite": bool(config.get("enable_query_rewrite", True)),
                "record_enrich": enable_record,
                "clue_refine": enable_refine,
            },
        )


def _normalize_profile(value: str | None) -> Literal["fast", "balanced", "high_recall"]:
    text = str(value or "").strip().lower()
    if text in {"fast", "latency", "low_latency"}:
        return "fast"
    if text in {"high_recall", "recall", "quality"}:
        return "high_recall"
    return "balanced"


__all__ = [
    "CleanedRecord",
    "ExtractedEntity",
    "IntelRecord",
    "PipelineItem",
    "RiskClassification",
    "RiskClue",
    "RoutedRecord",
    "RunPolicyContext",
]
