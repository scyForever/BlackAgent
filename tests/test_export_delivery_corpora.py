from __future__ import annotations

from scripts.export_delivery_corpora import _annotate_row, _query_stage_lookup
from src.enhancement.source_intake import MultimodalTextExtractor


def test_export_delivery_corpora_can_infer_variant_stage_and_special_signals():
    annotated = _annotate_row(
        {
            "trace_id": "export-1",
            "source_name": "variant-source",
            "query_theme": "诈骗引流",
            "query_term": "加薇",
            "content_text": "截图文案：➕V 后拉裙，继续引流。",
            "images": [{"ocr_text": "海报写着 联系 TG:plane007"}],
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
