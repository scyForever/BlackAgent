from src.enhancement.clue_quality import ClueQualityEvaluator
from src.enhancement.strategy import RiskClueAggregator
from src.pipeline.stages import ScoreStage


def test_clue_quality_exposes_freshness_score_and_reason():
    assessment = ClueQualityEvaluator().evaluate_one(
        {
            "clue_id": "fresh-clue",
            "confidence": 0.85,
            "evidence_trace_ids": ["r1", "r2", "r3"],
            "source_names": ["tg-a", "forum-b"],
            "entity_values": ["TG:fresh001"],
            "last_seen": "2026-06-07T08:00:00+00:00",
            "quality_reference_time": "2026-06-07T12:00:00+00:00",
        },
        conf_by_trace={"r1": 0.82, "r2": 0.8, "r3": 0.78},
        entities_by_trace={
            "r1": [{"entity_type": "contact", "normalized_value": "fresh001"}],
            "r2": [{"entity_type": "url", "normalized_value": "https://fresh.example/a"}],
        },
        quality_profile="balanced",
        require_cross_source=True,
        require_evidence_chain=True,
    )

    dumped = assessment.model_dump()

    assert assessment.freshness_score == 1.0
    assert dumped["freshness_score"] == 1.0
    assert assessment.false_positive_risk_score < 0.4
    assert "fresh_evidence_window" in assessment.quality_reasons
    assert "false_positive_risk_low" in dumped["false_positive_risk_reasons"]


def test_clue_quality_penalizes_stale_weak_entity_false_positive_risk():
    assessment = ClueQualityEvaluator().evaluate_one(
        {
            "clue_id": "stale-weak-clue",
            "confidence": 0.86,
            "evidence_trace_ids": ["r1"],
            "source_names": ["single-source"],
            "entity_values": [],
            "last_seen": "2026-05-20T12:00:00+00:00",
            "quality_reference_time": "2026-06-07T12:00:00+00:00",
        },
        conf_by_trace={"r1": 0.45},
        entities_by_trace={},
        quality_profile="balanced",
        require_cross_source=True,
        require_evidence_chain=True,
    )

    dumped = assessment.model_dump()

    assert assessment.freshness_score == 0.25
    assert assessment.false_positive_risk_score >= 0.7
    assert assessment.pass_threshold is False
    assert "stale_evidence_window" in dumped["freshness_reasons"]
    assert "single_source_false_positive_risk" in dumped["false_positive_risk_reasons"]
    assert "weak_entity_support_false_positive_risk" in dumped["false_positive_risk_reasons"]


def test_clue_quality_matches_classifications_by_trace_id_fallback():
    assessment = ClueQualityEvaluator().evaluate_many(
        [
            {
                "clue_id": "trace-id-clue",
                "confidence": 0.86,
                "evidence_trace_ids": ["trace-a", "trace-b"],
                "source_names": ["tg-a", "forum-b"],
                "entity_values": ["TG:traceid"],
                "last_seen": "2026-06-07T08:00:00+00:00",
                "quality_reference_time": "2026-06-07T12:00:00+00:00",
            }
        ],
        classifications=[
            {"trace_id": "trace-a", "risk_category": "工具交易", "confidence": 0.8},
            {"trace_id": "trace-b", "risk_category": "工具交易", "confidence": 0.7},
        ],
        entities=[],
        quality_profile="balanced",
        require_cross_source=True,
        require_evidence_chain=True,
    )[0]

    assert assessment.avg_classification_confidence == 0.75
    assert "classification_confidence_stable" in assessment.quality_reasons


def test_score_stage_adds_reviewability_metadata_for_weak_single_source_clue():
    scored = ScoreStage().run_batch(
        [
            {
                "clue_id": "weak-review",
                "clue_type": "shared_contact_48h",
                "key": "unknown-contact",
                "risk_category": "工具交易",
                "confidence": 0.72,
                "evidence_trace_ids": ["weak-r1"],
                "source_names": ["single-source"],
                "entity_values": [],
                "quality_reference_time": "2026-06-07T12:00:00+00:00",
            }
        ],
        context={
            "classifications": [
                {"trace_id": "weak-r1", "risk_category": "工具交易", "confidence": 0.42},
            ],
            "entities": [],
            "quality_profile": "balanced",
            "require_cross_source": True,
            "require_evidence_chain": True,
        },
    )

    reviewability = scored[0]["evidence_reviewability"]

    assert reviewability["source_count"] == 1
    assert reviewability["entity_support_count"] == 0
    assert reviewability["original_snippets"] == []
    assert reviewability["time_range"] == {"start": None, "end": None}
    assert reviewability["false_positive_risk"]["level"] == "high"
    assert reviewability["false_positive_risk"]["score"] >= 0.7
    assert reviewability["suggested_review_action"] == "human_verify_single_source_or_weak_entity_support"
    assert "verify_original_snippets_missing" in reviewability["review_action_reasons"]
    assert "verify_observed_time_missing" in reviewability["review_action_reasons"]


def test_score_stage_adds_reviewability_metadata_for_multi_source_entity_supported_clue():
    scored = ScoreStage().run_batch(
        [
            {
                "clue_id": "strong-review",
                "clue_type": "shared_contact_48h",
                "key": "TG:core01",
                "risk_category": "工具交易",
                "confidence": 0.9,
                "evidence_trace_ids": ["strong-r1", "strong-r2", "strong-r3"],
                "source_names": ["tg-a", "forum-b"],
                "entity_values": ["TG:core01", "risk.example"],
                "original_snippets": [
                    "群控脚本接码 TG:core01",
                    "论坛复现风险域名 risk.example",
                ],
                "first_seen": "2026-06-07T08:00:00+00:00",
                "last_seen": "2026-06-07T10:00:00+00:00",
                "quality_reference_time": "2026-06-07T12:00:00+00:00",
            }
        ],
        context={
            "classifications": [
                {"trace_id": "strong-r1", "risk_category": "工具交易", "confidence": 0.86},
                {"trace_id": "strong-r2", "risk_category": "工具交易", "confidence": 0.82},
                {"trace_id": "strong-r3", "risk_category": "工具交易", "confidence": 0.8},
            ],
            "entities": [
                {"source_trace_id": "strong-r1", "entity_type": "contact", "normalized_value": "core01"},
                {"source_trace_id": "strong-r2", "entity_type": "domain", "normalized_value": "risk.example"},
                {"source_trace_id": "strong-r3", "entity_type": "tool_name", "normalized_value": "群控脚本"},
            ],
            "quality_profile": "balanced",
            "require_cross_source": True,
            "require_evidence_chain": True,
        },
    )

    reviewability = scored[0]["evidence_reviewability"]

    assert reviewability["source_count"] == 2
    assert reviewability["entity_support_count"] >= 2
    assert reviewability["original_snippets"] == [
        "群控脚本接码 TG:core01",
        "论坛复现风险域名 risk.example",
    ]
    assert reviewability["time_range"] == {
        "start": "2026-06-07T08:00:00+00:00",
        "end": "2026-06-07T10:00:00+00:00",
    }
    assert reviewability["false_positive_risk"]["level"] == "low"
    assert reviewability["false_positive_risk"]["score"] < 0.4
    assert reviewability["suggested_review_action"] == "review_original_snippets_and_confirm_entity_linkage"


def test_contact_clue_aggregator_merges_telegram_prefixed_and_bare_handles():
    records = [
        {"trace_id": "contact-alias-1", "source_name": "tg-a", "publish_time": "2026-06-07T01:00:00+00:00"},
        {"trace_id": "contact-alias-2", "source_name": "forum-a", "publish_time": "2026-06-07T02:00:00+00:00"},
        {"trace_id": "contact-alias-3", "source_name": "feed-a", "publish_time": "2026-06-07T03:00:00+00:00"},
    ]
    classifications = [
        {"source_trace_id": "contact-alias-1", "risk_category": "工具交易"},
        {"source_trace_id": "contact-alias-2", "risk_category": "工具交易"},
        {"source_trace_id": "contact-alias-3", "risk_category": "工具交易"},
    ]
    entities = [
        {"source_trace_id": "contact-alias-1", "entity_type": "contact", "normalized_value": "Telegram:core01"},
        {"source_trace_id": "contact-alias-2", "entity_type": "contact", "normalized_value": "TG:core01"},
        {"source_trace_id": "contact-alias-3", "entity_type": "contact", "normalized_value": "core01"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    contact_clue = next(item for item in clues if item.clue_type == "shared_contact_48h")
    assert contact_clue.key == "Telegram:core01"
    assert contact_clue.evidence_trace_ids == ["contact-alias-1", "contact-alias-2", "contact-alias-3"]
