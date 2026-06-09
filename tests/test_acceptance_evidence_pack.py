import json
import subprocess
import sys

from scripts.build_acceptance_evidence_pack import build_evidence_pack, load_jsonl, write_jsonl


def test_build_acceptance_evidence_pack_joins_classification_entities_and_clues(tmp_path):
    acceptance_path = tmp_path / "acceptance.jsonl"
    classifications_path = tmp_path / "classifications.jsonl"
    entities_path = tmp_path / "entities.jsonl"
    clues_path = tmp_path / "clues.jsonl"
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"

    write_jsonl(
        [
            {
                "trace_id": "trace-1",
                "source_trace_id": "trace-1",
                "source_name": "v2ex-direct",
                "source_type": "Forum",
                "content_text": "原始帖：群控脚本引流，联系 TG:risk01",
                "normalized_text": "群控脚本引流 联系 TG:risk01",
            }
        ],
        acceptance_path,
    )
    write_jsonl(
        [
            {
                "source_trace_id": "trace-1",
                "risk_category": "工具交易",
                "secondary_label": "群控脚本",
                "confidence": 0.91,
                "evidence": ["群控", "脚本"],
            }
        ],
        classifications_path,
    )
    write_jsonl(
        [
            {
                "source_trace_id": "trace-1",
                "entity_type": "contact",
                "normalized_value": "Telegram:risk01",
                "confidence": 1.0,
            }
        ],
        entities_path,
    )
    write_jsonl(
        [
            {
                "clue_id": "clue-1",
                "clue_type": "shared_contact_48h",
                "risk_category": "工具交易",
                "key": "Telegram:risk01",
                "evidence_trace_ids": ["trace-1", "trace-2"],
                "source_names": ["v2ex-direct", "telegram-direct"],
            }
        ],
        clues_path,
    )

    report = build_evidence_pack(
        load_jsonl(acceptance_path),
        classifications=load_jsonl(classifications_path),
        entities=load_jsonl(entities_path),
        clues=load_jsonl(clues_path),
        output_path=output_path,
        report_path=report_path,
    )

    rows = load_jsonl(output_path)
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["status"] == "completed"
    assert saved_report["record_count"] == 1
    assert rows[0]["trace_id"] == "trace-1"
    assert rows[0]["raw_snippet"] == "原始帖：群控脚本引流，联系 TG:risk01"
    assert rows[0]["clean_text"] == "群控脚本引流 联系 TG:risk01"
    assert rows[0]["classification"]["risk_category"] == "工具交易"
    assert rows[0]["entities"][0]["normalized_value"] == "Telegram:risk01"
    assert rows[0]["clue_chain"][0]["clue_id"] == "clue-1"
    assert rows[0]["review_chain"]["status"] == "linked_to_cross_source_clue"
    assert rows[0]["evidence_completeness"]["has_raw_snippet"] is True
    assert rows[0]["evidence_completeness"]["has_classification"] is True
    assert rows[0]["evidence_completeness"]["has_entities"] is True
    assert rows[0]["evidence_completeness"]["has_clue_chain"] is True


def test_build_acceptance_evidence_pack_adds_review_chain_when_cross_source_clue_missing(tmp_path):
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"

    report = build_evidence_pack(
        [
            {
                "trace_id": "trace-1",
                "source_trace_id": "trace-1",
                "source_name": "article-direct",
                "source_type": "public_account",
                "content_text": "原文片段：接码平台风险分析",
                "clean_text": "接码平台风险分析",
            }
        ],
        classifications=[
            {
                "source_trace_id": "trace-1",
                "risk_category": "账号交易",
                "secondary_label": "接码注册",
                "confidence": 0.84,
            }
        ],
        entities=[
            {
                "source_trace_id": "trace-1",
                "entity_type": "tool_name",
                "normalized_value": "接码平台",
                "confidence": 0.9,
            }
        ],
        clues=[],
        output_path=output_path,
        report_path=report_path,
    )

    rows = load_jsonl(output_path)

    assert report["review_status_counts"]["no_cross_source_clue_yet"] == 1
    assert rows[0]["clue_chain"][0]["status"] == "no_cross_source_clue_yet"
    assert rows[0]["clue_chain"][0]["clue_type"] == "single_record_review_chain"
    assert rows[0]["clue_chain"][0]["evidence_trace_ids"] == ["trace-1"]
    assert rows[0]["evidence_completeness"]["has_clue_chain"] is True
    assert rows[0]["evidence_completeness"]["has_cross_source_clue"] is False


def test_build_acceptance_evidence_pack_prefers_linked_cleaned_text(tmp_path):
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"

    build_evidence_pack(
        [
            {
                "trace_id": "trace-1",
                "source_trace_id": "trace-1",
                "source_name": "article-direct",
                "source_type": "public_account",
                "content_text": "原始 带 噪声 文本",
            }
        ],
        classifications=[],
        entities=[],
        cleaned=[
            {
                "source_trace_id": "trace-1",
                "clean_text": "清洗后文本",
                "quality_score": 0.92,
                "risk_level": "HIGH",
            }
        ],
        clues=[],
        output_path=output_path,
        report_path=report_path,
    )

    rows = load_jsonl(output_path)

    assert rows[0]["clean_text"] == "清洗后文本"
    assert rows[0]["cleaning"]["source"] == "cleaning_phase"
    assert rows[0]["cleaning"]["quality_score"] == 0.92
    assert rows[0]["evidence_completeness"]["has_clean_text"] is True


def test_build_acceptance_evidence_pack_inline_cleans_rows_without_cleaning_artifact(tmp_path):
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"

    report = build_evidence_pack(
        [
            {
                "trace_id": "trace-1",
                "source_trace_id": "trace-1",
                "source_name": "article-direct",
                "source_type": "public_account",
                "content_text": "  群 控 脚本   加 V：risk01  ",
            }
        ],
        classifications=[],
        entities=[],
        cleaned=[],
        clues=[],
        output_path=output_path,
        report_path=report_path,
    )

    rows = load_jsonl(output_path)

    assert rows[0]["clean_text"] == "群 控 脚本 加 V:risk01"
    assert rows[0]["cleaning"]["source"] == "evidence_pack_inline_cleaning"
    assert rows[0]["cleaning"]["cleaning_version"] == "evidence_pack_inline_cleaner_v1"
    assert rows[0]["cleaning"]["claim_boundary"] == "cleaned_inline_for_acceptance_evidence_not_persisted_cleaning_phase"
    assert rows[0]["evidence_completeness"]["has_clean_text"] is True
    assert rows[0]["evidence_completeness"]["has_cleaning_phase_text"] is False
    assert rows[0]["evidence_completeness"]["has_inline_cleaning_text"] is True
    assert rows[0]["evidence_completeness"]["has_auditable_clean_text"] is True
    assert report["cleaning_source_counts"]["evidence_pack_inline_cleaning"] == 1


def test_build_acceptance_evidence_pack_preserves_full_source_evidence_and_cleaning_drop(tmp_path):
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"
    full_body = "full hydrated body " + ("risk detail " * 80)
    supplied_snippet = "crawler supplied snippet " + ("more detail " * 60)

    build_evidence_pack(
        [
            {
                "trace_id": "trace-full",
                "source_trace_id": "trace-full",
                "source_name": "hydrated-search",
                "source_type": "public_page",
                "source_url": "https://target.example/item/1",
                "content_text": full_body,
                "raw_snippet": supplied_snippet,
                "crawl_time": "2026-06-01T08:30:00Z",
                "publish_time": "2026-05-30T12:00:00Z",
                "capture_snapshot_uri": "s3://bucket/snapshots/trace-full.html",
                "raw_payload_uri": "s3://bucket/payloads/trace-full.json",
                "ocr_text": "OCR extracted risk text",
                "ocr_confidence": 0.87,
                "content_modality": "image_text",
                "image_path": "artifacts/images/trace-full.png",
                "screenshot_path": "artifacts/screenshots/trace-full.png",
                "attachments": [
                    {"kind": "image", "uri": "s3://bucket/attachments/trace-full-1.png"},
                ],
            }
        ],
        classifications=[],
        entities=[],
        cleaned=[],
        clues=[],
        cleaning_drops=[
            {
                "source_trace_id": "trace-full",
                "reason": "duplicate",
                "noise_score": 0.42,
                "dedup_group_id": "dedup-1",
                "similarity": 0.98,
                "stage": "cleaning_phase",
            }
        ],
        output_path=output_path,
        report_path=report_path,
    )

    row = load_jsonl(output_path)[0]

    assert row["raw_snippet"] == supplied_snippet
    assert row["source_evidence"] == {
        "raw_text": full_body,
        "raw_snippet": supplied_snippet,
        "crawl_time": "2026-06-01T08:30:00Z",
        "publish_time": "2026-05-30T12:00:00Z",
        "source_url": "https://target.example/item/1",
        "capture_snapshot_uri": "s3://bucket/snapshots/trace-full.html",
        "raw_payload_uri": "s3://bucket/payloads/trace-full.json",
        "ocr": {
            "ocr_text": "OCR extracted risk text",
            "ocr_confidence": 0.87,
            "content_modality": "image_text",
        },
        "media": {
            "image_path": "artifacts/images/trace-full.png",
            "screenshot_path": "artifacts/screenshots/trace-full.png",
            "attachments": [
                {"kind": "image", "uri": "s3://bucket/attachments/trace-full-1.png"},
            ],
        },
        "cleaning_drop": {
            "source_trace_id": "trace-full",
            "reason": "duplicate",
            "noise_score": 0.42,
            "dedup_group_id": "dedup-1",
            "similarity": 0.98,
            "stage": "cleaning_phase",
        },
    }


def test_build_acceptance_evidence_pack_uses_hydrated_page_body_over_search_snippet(tmp_path):
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"
    search_snippet = "search result snippet " + ("short " * 20)
    hydrated_body = "target page hydrated body " + ("full detail " * 120)

    report = build_evidence_pack(
        [
            {
                "trace_id": "search-trace",
                "source_trace_id": "search-trace",
                "source_name": "public-search",
                "source_type": "Search",
                "source_url": "https://target.example/thread/1",
                "content_text": search_snippet,
                "search_query_url": "https://search.example?q=risk",
                "crawl_time": "2026-06-01T08:00:00Z",
            }
        ],
        classifications=[],
        entities=[],
        cleaned=[],
        clues=[],
        hydrated=[
            {
                "trace_id": "hydrated-trace",
                "hydrated_from_trace_id": "search-trace",
                "source_url": "https://target.example/thread/1",
                "content_text": hydrated_body,
                "capture_snapshot_uri": "http://r.jina.ai/http://target.example/thread/1",
                "raw_payload_uri": "http://r.jina.ai/http://target.example/thread/1",
                "crawl_time": "2026-06-01T08:01:00Z",
            }
        ],
        output_path=output_path,
        report_path=report_path,
    )

    row = load_jsonl(output_path)[0]

    assert row["raw_snippet"] == search_snippet[:500]
    assert row["source_evidence"]["raw_text"] == hydrated_body
    assert row["source_evidence"]["raw_snippet"] == search_snippet[:500]
    assert row["source_evidence"]["capture_snapshot_uri"] == "http://r.jina.ai/http://target.example/thread/1"
    assert row["source_evidence"]["hydrated_trace_id"] == "hydrated-trace"
    assert report["source_evidence_counts"]["has_hydrated_body"] == 1
    assert report["source_evidence_counts"]["raw_text_differs_from_raw_snippet"] == 1
    assert report["source_evidence_counts"]["has_capture_snapshot_uri"] == 1


def test_build_acceptance_evidence_pack_uses_raw_text_when_content_text_missing(tmp_path):
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"

    build_evidence_pack(
        [
            {
                "trace_id": "trace-raw",
                "source_trace_id": "trace-raw",
                "source_name": "raw-only",
                "source_type": "forum",
                "source_url": "https://target.example/raw",
                "raw_text": "full raw fallback body",
            }
        ],
        classifications=[],
        entities=[],
        cleaned=[],
        clues=[],
        output_path=output_path,
        report_path=report_path,
    )

    row = load_jsonl(output_path)[0]

    assert row["source_evidence"]["raw_text"] == "full raw fallback body"
    assert row["source_evidence"]["raw_snippet"] == "full raw fallback body"


def test_build_acceptance_evidence_pack_cli_accepts_cleaning_drop_artifact(tmp_path):
    acceptance_path = tmp_path / "acceptance.jsonl"
    drops_path = tmp_path / "cleaning_drops.jsonl"
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"

    write_jsonl(
        [
            {
                "trace_id": "trace-drop",
                "source_trace_id": "trace-drop",
                "source_name": "direct",
                "source_type": "forum",
                "content_text": "原始公开帖正文",
            }
        ],
        acceptance_path,
    )
    write_jsonl(
        [
            {
                "source_trace_id": "trace-drop",
                "reason": "too_short_after_cleaning",
                "stage": "cleaning_phase",
            }
        ],
        drops_path,
    )

    subprocess.run(
        [
            sys.executable,
            "scripts/build_acceptance_evidence_pack.py",
            "--acceptance-pack",
            str(acceptance_path),
            "--classifications",
            str(tmp_path / "missing_classifications.jsonl"),
            "--entities",
            str(tmp_path / "missing_entities.jsonl"),
            "--cleaning-drops",
            str(drops_path),
            "--output",
            str(output_path),
            "--report-out",
            str(report_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    row = load_jsonl(output_path)[0]

    assert row["source_evidence"]["cleaning_drop"]["reason"] == "too_short_after_cleaning"
