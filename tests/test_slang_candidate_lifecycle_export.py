from __future__ import annotations

import csv
import json
import sys

from scripts import build_slang_candidate_report


def test_slang_candidate_report_exports_review_csv_and_lifecycle_records(tmp_path, monkeypatch):
    output = tmp_path / "slang_report.json"
    review_csv = tmp_path / "slang_review.csv"
    lifecycle_json = tmp_path / "slang_lifecycle.json"

    report = {
        "candidates": [
            {
                "term": "火苗",
                "normalized_term": "WhatsApp",
                "source_trace_ids_sample": ["trace-1", "trace-2"],
                "context_examples": ["低价火苗号，联系详聊"],
                "context_markers": ["contact_or_call_to_action"],
            },
            {
                "term": "普通文章",
                "normalized_term": "普通文章",
                "source_trace_ids_sample": ["trace-3"],
                "context_examples": ["普通技术文章"],
                "context_markers": [],
            },
            {
                "term": "影子词",
                "normalized_term": "影子词",
                "source_trace_ids_sample": ["trace-4"],
                "context_examples": ["影子词联系详聊"],
                "context_markers": ["contact_or_call_to_action"],
            },
        ]
    }

    build_slang_candidate_report.write_review_csv(report, review_csv)
    rows = list(csv.DictReader(review_csv.open("r", encoding="utf-8-sig")))
    rows[0]["review_status"] = "approved"
    rows[0]["reviewer"] = "analyst-a"
    rows[0]["target_risk_category"] = "诈骗引流"
    rows[0]["normalized_term"] = "WhatsApp"
    rows[0]["target_stage"] = "ACTIVE"
    rows[0]["notes"] = "confirmed from reviewed traces"
    rows[0]["baseline_eval_report"] = json.dumps({"rule_version": "rules-before", "primary_classification_f1": 0.62})
    rows[0]["post_eval_report"] = json.dumps({"rule_version": "rules-after", "primary_classification_f1": 0.67})
    rows[1]["review_status"] = "rejected"
    rows[1]["reviewer"] = "analyst-a"
    rows[1]["notes"] = "generic phrase"
    rows[2]["review_status"] = "pending"
    rows[2]["reviewer"] = "analyst-a"
    rows[2]["notes"] = "needs more evidence"
    with review_csv.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    lifecycle = build_slang_candidate_report.lifecycle_records_from_review_csv(review_csv)
    assert lifecycle["status"] == "completed"
    assert lifecycle["approved_count"] == 1
    assert lifecycle["rejected_count"] == 1
    assert lifecycle["pending_count"] == 1
    assert [stage["stage"] for stage in lifecycle["lifecycle_flow"]["stages"]] == [
        "candidate",
        "human_review_csv",
        "gray_rollout",
        "activate",
        "evaluation_gain",
    ]
    assert lifecycle["records"][0]["term"] == "火苗"
    assert lifecycle["records"][0]["stage"] == "GRAY_ROLLOUT"
    assert lifecycle["records"][0]["reviewer"] == "analyst-a"
    assert lifecycle["records"][0]["lifecycle_version"]
    assert lifecycle["records"][0]["batch_id"]
    assert lifecycle["records"][0]["target_risk_category"] == "诈骗引流"
    assert lifecycle["records"][0]["evaluation_gain"]["primary_classification_f1_delta"] == 0.05
    assert lifecycle["activation_blocked_count"] == 1
    assert lifecycle["activation_warnings"] == ["火苗:activation_deferred_until_gray_rollout_eval"]
    assert {record["term"] for record in lifecycle["runtime_ready_records"]} == {"火苗"}
    assert "影子词" not in {record["term"] for record in lifecycle["runtime_ready_records"]}

    output.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_slang_candidate_report.py",
            "--records",
            str(tmp_path / "missing-records.jsonl"),
            "--classifications",
            str(tmp_path / "missing-classifications.jsonl"),
            "--output",
            str(output),
            "--review-csv-out",
            str(review_csv),
            "--lifecycle-out",
            str(lifecycle_json),
        ],
    )
    assert build_slang_candidate_report.main() == 0
    assert review_csv.exists()
    assert lifecycle_json.exists()
    lifecycle_payload = json.loads(lifecycle_json.read_text(encoding="utf-8"))
    assert lifecycle_payload["approved_count"] == 1
    assert lifecycle_payload["records"][0]["stage"] == "GRAY_ROLLOUT"
    assert {record["term"] for record in lifecycle_payload["runtime_ready_records"]} == {"火苗"}


def test_slang_lifecycle_respects_gray_rollout_target_stage(tmp_path):
    review_csv = tmp_path / "slang_review.csv"
    review_csv.write_text(
        "\n".join(
            [
                ",".join(build_slang_candidate_report.REVIEW_CSV_FIELDS),
                "火苗,WhatsApp,approved,诈骗引流,GRAY_ROLLOUT,,,,,analyst-a,,gray gate,trace-1,,",
            ]
        )
        + "\n",
        encoding="utf-8-sig",
    )

    lifecycle = build_slang_candidate_report.lifecycle_records_from_review_csv(review_csv)

    assert lifecycle["approved_count"] == 1
    assert lifecycle["records"][0]["stage"] == "GRAY_ROLLOUT"
    assert lifecycle["records"][0]["target_risk_category"] == "诈骗引流"
    assert lifecycle["activation_blocked_count"] == 0
    assert {record["term"] for record in lifecycle["runtime_ready_records"]} == {"火苗"}


def test_slang_lifecycle_defers_active_target_to_gray_rollout_without_post_eval_evidence(tmp_path):
    review_csv = tmp_path / "slang_review.csv"
    review_csv.write_text(
        "\n".join(
            [
                ",".join(build_slang_candidate_report.REVIEW_CSV_FIELDS),
                "火苗,WhatsApp,approved,诈骗引流,ACTIVE,,,,,analyst-a,,needs eval,trace-1,,",
            ]
        )
        + "\n",
        encoding="utf-8-sig",
    )

    lifecycle = build_slang_candidate_report.lifecycle_records_from_review_csv(review_csv)

    assert lifecycle["approved_count"] == 1
    assert lifecycle["activation_blocked_count"] == 1
    assert lifecycle["records"][0]["stage"] == "GRAY_ROLLOUT"
    assert lifecycle["activation_warnings"] == ["火苗:activation_deferred_until_gray_rollout_eval"]
    assert {record["term"] for record in lifecycle["runtime_ready_records"]} == {"火苗"}


def test_slang_lifecycle_defers_active_target_to_gray_rollout_even_with_negative_eval_gain(tmp_path):
    review_csv = tmp_path / "slang_review.csv"
    with review_csv.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=build_slang_candidate_report.REVIEW_CSV_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "term": "火苗",
                "normalized_term": "WhatsApp",
                "review_status": "approved",
                "target_risk_category": "诈骗引流",
                "target_stage": "ACTIVE",
                "baseline_eval_report": json.dumps({"rule_version": "before", "primary_classification_f1": 0.67}),
                "post_eval_report": json.dumps({"rule_version": "after", "primary_classification_f1": 0.66}),
                "reviewer": "analyst-a",
                "notes": "regressed",
                "source_trace_ids": "trace-1",
            }
        )

    lifecycle = build_slang_candidate_report.lifecycle_records_from_review_csv(review_csv)

    assert lifecycle["approved_count"] == 1
    assert lifecycle["activation_blocked_count"] == 1
    assert lifecycle["records"][0]["stage"] == "GRAY_ROLLOUT"
    assert lifecycle["records"][0]["evaluation_gain"]["primary_classification_f1_delta"] == -0.01
    assert lifecycle["activation_warnings"] == ["火苗:activation_deferred_until_gray_rollout_eval"]
    assert {record["term"] for record in lifecycle["runtime_ready_records"]} == {"火苗"}


def test_slang_runtime_loader_activates_only_active_lifecycle_records_by_default():
    manager = build_slang_candidate_report.lifecycle_manager_from_records(
        [
            {"term": "候选词", "normalized_term": "Candidate", "stage": "NEW_CANDIDATE", "evidence_trace_ids": ["c"]},
            {"term": "灰度词", "normalized_term": "Gray", "stage": "GRAY_ROLLOUT", "evidence_trace_ids": ["g"]},
            {"term": "激活词", "normalized_term": "Active", "stage": "ACTIVE", "evidence_trace_ids": ["a"]},
            {"term": "拒绝词", "normalized_term": "Rejected", "stage": "REJECTED", "evidence_trace_ids": ["r"]},
        ]
    )

    assert manager.runtime_terms_mapping() == {"激活词": "Active"}
    assert manager.runtime_terms_mapping(include_gray=True) == {"激活词": "Active", "灰度词": "Gray"}


def test_slang_lifecycle_eval_gain_compares_baseline_and_post_reports():
    gain = build_slang_candidate_report.evaluation_gain_from_reports(
        {
            "rule_version": "rules-before",
            "primary_classification_f1": 0.62,
            "secondary_classification_f1": 0.51,
            "hierarchical_classification_f1": 0.42,
            "entity_f1": 0.9,
            "clue_f1": 0.25,
            "classification_review_rate": 0.45,
        },
        {
            "rule_version": "rules-after",
            "primary_classification_f1": 0.67,
            "secondary_classification_f1": 0.56,
            "hierarchical_classification_f1": 0.49,
            "entity_f1": 0.93,
            "clue_f1": 0.4,
            "classification_review_rate": 0.37,
        },
    )

    assert gain == {
        "baseline_rule_version": "rules-before",
        "post_rule_version": "rules-after",
        "primary_classification_f1_delta": 0.05,
        "secondary_classification_f1_delta": 0.05,
        "hierarchical_classification_f1_delta": 0.07,
        "entity_f1_delta": 0.03,
        "clue_f1_delta": 0.15,
        "classification_review_rate_delta": -0.08,
    }
