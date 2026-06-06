from scripts.build_heldout_eval import build_heldout_records
from scripts.build_ocr_hardset import build_records as build_ocr_hardset_records
from scripts.export_manual_heldout_review import export_rows as export_manual_heldout_rows
from scripts.generate_ops_dashboard import build_dashboard
from scripts.generate_source_smoke_report import build_report, load_sources
from scripts.run_cross_source_graph_demo import run_demo as run_cross_source_graph_demo
from scripts.run_live_source_smoke import run_smoke as run_live_source_smoke
from scripts.run_scale_benchmark import run_benchmark as run_scale_benchmark
from scripts.validate_manual_heldout import merge_review_csv, validate_records
from scripts.serve_demo_api import run_demo_request
from src.agent.user_request_parser import _fallback_intent
from src.collector.source_config import load_source_catalog
from src.conversation import ConversationMemoryStore, ConversationResolver, FollowupParser
from src.enhancement.source_intake import MultimodalTextExtractor
from src.enhancement.strategy import EvidenceChainRenderer, RiskClue
from src.enhancement.text_intelligence import FineGrainedIntentClassifier
from src.ml import LocalBertAdapter, LocalBertConfig
from src.ocr import BitmapGlyphOCREngine, OCRImageTextAdapter, render_demo_pbm
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


def test_bitmap_ocr_engine_reads_demo_image_pixels(tmp_path):
    image_path = render_demo_pbm("TG:OCR001", tmp_path / "ocr_demo.pbm")

    ocr = OCRImageTextAdapter(engine=BitmapGlyphOCREngine()).extract({"trace_id": "ocr-pixel", "image_path": str(image_path)})

    assert ocr.status == "completed"
    assert ocr.content_modality == "image_text"
    assert ocr.text == "TG:OCR001"
    assert "ocr_engine.image_path" in ocr.sources


def test_ocr_adapter_records_named_engine_comparison_outputs(tmp_path):
    image_path = render_demo_pbm("TG:OCR002", tmp_path / "ocr_demo_compare.pbm")

    ocr = OCRImageTextAdapter(
        engines={
            "bitmap_demo": BitmapGlyphOCREngine(),
            "cloud_ocr_fixture": lambda path: "TG:OCR002 群控脚本",
        }
    ).extract({"trace_id": "ocr-compare", "image_path": str(image_path)})

    assert ocr.status == "completed"
    assert ocr.engine_outputs["bitmap_demo"] == "TG:OCR002"
    assert ocr.engine_outputs["cloud_ocr_fixture"] == "TG:OCR002 群控脚本"
    assert "ocr_engine.bitmap_demo" in ocr.sources
    assert "ocr_engine.cloud_ocr_fixture" in ocr.sources


def test_local_bert_adapter_provides_no_dependency_prestage_contract():
    result = LocalBertAdapter(config=LocalBertConfig(enabled=False)).analyze("群控脚本接码上车，联系 TG:bert001")

    assert result.status == "deterministic_fallback"
    assert result.risk_category == "工具交易"
    assert any(entity["entity_type"] == "contact" and entity["normalized_value"] == "bert001" for entity in result.entities)


def test_review_load_calibration_auto_clears_high_confidence_crowd_secondary():
    result = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "crowd-auto-clear",
            "content_text": "承接TG私信代发广告投放，支持客户包量，业务联系 @demo，长期合作担保。",
            "matched_themes": ["众包任务"],
            "matched_keywords": ["代发", "接单", "广告"],
        }
    )

    assert result.risk_category == "众包服务"
    assert result.secondary_label == "代投服务"
    assert result.review_required is False


def test_review_only_secondary_stays_in_manual_review():
    result = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "review-only-order",
            "content_text": "支付通道调整后出现卡单和支付失败，下单用户请联系客服处理退款和补发。",
            "matched_themes": ["刷单作弊"],
            "matched_keywords": ["卡单"],
        }
    )

    assert result.secondary_label == "订单卡单"
    assert result.review_required is True


def test_conflict_review_calibration_resolves_only_high_evidence_cases():
    resolved = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "tool-conflict-calibrated",
            "content_text": "群控脚本 云控后台 软件功能下载 群发 拉群 拉人 批量注册 联系 @demo",
            "matched_themes": ["众包任务", "工具交易"],
            "matched_keywords": ["群发", "拉人", "批量注册", "软件", "后台"],
        }
    )
    unresolved = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "tool-conflict-weak",
            "content_text": (
                "注意同一个号码包内的协议号和直登号不可以同时运行,否则会引发异常。"
                "如要多端运行,需要用直登号接码建立新会话后才可以。"
                "有协议号软件带接码功能的也可以用协议号接码建立新会话。"
                "自助取号 @alangtg_bot 人工客服 @alangcn"
            ),
            "matched_themes": ["工具交易", "接码"],
            "matched_keywords": ["协议号", "接码"],
        }
    )

    assert resolved.secondary_label == "群控脚本"
    assert resolved.conflict_status == "RESOLVED"
    assert resolved.review_required is False
    assert unresolved.review_required is True
    assert unresolved.conflict_status == "CONFLICT_REVIEW"


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
    assert all("authorization_statement" in row for row in report["sources"])


def test_acceptance_catalog_contains_non_telegram_sources_for_live_e2e_quality():
    sources = load_source_catalog("config/intel_sources.acceptance_telegramnav_live.yaml")
    source_types = {str(source.get("source_type") or "") for source in sources}
    source_names = {str(source.get("source_name") or "") for source in sources}

    assert {"IM", "Forum", "Vertical"} <= source_types
    assert "acceptance_tieba_public_search" in source_names
    assert "acceptance_crowdsourcing_public_search" in source_names


def test_live_source_smoke_attempts_until_each_class_has_min_records(monkeypatch):
    calls = []

    def fake_collect(source, *, max_records=5, timeout_seconds=10.0):
        calls.append(source["source_name"])
        return {
            "collected_count": 1,
            "filtered_count": 0,
            "duplicate_rate": 0.0,
            "high_risk_candidate_count": 0,
            "failure_reason": None,
            "live_smoke_attempted": True,
        }

    monkeypatch.setattr("scripts.generate_source_smoke_report._collect_live_metrics", fake_collect)

    report = build_report(load_sources("config/intel_sources.public.yaml"), network_enabled=True)

    assert report["status"] == "completed"
    assert set(report["live_attempted_source_classes"]) == {"im_or_group", "social_or_forum", "vertical_or_technical"}
    assert len(calls) >= 3
    assert "telegram_group_public_timeline" in calls
    assert all(
        item["target_met"] or item["collected_count"] == item["configured_source_count"]
        for item in report["per_class_evidence"]
    )
    assert all(row["live_smoke_attempted"] for row in report["sources"])


def test_authorized_live_source_smoke_collects_loopback_feed():
    report = run_live_source_smoke()

    assert report["status"] == "completed"
    assert report["run_type"] == "live_authorized_loopback_collection_smoke"
    assert report["smoke_scope"] == "three_required_source_classes"
    assert report["authorization_enforced"] is True
    assert set(report["covered_source_classes"]) == {"im_or_group", "social_or_forum", "vertical_or_technical"}
    assert all(key in report["sources"][0] for key in ["collected_count", "filtered_count", "duplicate_rate", "high_risk_candidate_count", "failure_reason"])
    assert report["fetched_count"] >= 5
    assert report["high_risk_candidate_count"] >= 3


def test_one_click_demo_api_runs_without_external_network():
    report = run_demo_request({"query": "复核 demo 中的群控接码线索"})

    assert report["status"] == "completed"
    assert report["run_type"] == "local_one_click_defense_demo"
    assert report["input_count"] >= 1
    assert report["execution_summary"]


def test_heldout_builder_creates_reviewable_local_split():
    records = [
        {"trace_id": "h1", "source_name": "tg", "source_type": "IM", "content_text": "群控脚本接码，联系 TG:h001"},
        {"trace_id": "h2", "source_name": "forum", "source_type": "Forum", "content_text": "私域导流返利拉新，开户链接 https://h2.example/a"},
        {"trace_id": "h3", "source_name": "feed", "source_type": "THREAT_INTEL", "content_text": "代发广告投放业务，客户包量，联系 @h3"},
    ]

    heldout = build_heldout_records(records, limit=3, per_category=2)

    assert heldout
    assert all(item["dataset_kind"] == "heldout_public_authorized_seed" for item in heldout)
    assert all(item["annotation_source"] == "seeded_from_local_authorized_corpus_for_manual_review" for item in heldout)
    assert all((item.get("human_review") or {}).get("status") == "pending_human_confirmation" for item in heldout)


def test_manual_heldout_validator_emits_only_human_confirmed_rows():
    records = [
        {
            "trace_id": "m1",
            "content_text": "群控脚本 TG:m1",
            "expected_risk_categories": ["工具交易"],
            "human_review": {
                "status": "confirmed",
                "annotator": "analyst-a",
                "review_date": "2026-06-03",
                "final_risk_categories": ["工具交易"],
                "final_secondary_labels": ["群控脚本"],
                "conflict_handling": "rule_label_confirmed",
                "typical_error": "none",
            },
        },
        {"trace_id": "m2", "content_text": "待复核", "human_review": {"status": "pending_human_confirmation"}},
    ]

    confirmed, report = validate_records(records, min_records=1)

    assert report["status"] == "completed"
    assert len(confirmed) == 1
    assert confirmed[0]["annotation_source"] == "human_confirmed"
    assert confirmed[0]["dataset_kind"] == "manual_heldout_public_authorized"
    assert report["manual_gold_claim"]["can_claim_manual_gold"] is True


def test_manual_heldout_review_export_and_csv_merge_roundtrip():
    seed_records = [
        {
            "trace_id": "m1",
            "source_trace_id": "m1",
            "source_name": "tg",
            "source_type": "IM",
            "content_text": "群控脚本 TG:m1",
            "expected_risk_categories": ["工具交易"],
            "expected_secondary_labels": ["群控脚本"],
            "expected_entities": [{"entity_type": "contact", "normalized_value": "m1"}],
            "human_review": {"status": "pending_human_confirmation"},
        }
    ]

    rows = export_manual_heldout_rows(seed_records, limit=1)
    merged = merge_review_csv(
        seed_records,
        [
            {
                "source_trace_id": "m1",
                "status": "corrected",
                "annotator": "analyst-a",
                "review_date": "2026-06-03",
                "final_risk_categories": "账号交易",
                "final_secondary_labels": "接码注册",
                "conflict_handling": "secondary_corrected",
                "typical_error": "secondary_confusion",
            }
        ],
    )
    confirmed, report = validate_records(merged, min_records=1)

    assert rows[0]["status"] == "pending_human_confirmation"
    assert rows[0]["seed_expected_risk_categories"] == "工具交易"
    assert report["status"] == "completed"
    assert report["manual_gold_claim"]["claim_status"] == "human_confirmed_gold_ready"
    assert confirmed[0]["expected_risk_categories"] == ["账号交易"]
    assert confirmed[0]["expected_secondary_labels"] == ["接码注册"]


def test_manual_heldout_validator_keeps_pending_review_package_out_of_gold_claims():
    confirmed, report = validate_records(
        [{"trace_id": "m-pending", "human_review": {"status": "pending_human_confirmation"}}],
        min_records=1,
    )

    assert confirmed == []
    assert report["status"] == "insufficient_confirmed_records"
    assert report["confirmed_record_gap"] == 1
    assert report["manual_gold_claim"]["can_claim_manual_gold"] is False
    assert report["manual_gold_claim"]["claim_status"] == "review_package_only"


def test_ocr_hardset_builder_creates_labeled_image_text_rows(tmp_path):
    records = build_ocr_hardset_records(count=20, image_dir=tmp_path / "ocr_images")

    assert len(records) == 20
    assert all(record["content_modality"] == "image_text" for record in records)
    assert all(record["ocr_status"] == "completed" for record in records)
    assert all({"contact", "links", "slang", "tool_names"} <= set(record["manual_labels"]) for record in records)
    entity_types = {
        entity["entity_type"]
        for record in records
        for entity in record["expected_entities"]
    }
    assert {"contact", "tool_name", "invite_code"} <= entity_types
    assert any(entity["entity_type"] == "url" for record in records for entity in record["expected_entities"])


def test_cross_source_graph_demo_outputs_multi_source_clue():
    report = run_cross_source_graph_demo()

    assert report["status"] == "completed"
    assert report["source_count"] == 3
    assert report["cross_source_clue_count"] >= 1
    assert any(clue["clue_type"] == "shared_contact_48h" for clue in report["cross_source_clues"])


def test_ops_dashboard_aggregates_monitoring_metrics():
    dashboard = build_dashboard(
        collection_stats={"total_raw_records": 10, "source_counts": [{"source_name": "a", "count": 10}]},
        classification_summary={"classification_count": 4, "review_required_count": 2},
        source_smoke={
            "sources": [
                {"source_name": "ok", "compliance_status": "SCHEDULABLE"},
                {"source_name": "bad", "compliance_status": "REJECTED", "failure_reason": "blocked"},
            ],
            "covered_source_classes": ["im_or_group"],
            "missing_source_classes": ["social_or_forum"],
        },
        scale_report={"scenarios": [{"sample_size": 100, "records_per_second": 20.0, "p95_record_latency_ms": 1.2, "llm_calls_per_1000_records": 0.0, "estimated_tokens_per_1000_records": 0.0}]},
        llm_value={"record_enrich_policy": "conflict_only", "should_enable_record_enrich": False},
        review_records=[{"trace_id": "r1", "content_text": "代发广告投放业务，客户包量，联系 @demo"}],
    )

    assert dashboard["status"] == "completed"
    assert dashboard["source_quality"]["failure_rate"] == 0.5
    assert dashboard["classification_review_load"]["baseline"]["review_rate"] == 0.5
    assert dashboard["llm_cost_and_value"]["record_enrich_policy"] == "conflict_only"


def test_scale_benchmark_reports_latency_and_token_budget_on_small_sample():
    report = run_scale_benchmark(sample_sizes=[20], batch_size=10, profile="fast")

    scenario = report["scenarios"][0]
    assert report["status"] == "completed"
    assert scenario["sample_size"] == 20
    assert scenario["classified_count"] == 20
    assert scenario["records_per_second"] > 0
    assert "estimated_tokens_per_1000_records" in scenario
