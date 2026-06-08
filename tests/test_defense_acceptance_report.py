from __future__ import annotations

import json
import sys

from scripts import build_defense_acceptance_report


def test_collection_coverage_uses_delivery_manifest_shape_by_default():
    args = build_defense_acceptance_report.parse_args([])
    coverage = build_defense_acceptance_report._collection_coverage(
        {
            "raw_record_count": 4163,
            "source_class_counts": [
                {"source_class": "im_or_group", "count": 3786},
                {"source_class": "social_or_forum", "count": 356},
                {"source_class": "vertical_or_technical", "count": 21},
            ],
            "defense_quota_balanced_sample": {
                "selected_count": 209,
                "class_counts": [
                    {"source_class": "im_or_group", "count": 94},
                    {"source_class": "social_or_forum", "count": 94},
                    {"source_class": "vertical_or_technical", "count": 21},
                ],
                "warnings": [],
            },
        }
    )

    assert args.collection_stats == "data/collection_phase_delivery_manifest.json"
    assert coverage["total_raw_records"] == 4163
    assert coverage["source_class_counts"] == {
        "im_or_group": 3786,
        "social_or_forum": 356,
        "vertical_or_technical": 21,
    }
    assert coverage["defense_balanced_sample"]["selected_count"] == 209
    assert coverage["defense_balanced_sample"]["source_class_counts"]["social_or_forum"] == 94


def test_build_report_aggregates_acceptance_sections_and_test_results(tmp_path):
    collection_stats = tmp_path / "collection.json"
    cleaning_summary = tmp_path / "cleaning.json"
    classification_summary = tmp_path / "classification.json"
    classifications_jsonl = tmp_path / "classifications.jsonl"
    entities_jsonl = tmp_path / "entities.jsonl"
    e2e_evidence = tmp_path / "e2e.json"
    eval_report = tmp_path / "eval.json"
    output = tmp_path / "defense-report.json"

    collection_stats.write_text(
        json.dumps(
            {
                "total_raw_records": 12,
                "source_skew": {
                    "source_class_counts": [
                        {"source_class": "im_or_group", "count": 4},
                        {"source_class": "social_or_forum", "count": 5},
                        {"source_class": "vertical_or_technical", "count": 3},
                    ],
                    "im_or_group_share": 0.3333,
                    "warnings": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cleaning_summary.write_text(
        json.dumps(
            {
                "input_count": 12,
                "cleaned_count": 10,
                "dropped_count": 2,
                "drop_reason_counts": [{"reason": "duplicate", "count": 1}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    classification_summary.write_text(
        json.dumps(
            {
                "classification_count": 10,
                "entity_count": 7,
                "review_required_count": 3,
                "category_counts": [
                    {"risk_category": "工具交易", "count": 4},
                    {"risk_category": "正常业务白噪声", "count": 3},
                    {"risk_category": "unknown", "count": 1},
                ],
                "secondary_label_counts": [
                    {"secondary_label": "群控脚本", "count": 3},
                    {"secondary_label": "待研判", "count": 2},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    entities_jsonl.write_text(
        "\n".join(
            [
                json.dumps({"entity_type": "contact", "source_trace_id": "a"}, ensure_ascii=False),
                json.dumps({"entity_type": "url", "source_trace_id": "b"}, ensure_ascii=False),
                json.dumps({"entity_type": "contact", "source_trace_id": "c"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    classifications_jsonl.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "source_trace_id": "risk",
                        "risk_category": "工具交易",
                        "secondary_label": "群控脚本",
                        "review_required": False,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "source_trace_id": "noise",
                        "risk_category": "正常业务白噪声",
                        "secondary_label": "低相关",
                        "review_required": False,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "source_trace_id": "review",
                        "risk_category": "unknown",
                        "secondary_label": "待研判",
                        "review_required": True,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    e2e_evidence.write_text(
        json.dumps(
            {
                "status": "completed",
                "query": "取当天诈骗引流相关线索",
                "selected_source_classes": ["im_or_group", "social_or_forum"],
                "selected_source_names": ["public-tg", "forum-search"],
                "collection_runs": [
                    {
                        "source_name": "public-tg",
                        "source_class": "im_or_group",
                        "collection_layer": "theme_core",
                        "fetched_count": 6,
                        "status": "completed",
                    },
                    {
                        "source_name": "forum-search",
                        "source_class": "social_or_forum",
                        "collection_layer": "global_core",
                        "fetched_count": 4,
                        "status": "completed",
                    },
                ],
                "counts": {"input_count": 12, "fetched_count": 10, "accepted_count": 8, "high_quality_count": 1, "risk_clue_count": 2},
                "execution_summary": {
                    "elapsed_seconds": 1.25,
                    "budget": {"max_sources": 4, "max_elapsed_seconds": 20},
                    "llm_cost": {"total_usd": 0.02, "prompt_tokens": 120, "completion_tokens": 40},
                },
                "agent_final_output": [
                    {
                        "clue_id": "clue-1",
                        "clue_type": "traffic_diversion",
                        "risk_category": "诈骗引流",
                        "evidence_trace_count": 3,
                        "evidence_trace_ids": ["risk", "review"],
                        "source_names": ["forum"],
                        "evidence_reviewability": {
                            "suggested_review_action": "human_verify_cross_source_trace",
                            "review_action_reasons": ["verify_original_trace"],
                        },
                        "evidence_chain": [
                            {
                                "source_trace_id": "risk",
                                "source_name": "forum",
                                "classification_label": "诈骗引流",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    eval_report.write_text(
        json.dumps(
            {
                "primary_classification_f1": 0.82,
                "false_positive_rate": 0.12,
                "classification_review_rate": 0.3,
                "classification": {
                    "prediction_semantics": {
                        "metric_scope": "review_augmented_predictions",
                        "conflict_categories_counted_as_predictions": True,
                    }
                },
                "clue_f1": 0.42,
                "clue_precision": 0.7,
                "clue_recall": 0.3,
                "clue": {
                    "expected_clue_count": 24,
                    "actual_clue_count": 6,
                    "object_clue_eval": {
                        "overall": {"f1": 0.25},
                        "evidence_chain_precision": 0.5,
                        "evidence_chain_recall": 0.4,
                        "evidence_reviewability_rate": 0.6,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = build_defense_acceptance_report.build_report(
        collection_stats=build_defense_acceptance_report.load_json(collection_stats),
        cleaning_summary=build_defense_acceptance_report.load_json(cleaning_summary),
        classification_summary=build_defense_acceptance_report.load_json(classification_summary),
        classifications=build_defense_acceptance_report.load_jsonl(classifications_jsonl),
        entities=build_defense_acceptance_report.load_jsonl(entities_jsonl),
        e2e_evidence=build_defense_acceptance_report.load_json(e2e_evidence),
        eval_report=build_defense_acceptance_report.load_json(eval_report),
        test_commands=[f"{sys.executable} -c \"print('ok')\""],
        run_tests=True,
    )
    build_defense_acceptance_report.write_report(report, output)

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["collection_coverage"]["total_raw_records"] == 12
    assert saved["collection_coverage"]["source_class_counts"]["social_or_forum"] == 5
    assert saved["cleaning_stats"]["cleaned_count"] == 10
    assert saved["classification_stats"]["unknown_count"] == 1
    assert saved["classification_stats"]["record_review_buckets"] == {
        "explicit_risk": 1,
        "low_relevance": 1,
        "human_review_required": 1,
    }
    assert saved["classification_stats"]["review_split"] == saved["classification_stats"]["record_review_buckets"]
    assert saved["classification_stats"]["review_split_source"] == "classification_rows"
    assert saved["classification_stats"]["summary_estimate_review_split"]["human_review_required"] == 3
    assert saved["classification_stats"]["record_review_bucket_total"] == 3
    assert saved["entity_stats"]["entity_type_counts"]["contact"] == 2
    assert saved["clue_samples"][0]["clue_id"] == "clue-1"
    assert saved["clue_samples"][0]["suggested_review_action"] == "human_verify_cross_source_trace"
    assert saved["evaluation_metrics"]["primary_classification_f1"] == 0.82
    assert saved["evaluation_metrics"]["clue_f1"] == 0.42
    assert saved["evaluation_metrics"]["object_clue_f1"] == 0.25
    assert saved["evaluation_metrics"]["evidence_reviewability_rate"] == 0.6
    assert saved["evaluation_metrics"]["classification_prediction_semantics"]["metric_scope"] == "review_augmented_predictions"
    assert saved["test_results"][0]["returncode"] == 0
    demo = saved["end_to_end_demo"]
    assert demo["query"] == "取当天诈骗引流相关线索"
    assert demo["source_selection"]["selected_source_names"] == ["public-tg", "forum-search"]
    assert demo["source_selection"]["selected_source_classes"] == ["im_or_group", "social_or_forum"]
    assert demo["collection"]["fetched_count"] == 10
    assert demo["cleaning"]["cleaned_count"] == 10
    assert demo["classification"]["record_review_buckets"] == {
        "explicit_risk": 1,
        "low_relevance": 1,
        "human_review_required": 1,
    }
    assert demo["entities"]["entity_type_counts"]["contact"] == 2
    assert demo["clues"]["evidence_chain"][0]["clue_id"] == "clue-1"
    assert demo["clues"]["evidence_chain"][0]["evidence_trace_ids"] == ["risk", "review"]
    assert demo["clues"]["evidence_chain"][0]["suggested_review_action"] == "human_verify_cross_source_trace"
    assert demo["cost_latency"]["elapsed_seconds"] == 1.25
    assert demo["cost_latency"]["llm_cost"]["total_usd"] == 0.02
    assert demo["verification"]["test_results"][0]["status"] == "passed"
    assert demo["evidence_scope"]["mode"] == "single_e2e_artifact_with_supporting_aggregates"
    assert "end_to_end_demo" in saved["acceptance_keys"]


def test_build_report_hydrates_cost_latency_from_referenced_run_artifact(tmp_path):
    run_artifact = tmp_path / "acceptance-run.json"
    run_artifact.write_text(
        json.dumps(
            {
                "elapsed_seconds": 2.75,
                "execution_summary": {
                    "budget": {
                        "max_llm_calls": 40,
                        "max_llm_tokens": 50000,
                        "max_elapsed_seconds": 300,
                    },
                    "llm_cost": {
                        "prompt_tokens": 1200,
                        "completion_tokens": 300,
                        "total_usd": 0.04,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = build_defense_acceptance_report.build_report(
        e2e_evidence={
            "status": "completed",
            "run_artifact": str(run_artifact),
            "agent_final_output": [],
        }
    )

    cost_latency = report["end_to_end_demo"]["cost_latency"]
    assert cost_latency["elapsed_seconds"] == 2.75
    assert cost_latency["budget"]["max_llm_tokens"] == 50000
    assert cost_latency["llm_cost"]["prompt_tokens"] == 1200


def test_build_report_hydrates_nested_live_run_telemetry_from_referenced_artifact(tmp_path):
    run_artifact = tmp_path / "acceptance-run-nested.json"
    run_artifact.write_text(
        json.dumps(
            {
                "execution_summary": {
                    "budget": {
                        "max_llm_calls": 40,
                        "max_llm_tokens": 50000,
                        "max_elapsed_seconds": 300,
                    },
                    "llm_cost": {
                        "budget_reserved_tokens": 8901,
                        "prompt_estimated_tokens": 8901,
                    },
                    "telemetry": {
                        "elapsed_ms": 207244.5,
                        "elapsed_budget_exhausted": False,
                        "budget_controller": {
                            "elapsed_seconds": 196.5113,
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = build_defense_acceptance_report.build_report(
        e2e_evidence={
            "status": "completed",
            "run_artifact": str(run_artifact),
            "agent_final_output": [],
        }
    )

    cost_latency = report["end_to_end_demo"]["cost_latency"]
    assert cost_latency["elapsed_seconds"] == 196.5113
    assert cost_latency["elapsed_ms"] == 207244.5
    assert cost_latency["budget"]["max_llm_tokens"] == 50000
    assert cost_latency["llm_cost"]["budget_reserved_tokens"] == 8901
