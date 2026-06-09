import json

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
