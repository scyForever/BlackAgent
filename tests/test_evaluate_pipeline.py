from argparse import Namespace

from scripts.evaluate_pipeline import evaluate, evaluate_ablation, load_jsonl, quality_gate_failures
from src.evaluation.llm_ablation import LLMValueGate


def test_evaluate_pipeline_reports_classification_entity_and_clue_metrics():
    report = evaluate(
        load_jsonl("tests/evaluation/gold_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/gold_entities.jsonl"),
        clue_records=load_jsonl("tests/evaluation/gold_clues.jsonl"),
        hard_negative_records=load_jsonl("tests/evaluation/hard_negative.jsonl"),
        profile="fast",
    )

    assert report["profile"] == "fast"
    assert "classification" in report
    assert "entity" in report
    assert "clue" in report
    assert report["hard_negative_record_count"] >= 100
    assert report["hard_negative"]["tn"] >= 70
    assert report["false_positive_rate"] <= 0.3
    assert report["clue"]["actual_clue_count"] >= 1
    assert "shared_contact_48h" in report["clue"]["actual_clue_types"]
    assert report["classification"]["primary"]["fp"] <= 30
    assert "primary_classification_f1" in report
    assert "secondary_classification_f1" in report
    assert "hierarchical_classification_f1" in report
    assert report["classification"]["overall"]["metric_note"] == "hierarchical_primary_secondary_f1"
    assert "rule_version" in report
    assert "llm_calls_per_1000_records" in report
    assert "profile_comparison_dimensions" in report


def test_evaluate_pipeline_quality_gate_failures_are_explicit():
    report = {
        "classification_f1": 0.4,
        "primary_classification_f1": 0.4,
        "secondary_classification_f1": 0.2,
        "hierarchical_classification_f1": 0.3,
        "entity_f1": 0.5,
        "false_positive_rate": 0.25,
        "llm_calls_per_1000_records": 99.0,
        "clue": {
            "clue_overgeneration_ratio": 3.0,
            "review_load_per_100_records": 4.5,
        },
    }
    args = Namespace(
        min_classification_f1=0.8,
        min_primary_classification_f1=0.9,
        min_secondary_classification_f1=0.65,
        min_hierarchical_classification_f1=0.75,
        min_entity_f1=0.7,
        max_hard_negative_fpr=0.1,
        max_llm_calls_per_1000=10.0,
        max_clue_overgeneration_ratio=2.0,
        max_review_load_per_100_records=3.0,
    )

    failures = quality_gate_failures(report, args)

    assert len(failures) == 9
    assert failures[0].startswith("classification_f1_below_threshold")
    assert any(item.startswith("primary_classification_f1_below_threshold") for item in failures)
    assert any(item.startswith("secondary_classification_f1_below_threshold") for item in failures)
    assert any(item.startswith("hierarchical_classification_f1_below_threshold") for item in failures)
    assert any(item.startswith("clue_overgeneration_ratio_above_threshold") for item in failures)
    assert any(item.startswith("review_load_per_100_records_above_threshold") for item in failures)


def test_llm_ablation_reports_value_gate_when_mock_adds_no_quality_gain():
    report = evaluate_ablation(
        load_jsonl("tests/evaluation/gold_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/gold_entities.jsonl"),
        clue_records=load_jsonl("tests/evaluation/gold_clues.jsonl"),
        hard_negative_records=load_jsonl("tests/evaluation/hard_negative.jsonl"),
        with_budget=True,
    )

    assert report["mode"] == "llm_ablation"
    assert {"fast_off", "high_recall_off", "high_recall_mock"} <= set(report["scenarios"])
    assert "llm_calls_delta" in report["llm_value"]
    assert LLMValueGate().should_enable_record_enrich("high_recall", report["llm_value"]) is report["llm_value_gate"]["should_enable_record_enrich"]


def test_llm_value_gate_tightens_model_router_record_enrich_without_blocking_conflicts():
    from src.agent.model_router import ModelRouter

    router = ModelRouter(profile="high_recall").with_llm_value_metrics(
        {
            "classification_f1_delta": 0.0,
            "entity_f1_delta": 0.0,
            "clue_recall_delta": 0.0,
            "tokens_per_extra_valid_clue": None,
            "gate_reason": "llm_added_cost_without_measured_quality_gain",
        }
    )

    normal = router.decide_record(
        rule_confidence=0.5,
        risk_score=0.8,
        entity_count=1,
        has_contact=True,
        has_url=False,
        has_tool=False,
        has_conflict=False,
        is_duplicate=False,
        quality_score=0.7,
    )
    conflict = router.decide_record(
        rule_confidence=0.5,
        risk_score=0.8,
        entity_count=1,
        has_contact=True,
        has_url=False,
        has_tool=False,
        has_conflict=True,
        is_duplicate=False,
        quality_score=0.7,
    )

    assert normal.action == "deterministic_only"
    assert normal.reason == "llm_added_cost_without_measured_quality_gain"
    assert conflict.action == "llm_classify_extract"
    assert conflict.reason == "conflict_hard_case_despite_value_gate"
