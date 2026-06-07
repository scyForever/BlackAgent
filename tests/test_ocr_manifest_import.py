from __future__ import annotations

import json
import sys

from scripts import build_ocr_hardset
from src.ocr import render_demo_pbm


def test_ocr_hardset_imports_authorized_manifest_rows(tmp_path, monkeypatch):
    image_path = render_demo_pbm("TG:OCR009", tmp_path / "poster.pbm")
    manifest = tmp_path / "manifest.jsonl"
    output = tmp_path / "ocr_manifest.jsonl"
    report = tmp_path / "ocr_manifest_report.json"
    manifest.write_text(
        json.dumps(
            {
                "trace_id": "real-shot-1",
                "source_name": "analyst-authorized-screenshot",
                "source_type": "Image",
                "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
                "image_path": str(image_path),
                "caption": "真实截图标注：群控脚本 接码",
                "expected_risk_categories": ["工具交易"],
                "expected_secondary_labels": ["群控脚本"],
                "expected_entities": [{"entity_type": "contact", "normalized_value": "OCR009"}],
                "annotator": "analyst-a",
                "review_date": "2026-06-07",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = build_ocr_hardset.build_records_from_manifest(manifest)
    assert records[0]["trace_id"] == "real-shot-1"
    assert records[0]["ocr_status"] == "completed"
    assert "TG:OCR009" in records[0]["ocr_text"]
    assert records[0]["expected_risk_categories"] == ["工具交易"]
    assert records[0]["manual_labels"]["annotator"] == "analyst-a"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_ocr_hardset.py",
            "--manifest",
            str(manifest),
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    assert build_ocr_hardset.main() == 0
    saved_rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
    saved_report = json.loads(report.read_text(encoding="utf-8"))
    assert saved_rows[0]["source_name"] == "analyst-authorized-screenshot"
    assert saved_report["run_type"] == "build_ocr_manifest_hardset"
    assert saved_report["record_count"] == 1
    assert saved_report["claim_boundary"]
