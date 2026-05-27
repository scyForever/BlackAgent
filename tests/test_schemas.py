from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from storage import (
    AuditEvent,
    AuditRepo,
    BudgetConsumed,
    ClassificationResult,
    CleanedText,
    CleanedTextRepo,
    EntityExtractionResult,
    EntityRepo,
    ExplorationHypothesis,
    HypothesisType,
    LegalBasis,
    RawIntelligence,
    RawIntelligenceRepo,
    ReviewRepo,
)


def test_exploration_hypothesis_round_trip_and_review_gate() -> None:
    hypothesis = ExplorationHypothesis(
        source_trace_id="raw-hash-1",
        hypothesis_type=HypothesisType.NEW_SLANG_VARIANT,
        hypothesis_summary="音符 may be a candidate variant for a platform slang term.",
        supporting_evidence_ids=["raw-hash-1", "raw-hash-2"],
        suggested_label="slang_variant",
        suggested_normalized_term={"raw": "音符", "target": "抖音"},
        confidence=0.61,
        budget_consumed=BudgetConsumed(rounds=2, tokens=512, elapsed_ms=1200),
    )

    dumped = hypothesis.model_dump_json()
    restored = ExplorationHypothesis.model_validate_json(dumped)

    assert restored.hypothesis_id == hypothesis.hypothesis_id
    assert restored.requires_human_review is True
    assert restored.budget_consumed.rounds == 2

    with pytest.raises(ValidationError):
        ExplorationHypothesis(
            source_trace_id="raw-hash-1",
            hypothesis_type=HypothesisType.NEW_RISK_PATTERN,
            hypothesis_summary="Unsafe candidate.",
            confidence=0.4,
            requires_human_review=False,
        )

    with pytest.raises(ValidationError):
        restored.requires_human_review = False

    with pytest.raises(ValidationError):
        restored.model_copy(update={"requires_human_review": False})


def test_schema_validation_rejects_invalid_confidence_budget_offsets_and_type() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult(
            source_trace_id="trace-1",
            risk_category="tool_trade",
            confidence=1.01,
        )

    with pytest.raises(ValidationError):
        BudgetConsumed(rounds=-1, tokens=0, elapsed_ms=0)

    with pytest.raises(ValidationError):
        EntityExtractionResult(
            source_trace_id="trace-1",
            entity_type="url",
            entity_value="https://example.test",
            start_offset=5,
            end_offset=5,
        )

    with pytest.raises(ValidationError):
        ExplorationHypothesis(
            source_trace_id="trace-1",
            hypothesis_type="UNBOUNDED_FREE_EXPLORATION",
            hypothesis_summary="Invalid type must be blocked.",
            confidence=0.3,
        )


def test_raw_and_cleaned_schema_serialization() -> None:
    raw = RawIntelligence(
        hash_id="abc123",
        trace_id=uuid4(),
        source_type="IM",
        source_name="Authorized_Group_A",
        source_url="local://fixture",
        capture_snapshot_uri="s3://bucket/snapshot.png",
        collector_version="collector_fixture_v1",
        raw_payload_uri="file://payload.json",
        legal_basis=LegalBasis.AUTHORIZED_PARTNER,
        content_text="招募接码测试样本文本",
    )
    raw_payload = raw.model_dump(mode="json")

    restored = RawIntelligence.model_validate(raw_payload)
    assert str(restored.trace_id) == raw_payload["trace_id"]
    assert restored.legal_basis == LegalBasis.AUTHORIZED_PARTNER

    cleaned = CleanedText(
        source_trace_id=str(raw.trace_id),
        clean_text="招募接码测试样本文本",
        noise_score=0.1,
        dedup_group_id="dedup-1",
        quality_score=0.88,
        risk_score=0.74,
        risk_level="HIGH",
        risk_categories=["接码注册"],
        risk_markers=["接码", "contact_handle"],
        text_entropy=3.1024,
    )
    restored_cleaned = CleanedText.model_validate(cleaned.model_dump())
    assert restored_cleaned.clean_text == "招募接码测试样本文本"
    assert restored_cleaned.risk_level == "HIGH"
    assert restored_cleaned.risk_categories == ["接码注册"]

    with pytest.raises(ValidationError):
        CleanedText(source_trace_id=str(raw.trace_id), clean_text="x" * 4001, noise_score=0.1)


def test_in_memory_repositories_store_copies_and_indexes() -> None:
    raw_repo = RawIntelligenceRepo()
    cleaned_repo = CleanedTextRepo()
    entity_repo = EntityRepo()
    review_repo = ReviewRepo()
    audit_repo = AuditRepo()

    raw = RawIntelligence(
        hash_id="raw-hash-1",
        source_type="Social",
        source_name="public_fixture",
        legal_basis=LegalBasis.PUBLIC_COMPLIANT_DATA,
        content_text="低价脚本工具，联系 tg_test",
    )
    saved_raw = raw_repo.save(raw)
    assert raw_repo.get_by_hash("raw-hash-1") == saved_raw
    assert raw_repo.get_by_trace_id(saved_raw.trace_id) == saved_raw

    cleaned = cleaned_repo.save(
        CleanedText(
            source_trace_id=str(saved_raw.trace_id),
            clean_text="低价脚本工具，联系 tg_test",
            noise_score=0.0,
            quality_score=0.77,
            risk_score=0.68,
            risk_level="HIGH",
            risk_categories=["工具交易"],
            risk_markers=["群控", "contact_handle"],
        )
    )
    assert cleaned_repo.list_by_source(str(saved_raw.trace_id)) == [cleaned]

    classification = entity_repo.save_classification(
        ClassificationResult(
            source_trace_id=str(saved_raw.trace_id),
            risk_category="tool_trade",
            confidence=0.92,
            decision_version="rule_v1",
        )
    )
    entity = entity_repo.save_entity(
        EntityExtractionResult(
            source_trace_id=str(saved_raw.trace_id),
            entity_type="contact",
            entity_value="tg_test",
            start_offset=9,
            end_offset=16,
            masking_status="MASKED_SHA256",
        )
    )
    assert entity_repo.list_classifications(str(saved_raw.trace_id)) == [classification]
    assert entity_repo.list_entities(str(saved_raw.trace_id)) == [entity]

    hypothesis = review_repo.add_hypothesis(
        ExplorationHypothesis(
            source_trace_id="raw-hash-1",
            hypothesis_type=HypothesisType.NEW_RISK_PATTERN,
            hypothesis_summary="脚本工具样本需要人工确认是否为新工具交易话术。",
            confidence=0.5,
            budget_consumed=BudgetConsumed(rounds=1, tokens=100, elapsed_ms=50),
        )
    )
    assert review_repo.list_tasks(ReviewRepo.PENDING) == [hypothesis]
    state = review_repo.mark_reviewed(hypothesis.hypothesis_id, decision="APPROVED", reviewer="analyst")
    assert state.status == ReviewRepo.REVIEWED

    event = audit_repo.append(
        AuditEvent(
            event_type="review_state_changed",
            actor="analyst",
            target_id=str(hypothesis.hypothesis_id),
            payload={"decision": "APPROVED"},
        )
    )
    assert audit_repo.get(event.event_id) == event
    assert audit_repo.list("review_state_changed") == [event]

    leaked = raw_repo.get_by_hash("raw-hash-1")
    assert leaked is not None
    leaked.content_text = "mutated outside repo"
    assert raw_repo.get_by_hash("raw-hash-1").content_text == "低价脚本工具，联系 tg_test"
