from __future__ import annotations

import json
import sys

from scripts import build_defense_acceptance_report


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
                "counts": {"high_quality_count": 1, "risk_clue_count": 2},
                "agent_final_output": [
                    {
                        "clue_id": "clue-1",
                        "risk_category": "工具交易",
                        "evidence_trace_count": 3,
                        "source_names": ["forum"],
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
    assert saved["evaluation_metrics"]["primary_classification_f1"] == 0.82
    assert saved["test_results"][0]["returncode"] == 0
