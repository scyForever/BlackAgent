from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from scripts import build_ocr_hardset
from src.ocr import BitmapGlyphOCREngine, OCRImageTextAdapter, TesseractCliOCREngine, render_demo_pbm


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


def test_ocr_materialized_record_binds_original_image_hash_and_source_metadata(tmp_path):
    image_path = render_demo_pbm("TG:OCR013", tmp_path / "chat.pbm")
    adapter = build_ocr_hardset.OCRImageTextAdapter(engine=build_ocr_hardset.BitmapGlyphOCREngine())

    record = adapter.materialize_record(
        {
            "trace_id": "image-bind-1",
            "source_name": "authorized-chat-image",
            "source_type": "Image",
            "source_url": "https://evidence.example/chat/1",
            "crawl_time": "2026-06-09T08:00:00+08:00",
            "raw_payload_uri": "s3://payloads/chat-1.json",
            "capture_snapshot_uri": "s3://snapshots/chat-1.png",
            "image_path": str(image_path),
            "image_kind": "chat",
        }
    )

    evidence = record["image_evidence"][0]
    assert evidence["image_kind"] == "chat"
    assert evidence["image_path"] == str(image_path)
    assert evidence["original_image_uri"] == str(image_path)
    assert evidence["image_sha256"]
    assert evidence["ocr_text"] == "TG:OCR013"
    assert evidence["ocr_engine_provider"] == "image_path"
    assert evidence["ocr_confidence"] == 1.0
    assert evidence["source_url"] == "https://evidence.example/chat/1"
    assert evidence["crawl_time"] == "2026-06-09T08:00:00+08:00"
    assert evidence["raw_payload_uri"] == "s3://payloads/chat-1.json"
    assert evidence["capture_snapshot_uri"] == "s3://snapshots/chat-1.png"


def test_generated_ocr_hardset_rows_bind_original_image_evidence(tmp_path):
    records = build_ocr_hardset.build_records(count=4, image_dir=tmp_path)

    assert len(records) == 4
    for record in records:
        evidence = record["image_evidence"][0]
        assert evidence["image_kind"] in {"chat", "poster", "qr", "screenshot"}
        assert evidence["image_path"] == record["image_path"]
        assert evidence["original_image_uri"] == record["image_path"]
        assert evidence["image_sha256"]
        assert evidence["ocr_text"] == record["expected_image_text"]
        assert evidence["ocr_engine_provider"] == "bitmap_glyph"


def test_ocr_image_evidence_binds_each_image_to_its_engine_text(tmp_path):
    first_image = render_demo_pbm("TG:OCR101", tmp_path / "first.pbm")
    second_image = render_demo_pbm("TG:OCR102", tmp_path / "second.pbm")
    adapter = OCRImageTextAdapter(engine=BitmapGlyphOCREngine())

    record = adapter.materialize_record(
        {
            "trace_id": "multi-image-bind",
            "caption": "caption context should stay in aggregate text only",
            "images": [
                {"image_path": str(first_image), "image_kind": "chat"},
                {"image_path": str(second_image), "image_kind": "poster"},
            ],
        }
    )

    assert "caption context" in record["ocr_text"]
    assert "TG:OCR101" in record["ocr_text"]
    assert "TG:OCR102" in record["ocr_text"]
    assert record["image_evidence"][0]["ocr_text"] == "TG:OCR101"
    assert record["image_evidence"][1]["ocr_text"] == "TG:OCR102"


def test_generated_ocr_hardset_uses_repo_relative_image_paths():
    image_dir = Path("tests/evaluation/.tmp_ocr_portability")
    try:
        records = build_ocr_hardset.build_records(count=1, image_dir=image_dir)
        evidence = records[0]["image_evidence"][0]
        assert not Path(records[0]["image_path"]).is_absolute()
        assert not Path(evidence["image_path"]).is_absolute()
        assert evidence["original_image_uri"] == evidence["image_path"]
    finally:
        shutil.rmtree(image_dir, ignore_errors=True)


def test_tesseract_engine_passes_custom_tessdata_prefix(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, *, check, capture_output, text, timeout, env):
        captured["command"] = command
        captured["env"] = env

        class Result:
            returncode = 0
            stdout = "TG:OCR012"
            stderr = ""

        return Result()

    monkeypatch.setattr("src.ocr.engines.shutil.which", lambda executable: executable)
    monkeypatch.setattr("src.ocr.engines.subprocess.run", fake_run)

    engine = TesseractCliOCREngine(
        executable="tesseract",
        language="chi_sim+eng",
        tessdata_dir=tmp_path / "tessdata",
    )

    assert engine(tmp_path / "poster.png") == "TG:OCR012"
    assert captured["command"] == ["tesseract", str(tmp_path / "poster.png"), "stdout", "-l", "chi_sim+eng"]
    assert captured["env"]["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")


def test_ocr_report_records_engine_provider_and_expectation_quality_metrics(tmp_path):
    image_path = render_demo_pbm("TG:OCR010", tmp_path / "poster.pbm")
    records = build_ocr_hardset.build_records_from_manifest(
        _write_manifest(
            tmp_path / "manifest.jsonl",
            [
                {
                    "trace_id": "real-shot-1",
                    "source_name": "analyst-authorized-screenshot",
                    "source_type": "Image",
                    "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
                    "image_path": str(image_path),
                    "caption": "真实截图标注：手工单",
                    "expected_image_text": "TG:OCR010",
                }
            ],
        )
    )

    report = build_ocr_hardset.build_report(records, output_path=tmp_path / "out.jsonl", manifest_path=tmp_path / "manifest.jsonl")

    assert records[0]["ocr_engine_provider"] == "bitmap_glyph"
    assert report["ocr_engine_provider_counts"] == {"bitmap_glyph": 1}
    assert report["ocr_quality_metrics"] == {
        "evaluated_count": 1,
        "exact_match_count": 0,
        "exact_match_rate": 0.0,
        "substring_match_count": 1,
        "substring_match_rate": 1.0,
    }
    assert report["ocr_engine_comparison"]["configured_engines"] == ["bitmap_glyph"]
    assert report["ocr_engine_comparison"]["unavailable_engines"] == []
    assert report["ocr_engine_comparison"]["engine_availability"] == {
        "bitmap_glyph": "configured",
        "TesseractCliOCREngine": "not_configured",
        "cloud_ocr_callable": "not_configured",
    }


def test_ocr_manifest_report_tracks_required_image_kind_coverage(tmp_path):
    records = build_ocr_hardset.build_records_from_manifest(
        _write_manifest(
            tmp_path / "manifest.jsonl",
            [
                {
                    "trace_id": "shot-screenshot",
                    "image_path": str(render_demo_pbm("TG:OCR014", tmp_path / "screenshot.pbm")),
                    "image_kind": "screenshot",
                    "expected_image_text": "TG:OCR014",
                },
                {
                    "trace_id": "shot-poster",
                    "image_path": str(render_demo_pbm("TG:OCR015", tmp_path / "poster.pbm")),
                    "image_kind": "poster",
                    "expected_image_text": "TG:OCR015",
                },
            ],
        )
    )

    report = build_ocr_hardset.build_report(
        records,
        output_path=tmp_path / "out.jsonl",
        manifest_path=tmp_path / "manifest.jsonl",
    )

    assert report["image_kind_coverage"]["required_kinds"] == ["chat", "poster", "qr", "screenshot"]
    assert report["image_kind_coverage"]["present_kinds"] == ["poster", "screenshot"]
    assert report["image_kind_coverage"]["missing_kinds"] == ["chat", "qr"]
    assert report["image_kind_coverage"]["complete"] is False


def test_ocr_manifest_report_includes_real_scene_assessment_for_authorized_rows(tmp_path):
    records = [
        {
            "trace_id": "ocr-pass",
            "source_name": "analyst-authorized-screenshot",
            "source_type": "Image",
            "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            "image_kind": "screenshot",
            "content_text": "caption TG:OCR021 tool-alpha",
            "ocr_text": "TG:OCR021 tool-alpha",
            "ocr_status": "completed",
            "ocr_errors": [],
            "expected_image_text": "TG:OCR021",
            "expected_entities": [{"entity_type": "contact", "normalized_value": "OCR021"}],
        },
        {
            "trace_id": "ocr-fail",
            "source_name": "analyst-authorized-poster",
            "source_type": "Image",
            "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            "image_kind": "poster",
            "content_text": "caption only",
            "ocr_text": "",
            "ocr_status": "missing_ocr_text",
            "ocr_errors": ["no readable image text"],
            "expected_image_text": "TG:OCR022",
            "expected_entities": [{"entity_type": "contact", "normalized_value": "OCR022"}],
        },
    ]

    report = build_ocr_hardset.build_report(
        records,
        output_path=tmp_path / "out.jsonl",
        manifest_path=tmp_path / "manifest.jsonl",
    )

    assessment = report["real_scene_assessment"]
    assert report["status"] == "completed"
    assert assessment["target_range"] == {"min": 30, "max": 50}
    assert assessment["authorized_manifest_count"] == 2
    assert assessment["coverage_status"] == "insufficient_real_authorized_screenshots"
    assert assessment["image_kind_counts"] == {"screenshot": 1, "poster": 1}
    assert assessment["ocr_quality_metrics"]["evaluated_count"] == 2
    assert assessment["entity_extraction_impact"]["expected_entity_count"] == 2
    assert assessment["entity_extraction_impact"]["ocr_supported_entity_count"] == 1
    assert assessment["failure_samples"][0]["trace_id"] == "ocr-fail"


def test_generated_pbm_report_does_not_claim_real_scene_completion(tmp_path):
    records = build_ocr_hardset.build_records(count=20, image_dir=tmp_path)

    report = build_ocr_hardset.build_report(records, output_path=tmp_path / "out.jsonl")

    assert report["status"] == "completed"
    assert report["real_scene_assessment"]["authorized_manifest_count"] == 0
    assert report["real_scene_assessment"]["coverage_status"] == "not_real_scene_manifest"
    assert "not real-scene proof" in report["real_scene_assessment"]["claim_boundary"]


def test_real_scene_assessment_requires_explicit_authorization_for_coverage(tmp_path):
    authorized_records = [
        {
            "trace_id": f"authorized-{index:02d}",
            "source_type": "Image",
            "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            "image_kind": "screenshot",
            "ocr_text": f"TG:AUTH{index:02d}",
            "ocr_status": "completed",
            "expected_image_text": f"TG:AUTH{index:02d}",
            "expected_entities": [],
        }
        for index in range(30)
    ]
    records = [
        *authorized_records,
        {
            "trace_id": "missing-authorization",
            "source_type": "Image",
            "legal_basis": "",
            "image_kind": "poster",
            "ocr_text": "TG:MISSING",
            "ocr_status": "completed",
            "expected_image_text": "TG:MISSING",
            "expected_entities": [],
        },
        {
            "trace_id": "unauthorized-row",
            "source_type": "Image",
            "legal_basis": "UNAUTHORIZED_SOURCE",
            "image_kind": "poster",
            "ocr_text": "TG:UNAUTH",
            "ocr_status": "completed",
            "expected_image_text": "TG:UNAUTH",
            "expected_entities": [],
        },
    ]

    report = build_ocr_hardset.build_report(
        records,
        output_path=tmp_path / "out.jsonl",
        manifest_path=tmp_path / "manifest.jsonl",
    )

    assert report["status"] == "completed"
    assert report["real_scene_assessment"]["authorized_manifest_count"] == 30
    assert report["real_scene_assessment"]["coverage_status"] == "completed_real_authorized_screenshots"
    assert report["real_scene_assessment"]["image_kind_counts"] == {"screenshot": 30}


def test_real_scene_assessment_rejects_negative_authorization_substrings(tmp_path):
    negative_values = (
        "NOT_AUTHORIZED",
        "NO_PERMISSION",
        "NON_AUTHORIZED",
        "UNLICENSED",
        "REVOKED",
        "EXPIRED",
        "NOT_AUTHORIZED_SOURCE",
        "NO_PERMISSION_SOURCE",
        "NON_AUTHORIZED_MANIFEST",
    )
    records = [
        {
            "trace_id": f"negative-{index:02d}",
            "source_type": "Image",
            "legal_basis": negative_values[index % len(negative_values)],
            "image_kind": "screenshot",
            "ocr_text": f"TG:NEG{index:02d}",
            "ocr_status": "completed",
            "expected_image_text": f"TG:NEG{index:02d}",
            "expected_entities": [],
        }
        for index in range(30)
    ]

    report = build_ocr_hardset.build_report(
        records,
        output_path=tmp_path / "out.jsonl",
        manifest_path=tmp_path / "manifest.jsonl",
    )

    assert report["status"] == "completed"
    assert report["real_scene_assessment"]["authorized_manifest_count"] == 0
    assert report["real_scene_assessment"]["coverage_status"] == "insufficient_real_authorized_screenshots"


def test_real_scene_assessment_rejects_ambiguous_authorization_tokens(tmp_path):
    ambiguous_values = ("CONSENT_REQUIRED", "PERMISSION_PENDING", "PERMISSION_NEEDED", "LICENSED_REQUIRED")
    records = [
        {
            "trace_id": f"ambiguous-{index:02d}",
            "source_type": "Image",
            "legal_basis": ambiguous_values[index % len(ambiguous_values)],
            "image_kind": "screenshot",
            "ocr_text": f"TG:AMB{index:02d}",
            "ocr_status": "completed",
            "expected_image_text": f"TG:AMB{index:02d}",
            "expected_entities": [],
        }
        for index in range(30)
    ]

    report = build_ocr_hardset.build_report(
        records,
        output_path=tmp_path / "out.jsonl",
        manifest_path=tmp_path / "manifest.jsonl",
    )

    assert report["status"] == "completed"
    assert report["real_scene_assessment"]["authorized_manifest_count"] == 0
    assert report["real_scene_assessment"]["coverage_status"] == "insufficient_real_authorized_screenshots"


def test_real_scene_failure_samples_keep_string_ocr_errors_as_single_entry(tmp_path):
    records = [
        {
            "trace_id": "string-error",
            "source_type": "Image",
            "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            "image_kind": "poster",
            "ocr_text": "",
            "ocr_status": "missing_ocr_text",
            "ocr_errors": "single ocr failure",
            "expected_image_text": "TG:ERR001",
            "expected_entities": [],
        }
    ]

    report = build_ocr_hardset.build_report(
        records,
        output_path=tmp_path / "out.jsonl",
        manifest_path=tmp_path / "manifest.jsonl",
    )

    assert report["real_scene_assessment"]["failure_samples"][0]["ocr_errors"] == ["single ocr failure"]


def test_ocr_report_records_per_engine_quality_latency_failure_and_cost_metrics(tmp_path):
    records = [
        {
            "trace_id": "ocr-1",
            "ocr_text": "caption TG:OCR001",
            "ocr_status": "completed",
            "ocr_errors": [],
            "ocr_engine_provider": "bitmap_glyph,tesseract",
            "ocr_engine_outputs": {"bitmap_glyph": "TG:OCR001", "tesseract": "TG OCR001"},
            "ocr_engine_latencies_ms": {"bitmap_glyph": 1.25, "tesseract": 18.5},
            "ocr_engine_costs": {"bitmap_glyph": 0.0, "tesseract": 0.0},
            "expected_image_text": "TG:OCR001",
        },
        {
            "trace_id": "ocr-2",
            "ocr_text": "caption TG:OCR002",
            "ocr_status": "partial",
            "ocr_errors": ["ocr_engine_error:tesseract:/tmp/poster.png:tesseract_failed:missing chi_sim"],
            "ocr_engine_provider": "bitmap_glyph,tesseract",
            "ocr_engine_outputs": {"bitmap_glyph": "TG:OCR002"},
            "ocr_engine_latencies_ms": {"bitmap_glyph": 1.5},
            "ocr_engine_costs": {"bitmap_glyph": 0.0},
            "expected_image_text": "TG:OCR002",
        },
    ]

    report = build_ocr_hardset.build_report(records, output_path=tmp_path / "out.jsonl", manifest_path=tmp_path / "manifest.jsonl")

    assert report["ocr_quality_metrics"]["evaluated_count"] == 2
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["bitmap_glyph"] == {
        "evaluated_count": 2,
        "exact_match_count": 2,
        "exact_match_rate": 1.0,
        "substring_match_count": 2,
        "substring_match_rate": 1.0,
        "failure_count": 0,
        "failure_rate": 0.0,
        "latency_count": 2,
        "avg_latency_ms": 1.375,
        "total_cost": 0.0,
        "avg_cost": 0.0,
    }
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["evaluated_count"] == 2
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["exact_match_rate"] == 0.0
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["substring_match_rate"] == 0.0
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["failure_count"] == 1
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["failure_rate"] == 0.5
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["latency_count"] == 1
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["avg_latency_ms"] == 18.5
    assert report["ocr_engine_comparison"]["engine_quality_metrics"]["tesseract"]["total_cost"] == 0.0


def test_ocr_report_surfaces_unavailable_external_engine_errors(tmp_path):
    records = [
        {
            "trace_id": "external-ocr-1",
            "content_text": "",
            "ocr_text": "",
            "ocr_status": "missing_ocr_text",
            "ocr_sources": [],
            "ocr_errors": ["ocr_engine_error:tesseract:/tmp/poster.png:tesseract_not_found:tesseract"],
            "ocr_engine_provider": "none",
            "expected_image_text": "TG:OCR404",
            "expected_risk_categories": ["工具交易"],
            "expected_entities": [],
        }
    ]

    report = build_ocr_hardset.build_report(records, output_path=tmp_path / "out.jsonl", manifest_path=tmp_path / "manifest.jsonl")

    assert report["ocr_quality_metrics"]["evaluated_count"] == 1
    assert report["ocr_quality_metrics"]["substring_match_rate"] == 0.0
    assert report["ocr_engine_comparison"]["unavailable_engines"] == [
        {
            "engine": "tesseract",
            "error_count": 1,
            "sample_errors": ["ocr_engine_error:tesseract:/tmp/poster.png:tesseract_not_found:tesseract"],
        }
    ]
    assert report["ocr_engine_comparison"]["engine_availability"]["TesseractCliOCREngine"] == "unavailable"


def test_ocr_report_marks_configured_tesseract_engine_available_when_no_engine_error(tmp_path):
    records = [
        {
            "trace_id": "external-ocr-1",
            "ocr_text": "TG:OCR404",
            "ocr_status": "completed",
            "ocr_sources": ["ocr_engine.tesseract"],
            "ocr_errors": [],
            "ocr_engine_provider": "tesseract",
            "ocr_engine_outputs": {"tesseract": "TG:OCR404"},
            "expected_image_text": "TG:OCR404",
        }
    ]

    report = build_ocr_hardset.build_report(records, output_path=tmp_path / "out.jsonl", manifest_path=tmp_path / "manifest.jsonl")

    assert report["ocr_engine_comparison"]["configured_engines"] == ["tesseract"]
    assert report["ocr_engine_comparison"]["engine_availability"]["TesseractCliOCREngine"] == "configured"


def test_ocr_report_records_tesseract_and_cloud_environment_status(tmp_path, monkeypatch):
    monkeypatch.delenv("BLACKAGENT_CLOUD_OCR_API_KEY", raising=False)
    records = [
        {
            "trace_id": "external-ocr-1",
            "content_text": "",
            "ocr_text": "",
            "ocr_status": "missing_ocr_text",
            "ocr_sources": [],
            "ocr_errors": ["ocr_engine_error:tesseract:/tmp/poster.png:tesseract_not_found:tesseract"],
            "ocr_engine_provider": "tesseract",
            "expected_image_text": "TG:OCR404",
        }
    ]

    report = build_ocr_hardset.build_report(
        records,
        output_path=tmp_path / "out.jsonl",
        manifest_path=tmp_path / "manifest.jsonl",
        tesseract_executable="missing-tesseract",
        tesseract_language="chi_sim+eng",
    )

    assert report["ocr_engine_comparison"]["tesseract_environment"]["executable"] == "missing-tesseract"
    assert report["ocr_engine_comparison"]["tesseract_environment"]["language"] == "chi_sim+eng"
    assert report["ocr_engine_comparison"]["tesseract_environment"]["tessdata_dir"] is None
    assert report["ocr_engine_comparison"]["tesseract_environment"]["version_status"] == "not_found"
    assert report["ocr_engine_comparison"]["tesseract_environment"]["language_status"] == "not_checked"
    assert report["ocr_engine_comparison"]["tesseract_environment"]["required_languages"] == ["chi_sim", "eng"]
    assert report["ocr_engine_comparison"]["cloud_ocr_environment"] == {
        "provider": "not_configured",
        "api_key_env": "BLACKAGENT_CLOUD_OCR_API_KEY",
        "api_key_status": "missing",
        "configured": False,
        "claim_boundary": "Cloud OCR is not evaluated until a provider key and callable engine are configured.",
    }


def test_ocr_hardset_cli_can_evaluate_manifest_with_tesseract_engine(tmp_path, monkeypatch):
    image_path = render_demo_pbm("TG:OCR011", tmp_path / "poster.pbm")
    manifest = _write_manifest(
        tmp_path / "manifest.jsonl",
        [
            {
                "trace_id": "real-shot-tesseract",
                "source_name": "analyst-authorized-screenshot",
                "source_type": "Image",
                "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
                "image_path": str(image_path),
                "expected_image_text": "TG:OCR011",
            }
        ],
    )
    output = tmp_path / "ocr_manifest.jsonl"
    report = tmp_path / "ocr_manifest_report.json"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_ocr_hardset.py",
            "--manifest",
            str(manifest),
            "--ocr-engine",
            "tesseract",
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )

    assert build_ocr_hardset.main() == 0
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["ocr_engine_comparison"]["configured_engines"] == ["tesseract"]
    assert saved["ocr_engine_comparison"]["engine_availability"]["TesseractCliOCREngine"] in {"configured", "unavailable"}
    assert "bitmap_glyph" not in saved["ocr_engine_provider_counts"]


def _write_manifest(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path
