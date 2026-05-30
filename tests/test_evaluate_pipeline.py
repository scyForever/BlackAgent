from scripts.evaluate_pipeline import evaluate, load_jsonl


def test_evaluate_pipeline_reports_classification_entity_and_clue_metrics():
    report = evaluate(
        load_jsonl("tests/evaluation/gold_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/gold_entities.jsonl"),
        clue_records=load_jsonl("tests/evaluation/gold_clues.jsonl"),
        hard_negative_records=load_jsonl("tests/evaluation/hard_negative.jsonl"),
    )

    assert "classification" in report
    assert "entity" in report
    assert "clue" in report
    assert report["hard_negative"]["fp"] == 0
    assert report["hard_negative"]["tn"] >= 2
    assert report["clue"]["actual_clue_count"] >= 1
    assert "shared_contact_48h" in report["clue"]["actual_clue_types"]
    assert report["classification"]["primary"]["fp"] == 0
