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
        ]
    }

    build_slang_candidate_report.write_review_csv(report, review_csv)
    rows = list(csv.DictReader(review_csv.open("r", encoding="utf-8-sig")))
    rows[0]["review_status"] = "approved"
    rows[0]["reviewer"] = "analyst-a"
    rows[0]["target_risk_category"] = "诈骗引流"
    rows[0]["normalized_term"] = "WhatsApp"
    rows[0]["notes"] = "confirmed from reviewed traces"
    rows[1]["review_status"] = "rejected"
    rows[1]["reviewer"] = "analyst-a"
    rows[1]["notes"] = "generic phrase"
    with review_csv.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    lifecycle = build_slang_candidate_report.lifecycle_records_from_review_csv(review_csv)
    assert lifecycle["status"] == "completed"
    assert lifecycle["approved_count"] == 1
    assert lifecycle["rejected_count"] == 1
    assert lifecycle["records"][0]["term"] == "火苗"
    assert lifecycle["records"][0]["stage"] == "REVIEWED"
    assert lifecycle["records"][0]["reviewer"] == "analyst-a"

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
