from argparse import Namespace

from scripts.evaluate_pipeline import evaluate, load_jsonl, quality_gate_failures


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
    assert "rule_version" in report
    assert "llm_calls_per_1000_records" in report
    assert "profile_comparison_dimensions" in report


def test_evaluate_pipeline_quality_gate_failures_are_explicit():
    report = {
        "classification_f1": 0.4,
        "entity_f1": 0.5,
        "false_positive_rate": 0.25,
        "llm_calls_per_1000_records": 99.0,
    }
    args = Namespace(
        min_classification_f1=0.8,
        min_entity_f1=0.7,
        max_hard_negative_fpr=0.1,
        max_llm_calls_per_1000=10.0,
    )

    failures = quality_gate_failures(report, args)

    assert len(failures) == 4
    assert failures[0].startswith("classification_f1_below_threshold")
