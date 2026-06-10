from argparse import Namespace

from scripts import evaluate_pipeline
from scripts.evaluate_pipeline import evaluate, evaluate_ablation, evaluate_clues, evaluate_profile_curve, load_jsonl, quality_gate_failures
from src.evaluation.llm_ablation import LLMValueGate, llm_value_report_from_ablation


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
    assert report["hard_negative_record_count"] >= 112
    assert report["hard_negative"]["tn"] >= 70
    assert report["false_positive_rate"] <= 0.1
    assert report["clue"]["actual_clue_count"] >= 1
    assert "shared_contact_48h" in report["clue"]["actual_clue_types"]
    assert "entity_graph_tool_trade_cluster" in report["clue"]["actual_clue_types"]
    assert report["clue"]["standard_clue_eval"]["expected_clue_types"] == ["shared_contact_48h", "shared_domain_multi_source"]
    assert report["clue"]["graph_clue_eval"]["expected_clue_count"] == 1
    assert report["clue"]["graph_clue_eval"]["status"] == "completed"
    assert report["clue"]["overall_review_load_eval"]["metric_note"] == "review_load_is_reported_separately_from_standard_vs_graph_quality"
    assert report["clue"]["clue_overgeneration_ratio"] >= 1.0
    assert report["clue"]["overall_review_load_eval"]["clue_overgeneration_ratio"] == report["clue"]["clue_overgeneration_ratio"]
    assert report["clue"]["duplicate_clue_rate"] == 0.0
    assert report["clue"]["graph_clue_eval"]["clue_overgeneration_ratio"] == 1.0
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


def test_evaluate_clues_uses_expected_clue_objects_for_top_level_type_gold():
    records = [
        {
            "source_trace_id": "trace-a",
            "expected_clues": [
                {
                    "clue_type": "shared_tool_multi_source",
                    "key": "tool-a",
                    "expected_evidence_trace_ids": ["trace-a", "trace-b"],
                }
            ],
        }
    ]
    actual = [
        {
            "clue_type": "shared_template_multi_source",
            "key": "tool-a",
            "evidence_trace_ids": ["trace-a", "trace-b"],
        }
    ]

    metrics = evaluate_clues(records, actual)

    assert metrics["expected_clue_types"] == ["shared_tool_multi_source"]
    assert metrics["actual_clue_types"] == ["shared_template_multi_source"]
    assert metrics["overall"]["f1"] < 1.0


def test_evaluate_clues_prefers_matching_evidence_when_type_and_key_tie():
    records = [
        {
            "trace_id": "bridge-gold-a",
            "expected_clues": [
                {
                    "clue_type": "shared_domain_multi_source",
                    "key": "risk.example",
                    "risk_category": "诈骗引流",
                    "expected_evidence_trace_ids": ["bridge-gold-a", "bridge-gold-b"],
                    "expected_entity_values": ["risk.example"],
                    "min_evidence_count": 2,
                    "min_source_count": 2,
                }
            ],
        }
    ]
    actual = [
        {
            "clue_type": "shared_domain_multi_source",
            "key": "risk.example",
            "risk_category": "诈骗引流",
            "evidence_trace_ids": ["unrelated-a", "unrelated-b"],
            "entity_values": ["risk.example"],
        },
        {
            "clue_type": "shared_domain_multi_source",
            "key": "risk.example",
            "risk_category": "诈骗引流",
            "evidence_trace_ids": ["bridge-gold-a", "bridge-gold-b"],
            "entity_values": ["risk.example"],
            "source_names": ["source-a", "source-b"],
        },
    ]

    metrics = evaluate_clues(records, actual)

    assert metrics["object_clue_eval"]["matched_clue_count"] == 1
    assert metrics["object_clue_eval"]["evidence_chain_recall"] == 1.0


def test_evaluate_clues_does_not_match_keyed_graph_clue_by_type_only():
    records = [
        {
            "trace_id": "graph-gold-a",
            "expected_clues": [
                {
                    "clue_type": "entity_graph_tool_trade_cluster",
                    "key": "loginbot-mh22",
                    "risk_category": "工具交易",
                    "expected_evidence_trace_ids": ["graph-gold-a", "graph-gold-b"],
                    "expected_entity_values": ["loginbot-mh22", "批量登录"],
                }
            ],
        }
    ]
    actual = [
        {
            "clue_type": "entity_graph_tool_trade_cluster",
            "key": "Telegram:mhfinal24",
            "risk_category": "工具交易",
            "evidence_trace_ids": ["graph-gold-b", "graph-gold-c"],
            "entity_values": ["Telegram:mhfinal24", "群控"],
        }
    ]

    metrics = evaluate_clues(records, actual)

    assert metrics["object_clue_eval"]["matched_clue_count"] == 0


def test_manual_heldout_clue_gold_fixture_exists_with_evidence_chain_requirements():
    records = load_jsonl("tests/evaluation/manual_heldout_clues.jsonl")

    expected = [
        clue
        for record in records
        for clue in record.get("expected_clues", [])
    ]
    required_fields = {
        "expected_evidence_trace_ids",
        "expected_entity_values",
        "min_evidence_count",
        "min_source_count",
        "requires_original_snippets",
        "requires_time_range",
    }

    assert 20 <= len(records) <= 50
    assert 20 <= len(expected) <= 50
    for clue in expected:
        assert required_fields <= set(clue)
        assert clue["expected_evidence_trace_ids"]
        assert clue["expected_entity_values"]
        assert clue["min_evidence_count"] >= 2
        assert clue["min_source_count"] >= 2
        assert clue["requires_original_snippets"] is True
        assert clue["requires_time_range"] is True

    metrics = evaluate_clues(records, [])
    object_eval = metrics["object_clue_eval"]
    assert object_eval["expected_clue_count"] == len(expected)
    assert "precision" in object_eval["overall"]
    assert "recall" in object_eval["overall"]
    assert "duplicate_clue_rate" in object_eval
    assert "evidence_chain_precision" in object_eval
    assert "evidence_chain_recall" in object_eval
    assert "evidence_reviewability_rate" in object_eval


def test_manual_heldout_pipeline_generates_reviewable_object_clues():
    report = evaluate(
        load_jsonl("tests/evaluation/manual_heldout_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/manual_heldout_classification.jsonl"),
        clue_records=load_jsonl("tests/evaluation/manual_heldout_clues.jsonl"),
        profile="high_recall",
    )

    object_eval = report["clue"]["object_clue_eval"]
    assert object_eval["overall"]["recall"] >= 0.95
    assert object_eval["overall"]["f1"] >= 0.95
    assert object_eval["matched_clue_count"] >= 23
    assert object_eval["evidence_chain_precision"] >= 0.95
    assert object_eval["evidence_chain_recall"] >= 0.95
    assert object_eval["evidence_reviewability_rate"] == 1.0


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
            "overall": {"recall": 0.45},
            "object_clue_eval": {"overall": {"recall": 0.5}},
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
        min_clue_recall=0.8,
        min_object_clue_recall=0.95,
        max_clue_overgeneration_ratio=2.0,
        max_review_load_per_100_records=3.0,
        max_classification_review_rate=None,
    )

    failures = quality_gate_failures(report, args)

    assert len(failures) == 11
    assert failures[0].startswith("classification_f1_below_threshold")
    assert any(item.startswith("primary_classification_f1_below_threshold") for item in failures)
    assert any(item.startswith("secondary_classification_f1_below_threshold") for item in failures)
    assert any(item.startswith("hierarchical_classification_f1_below_threshold") for item in failures)
    assert any(item.startswith("clue_recall_below_threshold") for item in failures)
    assert any(item.startswith("object_clue_recall_below_threshold") for item in failures)
    assert any(item.startswith("clue_overgeneration_ratio_above_threshold") for item in failures)
    assert any(item.startswith("review_load_per_100_records_above_threshold") for item in failures)


def test_secondary_metrics_ignore_normal_noise_labels_on_hard_negatives():
    records = [
        {
            "trace_id": "positive",
            "expected_primary_risk": "工具交易",
            "expected_secondary_labels": ["群控脚本"],
        },
        {
            "trace_id": "hard-negative",
            "expected_risk_categories": [],
        },
    ]
    actual = [
        {
            "source_trace_id": "positive",
            "risk_category": "工具交易",
            "secondary_label": "群控脚本",
        },
        {
            "source_trace_id": "hard-negative",
            "risk_category": "正常业务白噪声",
            "secondary_label": "低相关",
        },
    ]

    metrics = evaluate_pipeline.evaluate_classification(records, actual)

    assert metrics["secondary"]["fp"] == 0
    assert metrics["secondary"]["f1"] == 1.0


def test_secondary_metrics_ignore_expected_normal_noise_labels_on_negative_gold():
    records = [
        {
            "trace_id": "positive",
            "expected_primary_risk": "工具交易",
            "expected_secondary_labels": ["群控脚本"],
        },
        {
            "trace_id": "negative-low-relevance",
            "expected_risk_categories": [],
            "expected_secondary_labels": ["低相关"],
        },
        {
            "trace_id": "negative-defense",
            "expected_risk_categories": [],
            "expected_secondary_labels": ["防御语境"],
        },
    ]
    actual = [
        {
            "source_trace_id": "positive",
            "risk_category": "工具交易",
            "secondary_label": "群控脚本",
        },
        {
            "source_trace_id": "negative-low-relevance",
            "risk_category": "正常业务白噪声",
            "secondary_label": "低相关",
        },
        {
            "source_trace_id": "negative-defense",
            "risk_category": "正常业务白噪声",
            "secondary_label": "防御语境",
        },
    ]

    metrics = evaluate_pipeline.evaluate_classification(records, actual)

    assert metrics["secondary_gold"]["expected_label_count"] == 1
    assert metrics["secondary"]["fn"] == 0
    assert metrics["secondary"]["f1"] == 1.0


def test_llm_ablation_reports_value_gate_when_mock_adds_no_quality_gain(monkeypatch):
    monkeypatch.delenv("BLACKAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BLACKAGENT_LLM_DRY_RUN", "true")

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


def test_llm_ablation_value_matrix_labels_high_recall_fallback_and_latency(monkeypatch):
    monkeypatch.delenv("BLACKAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BLACKAGENT_LLM_DRY_RUN", "true")

    report = evaluate_ablation(
        load_jsonl("tests/evaluation/gold_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/gold_entities.jsonl"),
        clue_records=load_jsonl("tests/evaluation/gold_clues.jsonl"),
        hard_negative_records=load_jsonl("tests/evaluation/hard_negative.jsonl"),
        with_budget=True,
    )

    assert {"fast_off", "balanced_mock", "high_recall_real_or_configured_fallback"} <= set(report["scenarios"])
    assert report["scenario_consistency"]["same_dataset_fingerprint"] is True

    matrix = {row["scenario"]: row for row in report["llm_value_matrix"]}
    assert {"fast_off", "balanced_mock", "high_recall_real_or_configured_fallback"} <= set(matrix)
    assert {row["dataset_fingerprint"] for row in matrix.values()} == {report["dataset_fingerprint"]}
    assert matrix["fast_off"]["profile"] == "fast"
    assert matrix["fast_off"]["effective_llm_mode"] == "off"
    assert matrix["balanced_mock"]["profile"] == "balanced"
    assert matrix["balanced_mock"]["effective_llm_mode"] == "mock"
    assert matrix["high_recall_real_or_configured_fallback"]["profile"] == "high_recall"
    assert matrix["high_recall_real_or_configured_fallback"]["requested_llm_mode"] == "real_or_configured_fallback"
    assert matrix["high_recall_real_or_configured_fallback"]["provider_status"] == "fallback"
    assert matrix["high_recall_real_or_configured_fallback"]["fallback_reason"]
    assert "latency_ms_per_f1_gain" in matrix["balanced_mock"]["delta_vs_fast_off"]
    assert "latency_ms_per_extra_valid_clue" in matrix["balanced_mock"]["delta_vs_fast_off"]
    assert "latency_ms_per_f1_gain" in report["llm_value"]
    assert "latency_ms_per_extra_valid_clue" in report["llm_value"]

    runtime_report = llm_value_report_from_ablation(report)

    assert "latency_ms_per_f1_gain" in runtime_report
    assert "latency_ms_per_extra_valid_clue" in runtime_report


def test_llm_ablation_value_matrix_includes_balanced_real_fallback(monkeypatch):
    monkeypatch.delenv("BLACKAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BLACKAGENT_LLM_DRY_RUN", "true")

    report = evaluate_ablation(
        load_jsonl("tests/evaluation/gold_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/gold_entities.jsonl"),
        clue_records=load_jsonl("tests/evaluation/gold_clues.jsonl"),
        hard_negative_records=load_jsonl("tests/evaluation/hard_negative.jsonl"),
        with_budget=True,
        include_real=True,
    )

    matrix = {row["scenario"]: row for row in report["llm_value_matrix"]}

    assert "balanced_real_or_configured_fallback" in report["scenarios"]
    assert "balanced_real_or_configured_fallback" in matrix
    assert matrix["balanced_real_or_configured_fallback"]["profile"] == "balanced"
    assert matrix["balanced_real_or_configured_fallback"]["requested_llm_mode"] == "real_or_configured_fallback"
    assert matrix["balanced_real_or_configured_fallback"]["provider_status"] == "fallback"
    assert report["llm_value"]["balanced_real_or_fallback"]["provider_status"] == "fallback"

    runtime_report = llm_value_report_from_ablation(report)

    assert "balanced_real_or_fallback" in runtime_report["provider_specific"]


def test_llm_value_delta_prefers_actual_usage_tokens_when_available():
    base = {
        "primary_classification_f1": 0.8,
        "entity_f1": 0.9,
        "clue_f1": 0.7,
        "clue_recall": 0.7,
        "llm_calls_per_1000_records": 0.0,
        "profile_comparison_dimensions": {
            "estimated_tokens": 1000,
            "actual_usage_tokens": 200,
            "p95_latency_ms": 1000,
        },
        "clue": {"overall": {"tp": 3}},
    }
    llm = {
        "primary_classification_f1": 0.8,
        "entity_f1": 0.9,
        "clue_f1": 0.9,
        "clue_recall": 0.9,
        "llm_calls_per_1000_records": 10.0,
        "profile_comparison_dimensions": {
            "estimated_tokens": 3000,
            "actual_usage_tokens": 700,
            "p95_latency_ms": 1300,
        },
        "clue": {"overall": {"tp": 3}},
    }

    delta = evaluate_pipeline._llm_value_delta(base, llm)

    assert delta["tokens_per_f1_gain"] == 2500.0


def test_llm_ablation_uses_hard_llm_required_and_context_conflict_samples(monkeypatch):
    monkeypatch.delenv("BLACKAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BLACKAGENT_LLM_DRY_RUN", "true")

    hard_positive_records = load_jsonl("tests/evaluation/llm_required_cases.jsonl")
    conflict_negative_records = load_jsonl("tests/evaluation/context_conflict.jsonl")

    report = evaluate_ablation(
        hard_positive_records,
        hard_negative_records=conflict_negative_records,
        with_budget=True,
    )

    assert len(hard_positive_records) == 50
    assert len(conflict_negative_records) == 50
    assert report["mode"] == "llm_ablation"
    assert report["scenario_consistency"]["same_dataset_fingerprint"] is True
    assert report["scenario_consistency"]["input_counts"]["classification_records"] == 50
    assert report["scenario_consistency"]["input_counts"]["hard_negative_records"] == 50
    assert "llm_value_gate" in report
    assert "should_enable_record_enrich" in report["llm_value_gate"]
    assert "reason" in report["llm_value_gate"]

    scenario = report["scenarios"]["high_recall_real_or_configured_fallback"]["ablation_scenario"]
    assert scenario["requested_llm_mode"] == "real_or_configured_fallback"
    assert scenario["provider_status"] == "fallback"
    assert scenario["fallback_reason"]
    assert scenario["input_counts"]["classification_records"] == 50
    assert scenario["input_counts"]["hard_negative_records"] == 50

    matrix = {row["scenario"]: row for row in report["llm_value_matrix"]}
    real_or_fallback = matrix["high_recall_real_or_configured_fallback"]
    assert real_or_fallback["requested_llm_mode"] == "real_or_configured_fallback"
    assert real_or_fallback["provider_status"] == "fallback"
    assert real_or_fallback["fallback_reason"]
    assert report["llm_value"]["real_or_fallback"]["provider_status"] == "fallback"
    assert report["llm_value"]["real_or_fallback"]["fallback_reason"]


def test_llm_ablation_latest_value_uses_runtime_fallback_status(monkeypatch):
    monkeypatch.setattr(
        evaluate_pipeline,
        "_real_or_fallback_ablation_choice",
        lambda *, include_real: {
            "effective_llm_mode": "mock",
            "provider_status": "real",
            "fallback_reason": None,
            "real_requested": include_real,
            "real_gateway_configured": True,
        },
    )
    monkeypatch.setattr(
        evaluate_pipeline,
        "_real_scenario_runtime_status",
        lambda report: {
            "provider_status": "fallback",
            "fallback_reason": "real_gateway_configured_but_used_local_fallback",
        },
    )

    report = evaluate_ablation(
        load_jsonl("tests/evaluation/gold_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/gold_entities.jsonl"),
        clue_records=load_jsonl("tests/evaluation/gold_clues.jsonl"),
        hard_negative_records=load_jsonl("tests/evaluation/hard_negative.jsonl"),
        with_budget=True,
    )
    runtime_report = llm_value_report_from_ablation(report)

    real_or_fallback = report["llm_value"]["real_or_fallback"]
    assert report["scenarios"]["high_recall_real_or_configured_fallback"]["ablation_scenario"]["provider_status"] == "fallback"
    assert real_or_fallback["provider_status"] == "fallback"
    assert real_or_fallback["fallback_reason"] == "real_gateway_configured_but_used_local_fallback"
    assert runtime_report["provider_specific"]["real_or_fallback"]["provider_status"] == "fallback"
    assert runtime_report["provider_specific"]["real_or_fallback"]["fallback_reason"] == "real_gateway_configured_but_used_local_fallback"


def test_profile_curve_reports_quality_cost_and_latency_for_all_profiles():
    report = evaluate_profile_curve(
        load_jsonl("tests/evaluation/gold_classification.jsonl"),
        entity_records=load_jsonl("tests/evaluation/gold_entities.jsonl"),
        clue_records=load_jsonl("tests/evaluation/gold_clues.jsonl"),
        hard_negative_records=load_jsonl("tests/evaluation/hard_negative.jsonl"),
        llm_mode="off",
        with_budget=True,
    )

    rows = report["profile_quality_cost_latency_curve"]
    by_profile = {row["profile"]: row for row in rows}

    assert report["mode"] == "profile_quality_cost_latency_curve"
    assert list(by_profile) == ["fast", "balanced", "high_recall"]
    for profile, row in by_profile.items():
        assert row["quality"]["primary_classification_f1"] is not None
        assert "hierarchical_classification_f1" in row["quality"]
        assert "clue_recall" in row["quality"]
        assert "llm_calls_per_1000_records" in row["cost"]
        assert "estimated_tokens" in row["cost"]
        assert "p95_latency_ms" in row["latency"]
        assert row["cost"]["profile_budget"]["max_llm_calls"] > 0
        assert row["tradeoff_summary"]["profile"] == profile


def test_evaluate_pipeline_budget_profile_uses_routing_profile_config():
    assert evaluate_pipeline._budget_profile("high_recall")["max_candidate_clues"] == 200
    assert evaluate_pipeline._budget_profile("fast")["max_candidate_clues"] == 20


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


def test_classification_metrics_count_conflict_categories_as_review_predictions():
    from scripts.evaluate_pipeline import evaluate_classification

    records = [
        {
            "trace_id": "overlap",
            "expected_risk_categories": ["账号交易", "工具交易"],
        }
    ]
    actual = [
        {
            "source_trace_id": "overlap",
            "risk_category": "工具交易",
            "conflict_categories": ["账号交易"],
        }
    ]

    metrics = evaluate_classification(records, actual, granularity="primary_only")

    assert metrics["prediction_semantics"]["metric_scope"] == "review_augmented_predictions"
    assert metrics["prediction_semantics"]["conflict_categories_counted_as_predictions"] is True
    assert metrics["primary"]["tp"] == 2
    assert metrics["primary"]["fp"] == 0
    assert metrics["primary"]["fn"] == 0
    assert metrics["primary"]["f1"] == 1.0


def test_classification_metrics_count_conflict_secondary_candidates_for_overlap_review():
    from scripts.evaluate_pipeline import evaluate_classification

    records = [
        {
            "trace_id": "overlap-secondary",
            "expected_risk_categories": ["账号交易", "工具交易"],
            "expected_secondary_labels": ["接码注册", "卡密交易"],
        }
    ]
    actual = [
        {
            "source_trace_id": "overlap-secondary",
            "risk_category": "工具交易",
            "secondary_label": "卡密交易",
            "conflict_categories": ["账号交易"],
            "candidate_secondary_labels": [
                {"label": "卡密交易", "evidence": ["卡密"], "reason": "secondary_gate_ready"},
                {"label": "接码注册", "evidence": ["接码"], "reason": "sms_platform_context"},
            ],
        }
    ]

    metrics = evaluate_classification(records, actual, granularity="hierarchical")

    assert metrics["secondary"]["tp"] == 2
    assert metrics["secondary"]["fp"] == 0
    assert metrics["secondary"]["fn"] == 0
    assert metrics["hierarchical"]["tp"] == 4
    assert metrics["hierarchical"]["fp"] == 0
    assert metrics["hierarchical"]["fn"] == 0


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


def test_classification_error_buckets_split_false_positive_false_negative_and_secondary_confusion():
    from scripts.evaluate_pipeline import evaluate_classification

    records = [
        {
            "trace_id": "fp",
            "content_text": "普通验证码自动填充文章",
            "expected_risk_categories": [],
            "expected_secondary_labels": ["低相关"],
            "human_review": {"typical_error": "false_positive"},
            "source_type": "Social",
        },
        {
            "trace_id": "fn",
            "content_text": "群控脚本 接码平台 联系 TG:risk",
            "expected_risk_categories": ["工具交易"],
            "expected_secondary_labels": ["群控脚本"],
            "human_review": {"typical_error": "seed_missing"},
            "source_type": "IM",
        },
        {
            "trace_id": "secondary-confusion",
            "content_text": "交易所返佣开户链接",
            "expected_risk_categories": ["诈骗引流"],
            "expected_secondary_labels": ["返利引流"],
            "human_review": {"typical_error": "category_confusion"},
            "source_type": "Social",
        },
        {
            "trace_id": "secondary-extra",
            "content_text": "账号出售",
            "expected_risk_categories": ["账号交易"],
            "expected_secondary_labels": [],
            "human_review": {"typical_error": "secondary_missing"},
            "source_type": "Forum",
        },
    ]
    actual = [
        {"source_trace_id": "fp", "risk_category": "诈骗引流", "secondary_label": "私域导流"},
        {"source_trace_id": "fn", "risk_category": "正常业务白噪声", "secondary_label": "低相关"},
        {"source_trace_id": "secondary-confusion", "risk_category": "诈骗引流", "secondary_label": "私域导流"},
        {"source_trace_id": "secondary-extra", "risk_category": "账号交易", "secondary_label": "实名账号买卖"},
    ]

    metrics = evaluate_classification(records, actual)
    buckets = metrics["error_buckets"]
    manual = metrics["manual_review_error_analysis"]

    assert buckets["false_positive"]["count"] == 1
    assert buckets["false_negative"]["count"] == 1
    assert buckets["secondary_confusion"]["count"] == 1
    assert buckets["secondary_extra"]["count"] == 1
    assert buckets["secondary_missing"]["count"] == 1
    assert buckets["primary_confusion"]["count"] == 0
    assert buckets["false_positive"]["examples"][0]["source_trace_id"] == "fp"
    assert buckets["secondary_confusion"]["examples"][0]["expected_secondary"] == ["返利引流"]
    assert manual["by_typical_error"][0]["value"] == "false_positive"
    assert manual["by_source_type"][0]["value"] in {"Social", "IM", "Forum"}


def test_typical_errors_use_formal_secondary_labels_for_negative_gold():
    from scripts.evaluate_pipeline import evaluate_classification

    metrics = evaluate_classification(
        [
            {
                "trace_id": "negative-low-relevance",
                "content_text": "普通验证码教程",
                "expected_risk_categories": [],
                "expected_secondary_labels": ["低相关"],
            }
        ],
        [{"source_trace_id": "negative-low-relevance", "risk_category": "正常业务白噪声", "secondary_label": "低相关"}],
    )

    assert metrics["typical_errors"] == []


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


def test_quality_gate_failures_include_classification_review_rate():
    report = {
        "classification_f1": 1.0,
        "primary_classification_f1": 1.0,
        "secondary_classification_f1": 1.0,
        "hierarchical_classification_f1": 1.0,
        "entity_f1": 1.0,
        "false_positive_rate": 0.0,
        "llm_calls_per_1000_records": 0.0,
        "classification_review_rate": 0.25,
        "clue": {
            "overall": {"recall": 1.0},
            "object_clue_eval": {"overall": {"recall": 1.0}},
            "clue_overgeneration_ratio": 1.0,
            "review_load_per_100_records": 0.0,
        },
    }
    args = Namespace(
        min_classification_f1=None,
        min_primary_classification_f1=None,
        min_secondary_classification_f1=None,
        min_hierarchical_classification_f1=None,
        min_entity_f1=None,
        max_hard_negative_fpr=None,
        max_llm_calls_per_1000=None,
        min_clue_recall=None,
        min_object_clue_recall=None,
        max_clue_overgeneration_ratio=None,
        max_review_load_per_100_records=None,
        max_classification_review_rate=0.2,
    )

    assert quality_gate_failures(report, args) == ["classification_review_rate_above_threshold:0.25>0.2"]


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
