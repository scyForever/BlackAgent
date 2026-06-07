from src.enhancement.clue_quality import ClueQualityEvaluator


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
