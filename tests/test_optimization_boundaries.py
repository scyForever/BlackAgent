from scripts.generate_source_smoke_report import build_report, load_sources
from src.agent.user_request_parser import _fallback_intent
from src.conversation import ConversationMemoryStore, ConversationResolver, FollowupParser
from src.enhancement.source_intake import MultimodalTextExtractor
from src.enhancement.strategy import EvidenceChainRenderer, RiskClue
from src.ml import LocalBertAdapter, LocalBertConfig
from src.ocr import OCRImageTextAdapter
from src.query import PreflightQueryParser


def test_preflight_query_parser_extracts_assets_before_llm_parse():
    intent = PreflightQueryParser().parse("近48小时只看 Telegram 群控脚本接码跨源线索")

    assert "工具交易" in intent.risk_types
    assert "账号交易" in intent.risk_types
    assert "telegram" in intent.preferred_source_types
    assert intent.need_cross_source is True
    assert intent.time_range_hours == 48
    assert intent.needs_llm_parse is True


def test_fallback_intent_uses_preflight_parser_contract():
    intent = _fallback_intent("找近24小时 TG 群控脚本和接码线索")

    assert "工具交易" in intent.risk_types
    assert "接码" in intent.risk_types
    assert "telegram" in intent.source_preferences
    assert "群控" in intent.include_keywords


def test_conversation_followup_parser_resolves_common_turns():
    store = ConversationMemoryStore()
    session = store.create(active_clue_ids=["clue-a", "clue-b"], active_entities=["TG:core01"])
    parser = FollowupParser()
    resolver = ConversationResolver()

    expand = parser.parse("展开第 2 条线索")
    explain = parser.parse("解释为什么判成工具交易")
    track = parser.parse("继续查这个 TG:core01")
    rerun = parser.parse("只看 Telegram 来源再跑一次 high_recall")
    report = parser.parse("基于当前线索生成报告")

    assert resolver.resolve(session, expand)["clue_id"] == "clue-b"
    assert explain.intent_type == "explain_clue"
    assert track.intent_type == "track_entity"
    assert rerun.needs_rerun is True and rerun.source_filter == "telegram"
    assert report.needs_report is True


def test_ocr_adapter_marks_image_text_modality_without_external_engine():
    record = {
        "trace_id": "ocr-1",
        "attachments": [{"ocr_text": "海报OCR：群控脚本 接码 TG:ocr001"}],
    }

    materialized = MultimodalTextExtractor().materialize(record)
    ocr = OCRImageTextAdapter().extract(record)

    assert materialized["content_modality"] == "image_text"
    assert ocr.content_modality == "image_text"
    assert "TG:ocr001" in ocr.text


def test_local_bert_adapter_provides_no_dependency_prestage_contract():
    result = LocalBertAdapter(config=LocalBertConfig(enabled=False)).analyze("群控脚本接码上车，联系 TG:bert001")

    assert result.status == "deterministic_fallback"
    assert result.risk_category == "工具交易"
    assert any(entity["entity_type"] == "contact" and entity["normalized_value"] == "bert001" for entity in result.entities)


def test_evidence_chain_renderer_outputs_reviewable_rows():
    clue = RiskClue(
        clue_id="clue-1",
        clue_type="shared_contact_48h",
        key="TG:core01",
        risk_category="工具交易",
        evidence_trace_ids=["r1"],
        source_names=["tg-a"],
        entity_values=["TG:core01"],
        confidence=0.9,
        threshold_reason="same_contact",
    )

    rows = EvidenceChainRenderer().render(
        [clue],
        [{"trace_id": "r1", "source_name": "tg-a", "source_type": "IM", "content_text": "群控脚本 TG:core01"}],
        entities=[{"source_trace_id": "r1", "entity_value": "TG:core01", "normalized_value": "tg:core01"}],
    )

    assert rows[0].source_name == "tg-a"
    assert rows[0].related_entities == ["TG:core01"]
    assert rows[0].extracted_entities == ["tg:core01"]


def test_source_smoke_report_covers_three_required_source_classes():
    report = build_report(load_sources("config/intel_sources.public.yaml"), network_enabled=False)

    assert report["status"] == "completed"
    assert set(report["covered_source_classes"]) == {"im_or_group", "social_or_forum", "vertical_or_technical"}
    assert all("legal_basis" in row and row["run_type"] == "dry_run_catalog_smoke" for row in report["sources"])
