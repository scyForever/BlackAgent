from __future__ import annotations

from scripts.export_delivery_corpora import _annotate_row, _query_stage_lookup, build_quota_sample
from src.enhancement.source_intake import MultimodalTextExtractor


def test_export_delivery_corpora_can_infer_variant_stage_and_special_signals():
    annotated = _annotate_row(
        {
            "trace_id": "export-1",
            "source_name": "variant-source",
            "query_theme": "诈骗引流",
            "query_term": "加薇",
            "content_text": "截图文案：➕V 后拉裙，继续引流。",
            "images": [{"image_url": "https://img.example/poster.png", "ocr_text": "海报写着 联系 TG:plane007"}],
            "screenshot_ref": "screenshots/poster-1.png",
        },
        stage_lookup=_query_stage_lookup(),
        multimodal_extractor=MultimodalTextExtractor(),
    )

    assert annotated["query_term_stage"] == "variant"
    assert annotated["query_term_stage_inferred"] is True
    assert "variant_or_homophone_normalized" in annotated["special_signal_types"]
    assert "emoji_marker" in annotated["special_signal_types"]
    assert "multimodal_text" in annotated["special_signal_types"]
    assert annotated["multimodal_signal_count"] >= 1
    assert "images.ocr_text" in annotated["multimodal_text_sources"]
    assert "images.image_url" in annotated["multimodal_reference_fields"]
    assert "screenshot_ref" in annotated["multimodal_reference_fields"]
    assert annotated["content_hash"]
    assert annotated["source_snapshot_id"].startswith("variant-source:")
    assert annotated["source_access_type"] == "manual_upload"
    assert annotated["collection_quality"]["quality_version"] == "collection_quality_v1"


def test_quota_sample_caps_single_source_and_reports_underfilled_classes():
    rows = [
        {"trace_id": f"im-{idx}", "source_name": "telegram_public_delivery:big", "source_type": "IM", "content_text": "接码"}
        for idx in range(10)
    ] + [
        {"trace_id": "forum-1", "source_name": "tieba", "source_type": "Forum", "content_text": "接码"},
        {"trace_id": "vertical-1", "source_name": "market", "source_type": "Vertical", "content_text": "接码"},
    ]

    sample = build_quota_sample(rows)

    assert sample["selected_count"] == 5
    assert any(item["source_name"] == "telegram_public_delivery:big" and item["count"] == 3 for item in sample["source_counts"])
    assert any("social_or_forum_quota_underfilled" in warning for warning in sample["warnings"])
    assert any("vertical_or_technical_quota_underfilled" in warning for warning in sample["warnings"])
