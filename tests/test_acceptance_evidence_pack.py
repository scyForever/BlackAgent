import json
import subprocess
import sys

from scripts.build_acceptance_evidence_pack import (
    build_clue_evidence_index,
    build_evidence_pack,
    load_jsonl,
    parse_args,
    write_jsonl,
)


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
                "start": 12,
                "end": 21,
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
                "threshold_reason": "same_contact_appears_in_at_least_2_sources_within_48h",
                "quality_level": "high",
                "quality_reasons": ["cross_source_confirmed"],
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
    assert rows[0]["entities"][0]["source_snippet"] == "原始帖：群控脚本引流，联系 TG:risk01"
    assert rows[0]["entities"][0]["source_url"] is None
    assert rows[0]["clue_chain"][0]["clue_id"] == "clue-1"
    assert rows[0]["clue_chain"][0]["clue_generation_basis"] == "same_contact_appears_in_at_least_2_sources_within_48h"
    assert rows[0]["clue_chain"][0]["quality_level"] == "high"
    assert rows[0]["review_chain"]["status"] == "linked_to_cross_source_clue"
    assert rows[0]["evidence_completeness"]["has_high_quality_clue"] is True
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
    assert rows[0]["evidence_completeness"]["has_high_quality_clue"] is False
    assert rows[0]["evidence_completeness"]["has_cross_source_clue"] is False


def test_build_acceptance_evidence_pack_distinguishes_single_source_clue_from_cross_source(tmp_path):
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
            }
        ],
        classifications=[],
        entities=[],
        clues=[
            {
                "clue_id": "clue-single-source",
                "clue_type": "shared_tool_multi_record",
                "risk_category": "账号交易",
                "key": "接码",
                "quality_score": 0.82,
                "evidence_trace_ids": ["trace-1"],
                "source_names": ["article-direct"],
            }
        ],
        output_path=output_path,
        report_path=report_path,
    )

    rows = load_jsonl(output_path)

    assert report["review_status_counts"]["linked_to_high_quality_clue"] == 1
    assert "linked_to_cross_source_clue" not in report["review_status_counts"]
    assert rows[0]["review_chain"]["status"] == "linked_to_high_quality_clue"
    assert rows[0]["evidence_completeness"]["has_high_quality_clue"] is True
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
                "image_evidence": [
                    {
                        "image_kind": "poster",
                        "original_image_uri": "s3://bucket/images/trace-full.png",
                        "image_sha256": "abc123",
                        "ocr_text": "OCR extracted risk text",
                        "ocr_engine_provider": "tesseract",
                    }
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
        "image_evidence": [
            {
                "image_kind": "poster",
                "original_image_uri": "s3://bucket/images/trace-full.png",
                "image_sha256": "abc123",
                "ocr_text": "OCR extracted risk text",
                "ocr_engine_provider": "tesseract",
            }
        ],
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


def test_build_acceptance_evidence_pack_reports_source_evidence_counts_by_category(tmp_path):
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"

    hydrated_article_body = "公众号文章 hydrated body " + ("接码风险细节 " * 40)
    rows = [
        {
            "trace_id": "trace-im",
            "source_trace_id": "trace-im",
            "source_name": "telegram-public",
            "source_type": "IM",
            "source_class": "im_or_group",
            "source_url": "https://tg.example/channel/1",
            "content_text": "IM 原文 群控脚本 TG:risk",
            "capture_snapshot_uri": "s3://snapshots/im.html",
            "raw_payload_uri": "s3://payloads/im.json",
        },
        {
            "trace_id": "trace-forum",
            "source_trace_id": "trace-forum",
            "source_name": "tieba-public",
            "source_type": "Forum",
            "acceptance_category": "social_or_forum",
            "source_url": "https://forum.example/thread/1",
            "content_text": "论坛原文 私域导流 TG:risk",
            "raw_payload_uri": "s3://payloads/forum.json",
        },
        {
            "trace_id": "trace-vertical",
            "source_trace_id": "trace-vertical",
            "source_name": "market-public",
            "source_type": "Vertical",
            "acceptance_category": "vertical_or_technical",
            "source_url": "https://market.example/item/1",
            "content_text": "垂直平台原文 账号批发 接码",
            "capture_snapshot_uri": "s3://snapshots/vertical.html",
            "raw_payload_uri": "s3://payloads/vertical.json",
        },
        {
            "trace_id": "trace-article",
            "source_trace_id": "trace-article",
            "source_name": "wechat-article",
            "source_type": "Article",
            "platform": "wechat_public",
            "source_quota_group": "public_account_or_article",
            "source_url": "https://mp.weixin.qq.com/s/article1",
            "content_text": "搜索摘要：接码风险文章",
            "raw_payload_uri": "s3://payloads/article-search.json",
        },
    ]

    report = build_evidence_pack(
        rows,
        classifications=[],
        entities=[],
        cleaned=[],
        clues=[],
        hydrated=[
            {
                "trace_id": "hydrated-article",
                "hydrated_from_trace_id": "trace-article",
                "source_url": "https://mp.weixin.qq.com/s/article1",
                "content_text": hydrated_article_body,
                "capture_snapshot_uri": "s3://snapshots/article.html",
                "raw_payload_uri": "s3://payloads/article-hydrated.json",
            }
        ],
        output_path=output_path,
        report_path=report_path,
    )

    rows = load_jsonl(output_path)

    assert rows[3]["acceptance_category"] == "public_account_or_article"
    assert rows[3]["source_evidence"]["raw_text"] == hydrated_article_body
    assert set(report["source_evidence_counts_by_category"]) == {
        "im_or_group",
        "public_account_or_article",
        "social_or_forum",
        "vertical_or_technical",
    }
    assert report["source_evidence_counts_by_category"]["im_or_group"]["has_raw_text"] == 1
    assert report["source_evidence_counts_by_category"]["social_or_forum"]["has_raw_payload_uri"] == 1
    assert report["source_evidence_counts_by_category"]["vertical_or_technical"]["has_capture_snapshot_uri"] == 1
    assert report["source_evidence_counts_by_category"]["public_account_or_article"]["has_hydrated_body"] == 1
    assert report["source_evidence_counts_by_category"]["public_account_or_article"]["has_capture_snapshot_uri"] == 1


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


def test_build_clue_evidence_index_links_high_quality_clue_to_answer_chain():
    evidence_rows = [
        {
            "trace_id": "trace-1",
            "source_url": "https://forum.example/thread/1",
            "raw_snippet": "原始帖：群控脚本引流，联系 TG:risk01",
            "source_evidence": {
                "capture_snapshot_uri": "s3://snapshots/trace-1.html",
                "raw_payload_uri": "s3://payloads/trace-1.json",
                "source_url": "https://forum.example/thread/1",
                "raw_snippet": "原始帖：群控脚本引流，联系 TG:risk01",
            },
            "clean_text": "群控脚本引流 联系 TG:risk01",
            "classification": {
                "risk_category": "工具交易",
                "confidence": 0.91,
            },
            "entities": [
                {
                    "entity_type": "contact",
                    "normalized_value": "Telegram:risk01",
                    "confidence": 1.0,
                }
            ],
        }
    ]
    clues = [
        {
            "clue_id": "clue-1",
            "clue_type": "shared_contact_48h",
            "risk_category": "工具交易",
            "key": "Telegram:risk01",
            "quality_score": 0.72,
            "quality_level": "medium",
            "threshold_reason": "same_contact_appears_in_at_least_2_sources_within_48h",
            "quality_reasons": ["cross_source_confirmed", "critical_entities_present"],
            "evidence_trace_ids": ["trace-1"],
        },
        {
            "clue_id": "clue-low",
            "clue_type": "shared_keyword",
            "risk_category": "工具交易",
            "key": "low",
            "quality_score": 0.2,
            "quality_level": "low",
            "evidence_trace_ids": ["trace-1"],
        },
    ]

    index = build_clue_evidence_index(evidence_rows, clues=clues)

    assert index["report"]["high_quality_clue_count"] == 1
    assert index["report"]["indexed_clue_count"] == 1
    assert index["rows"][0]["clue_generation_basis"] == "same_contact_appears_in_at_least_2_sources_within_48h"
    assert index["rows"][0]["quality_reasons"] == ["cross_source_confirmed", "critical_entities_present"]
    chain = index["rows"][0]["answer_chain"]
    assert chain[0]["raw_snapshot"]["capture_snapshot_uri"] == "s3://snapshots/trace-1.html"
    assert chain[0]["raw_snapshot"]["raw_payload_uri"] == "s3://payloads/trace-1.json"
    assert chain[0]["clean_text"] == "群控脚本引流 联系 TG:risk01"
    assert chain[0]["classification"]["risk_category"] == "工具交易"
    assert chain[0]["entities"][0]["normalized_value"] == "Telegram:risk01"
    assert index["rows"][0]["clickable_chain_uri"].startswith("evidence-pack://clue/")


def test_build_clue_evidence_index_includes_high_quality_level_without_score_and_reports_missing_trace():
    evidence_rows = [
        {
            "trace_id": "trace-1",
            "source_evidence": {"capture_snapshot_uri": "s3://snapshots/trace-1.html"},
            "clean_text": "清洗文本",
            "classification": {},
            "entities": [],
        }
    ]
    clues = [
        {
            "clue_id": "clue-level-high",
            "clue_type": "shared_contact_48h",
            "risk_category": "账号交易",
            "key": "Telegram:high",
            "quality_level": "HIGH",
            "evidence_trace_ids": ["trace-1", "trace-missing"],
        },
        {
            "clue_id": "clue-low",
            "clue_type": "shared_contact_48h",
            "risk_category": "账号交易",
            "key": "Telegram:low",
            "quality_level": "medium",
            "evidence_trace_ids": ["trace-1"],
        },
    ]
    graph_relations = [
        {
            "relation_id": "rel-1",
            "source_entity": "Telegram:high",
            "target_entity": "trace-1",
            "evidence_trace_ids": ["trace-1"],
        },
        {
            "relation_id": "rel-other",
            "source_entity": "Telegram:other",
            "target_entity": "trace-other",
            "evidence_trace_ids": ["trace-other"],
        },
    ]

    index = build_clue_evidence_index(evidence_rows, clues=clues, graph_relations=graph_relations)

    assert index["report"]["status"] == "completed"
    assert index["report"]["high_quality_clue_count"] == 1
    assert index["report"]["missing_evidence_trace_count"] == 1
    assert index["rows"][0]["clue_id"] == "clue-level-high"
    assert index["rows"][0]["quality_score"] is None
    assert index["rows"][0]["answer_chain"][0]["graph_relations"][0]["relation_id"] == "rel-1"


def test_build_clue_evidence_index_counts_all_missing_high_quality_clue_as_unindexed():
    clues = [
        {
            "clue_id": "clue-missing",
            "clue_type": "shared_contact_48h",
            "risk_category": "工具交易",
            "key": "Telegram:missing",
            "quality_score": 0.8,
            "evidence_trace_ids": ["missing-1", "missing-2"],
        }
    ]

    index = build_clue_evidence_index([], clues=clues)

    assert index["report"]["status"] == "completed"
    assert index["report"]["high_quality_clue_count"] == 1
    assert index["report"]["indexed_clue_count"] == 0
    assert index["report"]["answer_chain_card_count"] == 0
    assert index["report"]["missing_evidence_trace_count"] == 2
    assert "not_fully_indexed" in index["report"]["claim_boundary"]
    assert index["rows"][0]["answer_chain"] == []


def test_build_clue_evidence_index_resolves_display_trace_and_aliases_with_clue_trace_on_card():
    evidence_rows = [
        {
            "trace_id": "display-trace",
            "source_trace_id": "source-trace",
            "hash_id": "hash-trace",
            "source_evidence": {"capture_snapshot_uri": "s3://snapshots/display.html"},
            "clean_text": "display clean text",
            "classification": {},
            "entities": [],
        }
    ]
    clues = [
        {
            "clue_id": "clue-display",
            "quality_score": 0.7,
            "evidence_trace_ids": ["display-trace", "source-trace", "hash-trace"],
        }
    ]

    index = build_clue_evidence_index(evidence_rows, clues=clues)

    assert index["report"]["indexed_clue_count"] == 1
    assert [card["trace_id"] for card in index["rows"][0]["answer_chain"]] == [
        "display-trace",
        "display-trace",
        "display-trace",
    ]
    assert index["rows"][0]["answer_chain"][0].get("matched_evidence_trace_id") is None
    assert index["rows"][0]["answer_chain"][1]["matched_evidence_trace_id"] == "source-trace"
    assert index["rows"][0]["answer_chain"][2]["matched_evidence_trace_id"] == "hash-trace"
    assert [card["clean_text"] for card in index["rows"][0]["answer_chain"]] == [
        "display clean text",
        "display clean text",
        "display clean text",
    ]


def test_build_clue_evidence_index_uri_escapes_unsafe_clue_id():
    index = build_clue_evidence_index(
        [{"trace_id": "trace-1", "source_evidence": {}, "clean_text": ""}],
        clues=[
            {
                "clue_id": "clue /?#% 1",
                "quality_score": 0.7,
                "evidence_trace_ids": ["trace-1"],
            }
        ],
    )

    assert index["rows"][0]["clickable_chain_uri"] == "evidence-pack://clue/clue%20%2F%3F%23%25%201"


def test_build_clue_evidence_index_matches_graph_relations_by_trace_alias_and_overlap():
    evidence_rows = [
        {
            "trace_id": "display-trace",
            "source_trace_id": "source-trace",
            "source_evidence": {},
            "clean_text": "clean",
            "classification": {},
            "entities": [],
        }
    ]
    clues = [
        {
            "clue_id": "clue-rel",
            "quality_score": 0.7,
            "evidence_trace_ids": ["display-trace", "source-trace"],
        }
    ]
    graph_relations = [
        {"relation_id": "rel-source", "source_trace_id": "source-trace"},
        {"relation_id": "rel-multi", "evidence_trace_ids": ["source-trace", "other-trace"]},
    ]

    index = build_clue_evidence_index(evidence_rows, clues=clues, graph_relations=graph_relations)

    relations_by_card = {
        card.get("matched_evidence_trace_id", card["trace_id"]): [relation["relation_id"] for relation in card["graph_relations"]]
        for card in index["rows"][0]["answer_chain"]
    }
    assert relations_by_card["display-trace"] == []
    assert relations_by_card["source-trace"] == ["rel-source", "rel-multi"]


def test_parse_args_defaults_to_no_clue_index_output(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["build_acceptance_evidence_pack.py"])

    args = parse_args()

    assert args.clue_index_output == ""


def test_build_acceptance_evidence_pack_cli_writes_clue_evidence_index_only_when_requested(tmp_path):
    acceptance_path = tmp_path / "acceptance.jsonl"
    clues_path = tmp_path / "clues.jsonl"
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"
    clue_index_path = tmp_path / "clue_index.json"
    default_old_path = tmp_path / "data" / "collection_phase_multi_source_clue_evidence_index.json"

    write_jsonl(
        [
            {
                "trace_id": "trace-1",
                "source_trace_id": "trace-1",
                "source_name": "direct",
                "source_type": "forum",
                "content_text": "原始帖：群控脚本引流，联系 TG:risk01",
                "capture_snapshot_uri": "s3://snapshots/trace-1.html",
            }
        ],
        acceptance_path,
    )
    write_jsonl(
        [
            {
                "clue_id": "clue-1",
                "clue_type": "shared_contact_48h",
                "risk_category": "工具交易",
                "key": "Telegram:risk01",
                "quality_score": 0.7,
                "evidence_trace_ids": ["trace-1"],
            }
        ],
        clues_path,
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
            "--clues",
            str(clues_path),
            "--output",
            str(output_path),
            "--report-out",
            str(report_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not default_old_path.exists()
    assert not clue_index_path.exists()

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
            "--clues",
            str(clues_path),
            "--output",
            str(output_path),
            "--report-out",
            str(report_path),
            "--clue-index-output",
            str(clue_index_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    index = json.loads(clue_index_path.read_text(encoding="utf-8"))

    assert index["report"]["indexed_clue_count"] == 1
    assert index["rows"][0]["answer_chain"][0]["raw_snapshot"]["capture_snapshot_uri"] == "s3://snapshots/trace-1.html"


def test_build_acceptance_evidence_pack_cli_binds_graph_relations_when_requested(tmp_path):
    acceptance_path = tmp_path / "acceptance.jsonl"
    clues_path = tmp_path / "clues.jsonl"
    graph_relations_path = tmp_path / "graph_relations.jsonl"
    output_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.json"
    clue_index_path = tmp_path / "clue_index.json"

    write_jsonl(
        [
            {
                "trace_id": "trace-graph",
                "source_trace_id": "trace-graph",
                "source_name": "direct",
                "source_type": "forum",
                "content_text": "群控脚本引流 联系 TG:risk01",
            }
        ],
        acceptance_path,
    )
    write_jsonl(
        [
            {
                "clue_id": "clue-graph",
                "clue_type": "shared_contact_48h",
                "risk_category": "工具交易",
                "key": "Telegram:risk01",
                "quality_score": 0.7,
                "evidence_trace_ids": ["trace-graph"],
            }
        ],
        clues_path,
    )
    write_jsonl(
        [
            {
                "relation_id": "rel-graph",
                "source_entity": "Telegram:risk01",
                "target_entity": "群控脚本",
                "evidence_trace_ids": ["trace-graph", "trace-other"],
            }
        ],
        graph_relations_path,
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
            "--clues",
            str(clues_path),
            "--graph-relations",
            str(graph_relations_path),
            "--output",
            str(output_path),
            "--report-out",
            str(report_path),
            "--clue-index-output",
            str(clue_index_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    index = json.loads(clue_index_path.read_text(encoding="utf-8"))

    assert index["rows"][0]["answer_chain"][0]["graph_relations"][0]["relation_id"] == "rel-graph"
