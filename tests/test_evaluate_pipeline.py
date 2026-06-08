from argparse import Namespace

from scripts import evaluate_pipeline
from scripts.evaluate_pipeline import evaluate, evaluate_ablation, evaluate_clues, load_jsonl, quality_gate_failures
from src.evaluation.llm_ablation import LLMValueGate


def test_evaluate_pipeline_configures_stdout_utf8(monkeypatch):
    class FakeStdout:
        def __init__(self):
            self.reconfigure_kwargs = None

        def reconfigure(self, **kwargs):
            self.reconfigure_kwargs = kwargs

    fake_stdout = FakeStdout()
    monkeypatch.setattr(evaluate_pipeline.sys, "stdout", fake_stdout)

    evaluate_pipeline.configure_stdout_utf8()

    assert fake_stdout.reconfigure_kwargs == {"encoding": "utf-8"}


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
    assert "entity_graph_tool_trade_cluster" in report["clue"]["actual_clue_types"]
    assert report["clue"]["standard_clue_eval"]["expected_clue_types"] == ["shared_contact_48h", "shared_domain_multi_source"]
    assert report["clue"]["graph_clue_eval"]["expected_clue_count"] == 1
    assert report["clue"]["graph_clue_eval"]["status"] == "completed"
    assert report["clue"]["overall_review_load_eval"]["metric_note"] == "review_load_is_reported_separately_from_standard_vs_graph_quality"
    assert report["clue"]["clue_overgeneration_ratio"] == 1.0
    assert report["classification"]["primary"]["fp"] <= 30
    assert "primary_classification_f1" in report
    assert "secondary_classification_f1" in report
    assert "hierarchical_classification_f1" in report
    assert report["classification"]["overall"]["metric_note"] == "hierarchical_primary_secondary_f1"
    assert report["classification"]["secondary"]["status"] == "completed"
    assert report["classification"]["hierarchical"]["status"] == "completed"
    assert report["classification"]["secondary_gold"]["ready"] is True
    assert report["classification"]["confusion_analysis"]["status"] == "completed"
    assert report["secondary_classification_f1"] >= 0.9
    assert report["hierarchical_classification_f1"] >= 0.9
    assert report["secondary_label_policy"] == "formal_metric"
    assert "rule_version" in report
    assert "llm_calls_per_1000_records" in report
    assert "profile_comparison_dimensions" in report


def test_evaluate_clues_scores_expected_clue_objects_evidence_chains_and_reviewability():
    records = [
        {
            "trace_id": "clue-gold-a",
            "expected_clues": [
                {
                    "clue_type": "shared_contact_48h",
                    "key": "TG:core01",
                    "risk_category": "工具交易",
                    "expected_evidence_trace_ids": ["clue-gold-a", "clue-gold-b"],
                    "expected_entity_values": ["TG:core01"],
                    "min_evidence_count": 2,
                    "min_source_count": 2,
                    "requires_original_snippets": True,
                    "requires_time_range": True,
                }
            ],
        },
        {
            "trace_id": "clue-gold-c",
            "expected_clues": [
                {
                    "clue_type": "shared_domain_multi_source",
                    "key": "risk.example",
                    "risk_category": "诈骗引流",
                    "expected_evidence_trace_ids": ["clue-gold-c"],
                    "expected_entity_values": ["risk.example"],
                    "min_evidence_count": 1,
                    "min_source_count": 1,
                }
            ],
        },
    ]
    actual = [
        {
            "clue_type": "shared_contact_48h",
            "key": "TG:core01",
            "risk_category": "工具交易",
            "evidence_trace_ids": ["clue-gold-a", "clue-gold-b"],
            "entity_values": ["TG:core01"],
            "source_names": ["tg-a", "forum-a"],
            "evidence_reviewability": {
                "source_count": 2,
                "entity_support_count": 1,
                "original_snippets": ["群控脚本 TG:core01", "论坛复现 TG:core01"],
                "time_range": {
                    "start": "2026-06-07T08:00:00+00:00",
                    "end": "2026-06-07T10:00:00+00:00",
                },
            },
        },
        {
            "clue_type": "shared_contact_48h",
            "key": "TG:core01",
            "risk_category": "工具交易",
            "evidence_trace_ids": ["clue-gold-a", "clue-gold-b"],
            "entity_values": ["TG:core01"],
        },
        {
            "clue_type": "shared_domain_multi_source",
            "key": "noise.example",
            "risk_category": "诈骗引流",
            "evidence_trace_ids": ["unrelated"],
            "entity_values": ["noise.example"],
        },
    ]

    metrics = evaluate_clues(records, actual)

    assert metrics["object_clue_eval"]["expected_clue_count"] == 2
    assert metrics["object_clue_eval"]["overall"]["tp"] == 1
    assert metrics["object_clue_eval"]["overall"]["fp"] == 2
    assert metrics["object_clue_eval"]["overall"]["fn"] == 1
    assert metrics["object_clue_eval"]["evidence_chain_precision"] == 1.0
    assert metrics["object_clue_eval"]["evidence_chain_recall"] == 1.0
    assert metrics["object_clue_eval"]["evidence_reviewability_rate"] == 1.0
    assert metrics["duplicate_clue_rate"] > 0


def test_manual_heldout_clue_gold_fixture_exists_with_evidence_chain_requirements():
    records = load_jsonl("tests/evaluation/manual_heldout_clues.jsonl")

    expected = [
        clue
        for record in records
        for clue in record.get("expected_clues", [])
    ]

    assert len(records) >= 8
    assert len(expected) >= 8
    assert all(clue.get("expected_evidence_trace_ids") for clue in expected)
    assert all(clue.get("min_source_count") for clue in expected)


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
    assert conflict.reason == "conflict_only_hard_case_record_enrich"


def test_llm_value_gate_distinguishes_missing_report_from_no_benefit_report():
    from src.agent.model_router import ModelRouter

    missing = ModelRouter(profile="balanced").with_record_enrich_policy(
        enabled=False,
        reason="llm_value_report_missing_hard_cases_only",
        policy="hard_cases_only",
    )
    no_benefit = ModelRouter(profile="balanced").with_llm_value_metrics(
        {
            "classification_f1_delta": 0.0,
            "entity_f1_delta": 0.0,
            "clue_recall_delta": 0.0,
            "should_enable_record_enrich": False,
            "gate_reason": "llm_added_cost_without_measured_quality_gain",
        }
    )

    hard_low_confidence = dict(
        rule_confidence=0.52,
        risk_score=0.82,
        entity_count=1,
        has_contact=True,
        has_url=False,
        has_tool=False,
        has_conflict=False,
        is_duplicate=False,
        quality_score=0.7,
    )

    assert missing.record_enrich_policy == "hard_cases_only"
    assert missing.decide_record(**hard_low_confidence).action == "llm_classify_extract"
    assert no_benefit.record_enrich_policy == "conflict_only"
    assert no_benefit.decide_record(**hard_low_confidence).action == "deterministic_only"


def test_slang_variants_primary_target_is_met_without_llm():
    report = evaluate(
        load_jsonl("tests/evaluation/slang_variants.jsonl"),
        entity_records=load_jsonl("tests/evaluation/slang_variants.jsonl"),
        profile="fast",
    )

    assert report["primary_classification_f1"] >= 0.8
    assert report["entity_f1"] >= 0.8


def test_context_conflict_gold_is_reported_as_hard_negative_not_zero_f1():
    report = evaluate(load_jsonl("tests/evaluation/context_conflict.jsonl"), profile="fast")

    assert report["classification"]["evaluation_mode"] == "hard_negative"
    assert report["classification_f1"] is None
    assert report["hard_negative"]["tn"] + report["hard_negative"]["fp"] >= 50


def test_graph_clue_gold_auto_enables_graph_generation_even_for_fast_profile():
    report = evaluate([], clue_records=load_jsonl("tests/evaluation/cross_source_entity_graph.jsonl"), profile="fast")

    assert report["pipeline_summary"]["graph_clue_generation_enabled"] is True
    assert report["clue"]["expected_clue_count"] == 1
    assert report["clue"]["actual_clue_count"] >= 1
    assert "entity_graph_tool_trade_cluster" in report["clue"]["actual_clue_types"]
    assert report["clue"]["standard_clue_eval"]["expected_clue_count"] == 0
    assert report["clue"]["graph_clue_eval"]["expected_clue_count"] == 1
    assert "entity_graph_tool_trade_cluster" in report["clue"]["graph_clue_eval"]["actual_clue_types"]


def test_hierarchical_classification_requires_and_scores_secondary_gold():
    records = [
        {
            "trace_id": "h1",
            "expected_risk_categories": ["工具交易"],
            "expected_secondary_labels": ["群控脚本"],
        },
        {
            "trace_id": "h2",
            "expected_risk_categories": ["诈骗引流"],
            "expected_secondary_risk": "私域导流",
        },
    ]
    actual = [
        {"source_trace_id": "h1", "risk_category": "工具交易", "secondary_label": "群控脚本"},
        {"source_trace_id": "h2", "risk_category": "诈骗引流", "secondary_label": "返利引流"},
    ]

    from scripts.evaluate_pipeline import evaluate_classification

    metrics = evaluate_classification(records, actual, granularity="hierarchical")

    assert metrics["granularity"] == "hierarchical"
    assert metrics["secondary_gold"]["ready"] is True
    assert metrics["secondary"]["status"] == "completed"
    assert metrics["secondary"]["tp"] == 1
    assert metrics["secondary"]["fp"] == 1
    assert metrics["secondary"]["fn"] == 1


def test_classification_review_load_reports_review_buckets():
    from scripts.evaluate_pipeline import evaluate_classification

    records = [
        {"trace_id": "risk", "expected_risk_categories": ["工具交易"]},
        {"trace_id": "noise"},
        {"trace_id": "weak", "expected_risk_categories": ["账号交易"]},
    ]
    actual = [
        {
            "source_trace_id": "risk",
            "risk_category": "工具交易",
            "secondary_label": "群控脚本",
            "review_required": False,
            "review_bucket": "explicit_risk",
        },
        {
            "source_trace_id": "noise",
            "risk_category": "正常业务白噪声",
            "secondary_label": "低相关",
            "review_required": False,
            "review_bucket": "low_relevance",
        },
        {
            "source_trace_id": "weak",
            "risk_category": "账号交易",
            "secondary_label": "待研判",
            "review_required": True,
            "review_bucket": "human_review_required",
        },
    ]

    metrics = evaluate_classification(records, actual)
    buckets = {item["value"]: item["count"] for item in metrics["review_load"]["by_review_bucket"]}
    final_buckets = {item["value"]: item["count"] for item in metrics["review_load"]["final_review_buckets"]}

    assert buckets["human_review_required"] == 1
    assert final_buckets == {
        "explicit_risk": 1,
        "low_relevance": 1,
        "human_review_required": 1,
    }


def test_classification_review_load_recomputes_stale_review_bucket_for_manual_review_noise():
    from scripts.evaluate_pipeline import evaluate_classification

    records = [{"trace_id": "noise-review"}]
    actual = [
        {
            "source_trace_id": "noise-review",
            "risk_category": "正常业务白噪声",
            "secondary_label": "低相关",
            "review_required": True,
            "review_bucket": "low_relevance",
            "conflict_status": "CONFLICT_REVIEW",
        },
    ]

    metrics = evaluate_classification(records, actual)
    buckets = {item["value"]: item["count"] for item in metrics["review_load"]["by_review_bucket"]}
    final_buckets = {item["value"]: item["count"] for item in metrics["review_load"]["final_review_buckets"]}

    assert buckets == {"human_review_required": 1}
    assert final_buckets == {"human_review_required": 1}


def test_difficult_evaluation_sets_exist_and_can_be_loaded():
    from pathlib import Path

    from scripts.evaluate_pipeline import evaluate_difficult_sets

    paths = [
        Path("tests/evaluation/hard_slang_ambiguous.jsonl"),
        Path("tests/evaluation/context_conflict.jsonl"),
        Path("tests/evaluation/low_evidence_high_risk.jsonl"),
        Path("tests/evaluation/cross_source_entity_graph.jsonl"),
        Path("tests/evaluation/llm_required_cases.jsonl"),
    ]

    assert all(path.exists() and load_jsonl(path) for path in paths)
    report = evaluate_difficult_sets(paths[:1], profile="fast", llm_mode="off")
    assert report["status"] == "completed"
    assert report["subsets"]["hard_slang_ambiguous"]["record_count"] >= 50
