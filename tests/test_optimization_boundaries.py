from scripts.build_heldout_eval import build_heldout_records
from scripts.build_ocr_hardset import build_records as build_ocr_hardset_records
from argparse import Namespace
from collections import Counter

from scripts.collect_public_sources import (
    DEFAULT_SOURCE_MIN_QUOTAS,
    balanced_source_slice,
    selected_sources_from_args,
    source_minimum_quotas_from_args,
)
from scripts.export_acceptance_e2e_evidence import build_evidence
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
from src.collector.source_config import quota_balanced_source_slice
from src.collector.source_metadata import source_class_for_record, source_quota_groups_for_record
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


def test_ocr_adapter_preserves_nested_upstream_confidence_metadata():
    record = {
        "trace_id": "ocr-confidence",
        "attachments": [
            {
                "ocr_text": "海报OCR：接码注册 TG:ocrconf",
                "ocr_confidence": 0.83,
            }
        ],
    }

    materialized = MultimodalTextExtractor().materialize(record)
    ocr = OCRImageTextAdapter().extract(record)
    materialized_record = OCRImageTextAdapter().materialize_record(record)

    expected_details = [
        {
            "source": "attachments.ocr_text",
            "field": "attachments.ocr_confidence",
            "confidence": 0.83,
        }
    ]
    assert materialized["content_modality"] == "image_text"
    assert materialized["ocr_confidence"] == 0.83
    assert materialized["ocr_confidence_details"] == expected_details
    assert ocr.model_dump()["ocr_confidence"] == 0.83
    assert ocr.model_dump()["ocr_confidence_details"]["upstream"] == expected_details
    assert materialized_record["ocr_confidence"] == 0.83
    assert materialized_record["ocr_confidence_details"]["upstream"] == expected_details


def test_ocr_adapter_materializes_explicit_ocr_text_field():
    record = {
        "trace_id": "ocr-text-contract",
        "attachments": [{"ocr_text": "海报OCR：群控脚本 接码 TG:ocrtext"}],
    }

    materialized_record = OCRImageTextAdapter().materialize_record(record)

    assert materialized_record["ocr_text"] == materialized_record["content_text"]
    assert "TG:ocrtext" in materialized_record["ocr_text"]


def test_caption_confidence_does_not_become_ocr_confidence():
    record = {
        "trace_id": "caption-confidence",
        "content_text": "正文",
        "attachments": [
            {
                "caption": "图片说明文字，不是 OCR",
                "confidence": 0.91,
            }
        ],
    }

    materialized = MultimodalTextExtractor().materialize(record)

    assert materialized["content_modality"] == "mixed"
    assert materialized["ocr_confidence"] is None
    assert materialized["ocr_confidence_details"] == []


def test_bitmap_ocr_engine_reads_demo_image_pixels(tmp_path):
    image_path = render_demo_pbm("TG:OCR001", tmp_path / "ocr_demo.pbm")

    ocr = OCRImageTextAdapter(engine=BitmapGlyphOCREngine()).extract({"trace_id": "ocr-pixel", "image_path": str(image_path)})

    assert ocr.status == "completed"
    assert ocr.content_modality == "image_text"
    assert ocr.text == "TG:OCR001"
    assert "ocr_engine.image_path" in ocr.sources


def test_ocr_adapter_records_structured_engine_confidence(tmp_path):
    image_path = tmp_path / "structured_engine.png"

    def structured_engine(path):
        return {"text": "TG:OCR777 群控脚本", "confidence": "91.5"}

    adapter = OCRImageTextAdapter(engines={"structured_fixture": structured_engine})

    ocr = adapter.extract({"trace_id": "ocr-engine-confidence", "image_path": str(image_path)})
    materialized = adapter.materialize_record({"trace_id": "ocr-engine-confidence", "image_path": str(image_path)})

    assert ocr.status == "completed"
    assert ocr.text == "TG:OCR777 群控脚本"
    assert ocr.model_dump()["ocr_confidence"] == 0.915
    assert ocr.model_dump()["ocr_engine_confidences"] == {"structured_fixture": 0.915}
    assert ocr.model_dump()["ocr_confidence_details"]["engines"]["structured_fixture"] == {
        "source": "ocr_engine.structured_fixture",
        "field": "confidence",
        "confidence": 0.915,
    }
    assert materialized["ocr_confidence"] == 0.915
    assert materialized["ocr_engine_confidences"] == {"structured_fixture": 0.915}


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


def test_anti_fraud_warning_with_brushing_terms_is_low_relevance():
    result = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "anti-fraud-brushing-warning",
            "source_name": "public_security_news",
            "source_type": "Article",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "反诈提醒：刷单返佣都是诈骗，切勿垫付，不要相信日结兼职广告。",
            "matched_keywords": ["刷单", "返佣", "垫付"],
            "matched_themes": ["刷单作弊"],
        }
    )

    assert result.risk_category == "正常业务白噪声"
    assert result.review_bucket == "low_relevance"
    assert result.review_required is False
    assert result.conflict_status == "NEGATIVE_RISK_ASSERTION"


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


def test_manual_heldout_typical_errors_handle_tutorial_rebate_and_account_tool_conflicts():
    classifier = FineGrainedIntentClassifier()

    ordinary_mod_discussion = classifier.classify(
        {
            "trace_id": "typical-error-tutorial",
            "source_name": "tieba_blackgray_search",
            "source_type": "Forum",
            "content_text": (
                "CK3 steam版MOD教程：如何开启自动招募宫廷职位功能，"
                "下载 Court Position Automation 后在创意工坊订阅说明里配置。"
            ),
        }
    )
    rebate_traffic = classifier.classify(
        {
            "trace_id": "typical-error-rebate",
            "source_name": "x_blackgray_search",
            "source_type": "Social",
            "content_text": (
                "交易所高返佣账号开户链接，支持 API 高返佣和拉新，"
                "联系 TG:rebate001 领取节点。"
            ),
        }
    )
    account_tool_hybrid = classifier.classify(
        {
            "trace_id": "typical-error-account-tool",
            "source_name": "telegram_public_delivery:chaojiyun88",
            "source_type": "IM",
            "content_text": (
                "用户端注册账号，免费赠送测试卡密，接码平台电脑端客服 @kefututu88，"
                "网址 https://2222sim.example.com"
            ),
        }
    )

    assert ordinary_mod_discussion.risk_category == "正常业务白噪声"
    assert ordinary_mod_discussion.secondary_label == "低相关"
    assert ordinary_mod_discussion.review_required is False

    assert rebate_traffic.risk_category == "诈骗引流"
    assert rebate_traffic.secondary_label == "返利引流"
    assert "刷单作弊" not in rebate_traffic.conflict_categories

    assert account_tool_hybrid.review_required is True
    assert account_tool_hybrid.conflict_status == "CONFLICT_REVIEW"
    assert set(account_tool_hybrid.conflict_categories) >= {"账号交易", "工具交易"} - {account_tool_hybrid.risk_category}
    assert {"卡密交易", "接码注册"} <= {item["label"] for item in account_tool_hybrid.candidate_secondary_labels}


def test_manual_heldout_health_private_domain_public_ad_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-health-private-domain-ad",
            "source_name": "health_saas_public_article",
            "source_type": "Vertical",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "健康管理机构私域运营白皮书广告：慢病随访、会员复购、"
                "企微客户管理和公开投放案例介绍，面向诊所和营养师的普通SaaS资料。"
            ),
            "matched_keywords": ["私域", "运营"],
            "matched_themes": ["诈骗引流", "众包任务"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_health_private_domain_variant_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-health-private-domain-variant",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "大健康项目招前段详情详谈【私域流量社群运营吧】。我们有全牌照权威资质、"
                "顶尖科研技术平台、完整服务产品体系和专业医疗交付能力。"
            ),
            "matched_keywords": ["私域", "运营"],
            "matched_themes": ["诈骗引流", "众包任务"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_public_addv_discussion_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-public-addv-discussion",
            "source_name": "x_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "网友讨论：现在很多帖子动不动就写加v，大家只是在吐槽公共社交礼仪，"
                "没有给出微信号、群链接、开户链接或任何接单交易信息。"
            ),
            "matched_keywords": ["加v"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_private_domain_monetization_prefers_fraud_traffic_over_crowd_ops():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-private-domain-monetization",
            "source_name": "x_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "收益拆解：我代运营的真实逻辑。私域转化是精准用户导流，"
                "后端卖课程、工具、服务，代运营分成是帮别人跑矩阵。"
            ),
            "matched_keywords": ["私域", "导流", "代运营", "矩阵"],
            "matched_themes": ["诈骗引流", "众包任务"],
        }
    )

    assert classification.risk_category == "诈骗引流"
    assert classification.secondary_label == "私域导流"
    assert "众包服务" not in classification.conflict_categories


def test_manual_heldout_public_private_domain_book_discussion_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-private-domain-book-discussion",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "马化腾的私域赚钱思维书籍读后讨论，普通人能学会吗？"
                "吧友分享阅读方法和收获，没有微信号、开户链接或接单交易。"
            ),
            "matched_keywords": ["私域"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_exchange_rebate_without_task_terms_routes_to_rebate_traffic():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-exchange-rebate-row",
            "source_name": "x_blackgray_search",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "散户在币圈想快速赚钱，最简单的方法就是找一家返佣高的交易所合作，"
                "然后疯狂拉人头吃返佣。普通人一天拉几个人，遇到交易量大的大户每天返上千U。"
            ),
            "matched_keywords": ["返佣", "拉人"],
            "matched_themes": ["刷单作弊", "众包任务"],
        }
    )

    assert classification.risk_category == "诈骗引流"
    assert classification.secondary_label == "返利引流"
    assert "刷单作弊" not in classification.conflict_categories


def test_manual_heldout_sms_platform_api_article_keeps_account_sms_secondary():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-sms-platform-api-article",
            "source_name": "seeding_note_blackgray_search",
            "source_type": "Social",
            "source_url": "https://post.smzdm.com/p/aomp49wn/",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "在线接收国外验证码的虚拟号码服务平台，支持API二次开发。"
                "HeroSMS作为SMS-Active的升级替代平台，不仅能接收国外手机验证码，"
                "还支持API二次开发，为普通用户和开发者提供稳定可靠的新选择。"
            ),
            "matched_keywords": ["验证码", "虚拟号码"],
            "matched_themes": ["接码"],
        }
    )

    assert classification.risk_category == "账号交易"
    assert classification.secondary_label == "接码注册"


def test_manual_heldout_consumer_verification_autofill_article_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-consumer-verification-autofill",
            "source_name": "seeding_note_blackgray_search",
            "source_type": "Social",
            "source_url": "https://post.smzdm.com/p/apw3mro9/",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "手机验证码不用手输! 一键开启自动填充，再也不怕输错还能防偷窥。"
                "手机软件文章介绍登录和支付时的验证码自动填充功能，提升账号安全。"
            ),
            "matched_keywords": ["验证码", "软件"],
            "matched_themes": ["接码", "工具交易"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_consumer_sms_verification_complaint_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-consumer-sms-verification-complaint",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "云闪付切换设备需付费短信验证，哪些软件也如此？"
                "用户吐槽登录还要发短信验证码，讨论安全验证机制和短信费用。"
            ),
            "matched_keywords": ["验证码", "软件"],
            "matched_themes": ["接码", "工具交易"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_tool_group_send_procedure_is_not_defensive_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-tool-group-send-procedure",
            "source_name": "telegram_public_delivery:tgliuxing",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "步骤3: 群组群发介绍说明 1: Sessions 目录中放广告文案和自动回复。"
                "如果账号运行过程中有人咨询,这个账号要自动回复。"
                "群组群发会记录生成 Group.txt, 程序设定默认每20秒左右发送1个群, "
                "目录下所有session账号会同时启动发送。"
            ),
            "matched_keywords": ["群发"],
            "matched_themes": ["众包任务"],
        }
    )

    assert classification.risk_category == "工具交易"
    assert classification.secondary_label == "群控脚本"
    assert classification.review_decision_reason != "defensive_or_negative_context"


def test_manual_heldout_public_addv_threshold_discussion_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-public-addv-threshold",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "现在加v门槛有多高? 一百多加v还下头? 平平淡淡既为真，恋人未满，来自 Android 客户端。",
            "matched_keywords": ["加v", "加微"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_sms_platform_second_use_gets_sms_secondary():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-sms-platform-second-use",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "接码平台二次使用显示不在线解决办法。我用飞鱼接的码，二次想用的时候显示指定专属手机码不在线，有偿求解。",
            "matched_keywords": ["接码"],
            "matched_themes": ["接码"],
        }
    )

    assert classification.risk_category == "账号交易"
    assert classification.secondary_label == "接码注册"


def test_manual_heldout_websea_rebate_account_routes_to_rebate_traffic():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-websea-rebate",
            "source_name": "x_blackgray_search",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Websea合约高返佣账号今日推出活期理财，保本跟单特权码跟不了的资金别闲着，"
                "高返佣账号支持拉新返利和合约交易量返点。"
            ),
            "matched_keywords": ["返佣"],
            "matched_themes": ["刷单作弊"],
        }
    )

    assert classification.risk_category == "诈骗引流"
    assert classification.secondary_label == "返利引流"
    assert "刷单作弊" not in classification.conflict_categories


def test_manual_heldout_group_sender_download_is_tool_not_crowd_conflict():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-haodi-group-sender",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "欢迎大家使用豪迪群发器_豪迪群发器吧_百度贴吧。"
                "欢迎下载:http://www.8222.cn 欢迎下载:http://www.haodisoft.com"
            ),
            "matched_keywords": ["群发"],
            "matched_themes": ["众包任务", "工具交易"],
        }
    )

    assert classification.risk_category == "工具交易"
    assert classification.secondary_label == "群控脚本"
    assert "众包服务" not in classification.conflict_categories


def test_manual_heldout_bitget_rebate_variant_routes_to_rebate_traffic():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-bitget-rebate-variant",
            "source_name": "x_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "Bitget Wallet 的美股现货反佣正式生效啦，有超高返佣，交易量越高返得越多。",
            "matched_keywords": ["反佣"],
            "matched_themes": ["刷单作弊"],
        }
    )

    assert classification.risk_category == "诈骗引流"
    assert classification.secondary_label == "返利引流"
    assert "刷单作弊" not in classification.conflict_categories


def test_manual_heldout_bitget_fanyong_spelling_routes_to_rebate_traffic():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-bitget-fanyong-spelling",
            "source_name": "x_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "Bitget Wallet 的美股现货反佣正式生效啦，交易量越高反得越多，开户后按交易量返点。",
            "matched_keywords": ["反佣"],
            "matched_themes": ["刷单作弊"],
        }
    )

    assert classification.risk_category == "诈骗引流"
    assert classification.secondary_label == "返利引流"
    assert "刷单作弊" not in classification.conflict_categories


def test_manual_heldout_automation_license_manager_error_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-automation-license-manager",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "windows 无法启动 automation license manager service 报错1067。"
                "博途吧建议删掉已经安装的 automation license manager，重新安装前关掉防火墙。"
            ),
            "matched_keywords": ["automation"],
            "matched_themes": ["工具交易"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_creator_addv_certification_question_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-creator-addv-certification",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "已通过创作人认证，如何申请加V？申请条件里写获得百家号原创标签，"
                "现在原创标签已经下线，是不是有问题？"
            ),
            "matched_keywords": ["加v"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_public_course_scam_discussion_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-public-course-scam-discussion",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "拉人进群的绘画群多为卖课骗局吗？吧友揭秘绘画群拉人套路及真假辨别方法。",
            "matched_keywords": ["拉人", "拉群"],
            "matched_themes": ["诈骗引流", "众包任务"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_manual_heldout_english_second_hand_account_sale_is_account_trade():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-carousell-account-sale",
            "source_name": "second_hand_blackgray_search",
            "source_type": "Vertical",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "Carousell account For Sale | Buy 1,000+ Carousell account online from sellers.",
        }
    )

    assert classification.risk_category == "账号交易"
    assert classification.secondary_label == "实名账号买卖"


def test_manual_heldout_game_tanking_pull_guide_is_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "typical-error-wow-tanking-pull-guide",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "防骑拉怪技巧：命令圣印、奉献与嘲讽技能详解，BOSS阶段按天赋循环输出。",
            "matched_keywords": ["拉怪"],
            "matched_themes": ["诈骗引流", "众包任务"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_classifier_demotes_public_scam_news_without_contact_to_defensive_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "hard-negative-running-score-news",
            "source_name": "public_security_news",
            "source_type": "News",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "新闻报道某地破获跑分团伙，文中无联系方式。",
            "matched_keywords": ["跑分"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label in {"防御语境", "低相关"}
    assert classification.review_required is False


def test_classifier_treats_negated_contact_and_open_link_as_non_affirmative():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "context-conflict-negated-open-link",
            "source_name": "context-safety-3",
            "source_type": "Policy",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "辟谣帖：所谓接码验证码项目是骗局，请勿上车，未给出 TG 或开户链接。",
            "matched_keywords": ["接码", "验证码", "开户链接"],
            "matched_themes": ["诈骗引流", "接码"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label in {"防御语境", "低相关"}
    assert classification.review_required is False


def test_classifier_demotes_consumer_sms_verification_complaint_to_low_relevance():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "hard-negative-qq-sms-complaint",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "QQ短信验证码收不到? 手机号未欠费且其他软件验证码正常，"
                "但唯独QQ收不到短信验证码，换手机尝试也无效，探讨QQ验证码收不到的原因及解决办法。"
            ),
            "matched_keywords": ["验证码", "短信验证码"],
            "matched_themes": ["接码"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_classifier_demotes_ordinary_group_send_mail_discussion_to_low_relevance():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "hard-negative-group-mail-reply",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "群发的邮件回复是所有人都能看到吗？取决于发送时选择的是收件人还是全部回复，"
                "普通办公邮件设置讨论。"
            ),
            "matched_keywords": ["群发"],
            "matched_themes": ["众包任务"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_classifier_keeps_active_risk_contrasts_for_false_positive_guards():
    classifier = FineGrainedIntentClassifier()

    sms_trade = classifier.classify(
        {
            "trace_id": "contrast-real-sms-trade",
            "source_name": "telegram_public_delivery:sms",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "接码平台批量注册账号，临时号码接收验证码，联系客服 @smsbot 下单，价格低。",
            "matched_keywords": ["接码", "验证码"],
            "matched_themes": ["接码"],
        }
    )
    click_work = classifier.classify(
        {
            "trace_id": "contrast-real-click-work",
            "source_name": "telegram_public_delivery:click",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "刷单补单垫付返佣，日结兼职招募，做任务后返佣，联系 @tasker 上车。",
            "matched_keywords": ["刷单", "补单", "垫付", "返佣"],
            "matched_themes": ["刷单作弊"],
        }
    )
    rebate_traffic = classifier.classify(
        {
            "trace_id": "contrast-real-rebate-traffic",
            "source_name": "x_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "开户链接拉人注册吃返佣，交易量越高返点越多，联系 TG:rebate001 领取高佣链接。",
            "matched_keywords": ["开户链接", "返佣"],
            "matched_themes": ["诈骗引流"],
        }
    )
    tool_trade = classifier.classify(
        {
            "trace_id": "contrast-real-tool-trade",
            "source_name": "telegram_public_delivery:tool",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "群控脚本卡密出售，云控后台支持群发拉群，低价卡密联系 @toolbot。",
            "matched_keywords": ["群控", "脚本", "卡密", "群发"],
            "matched_themes": ["工具交易"],
        }
    )

    assert sms_trade.risk_category == "账号交易"
    assert sms_trade.secondary_label == "接码注册"
    assert click_work.risk_category == "刷单作弊"
    assert click_work.secondary_label in {"刷单返佣", "垫付兼职"}
    assert rebate_traffic.risk_category == "诈骗引流"
    assert rebate_traffic.secondary_label == "返利引流"
    assert tool_trade.risk_category == "工具交易"
    assert tool_trade.secondary_label == "群控脚本"


def test_classifier_demotes_public_retail_private_domain_case_study_to_low_relevance():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "heldout-retail-private-domain-case-study",
            "source_name": "seeding_note_blackgray_search",
            "source_type": "Social",
            "source_url": "https://post.smzdm.com/p/azz25vr5/",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "【案例拆解】私域会员超600万,复购率超50%,泡泡玛特私域体系全拆解!_服务软件_什么值得买 "
                "泡泡玛特围绕企业微信、小程序商城构建私域体系，通过精细化的引流策略、会员机制和多元营销玩法，"
                "为潮流零售行业提供有参考价值的运营范本。"
            ),
            "matched_keywords": ["引流", "私域"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_classifier_demotes_meta_sms_login_complaint_to_low_relevance():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "heldout-meta-sms-login-complaint",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Meta账户手机短信验证码接收不到,怎么解决? 我以前已经注册了账户，"
                "现在登录需要手机验证码，登录不上去，手机验证码接收不到，"
                "我是移动手机号，也打过10086问没有拦截，我自己也没开拦截，这是什么原因?"
            ),
            "matched_keywords": ["验证码", "短信验证码"],
            "matched_themes": ["接码"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


def test_classifier_routes_full_bitget_rebate_ocr_row_to_rebate_traffic():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "heldout-full-bitget-rebate",
            "source_name": "x_blackgray_search",
            "source_type": "IM",
            "source_url": "https://x.com/LeslieLi92210/status/2058834573514211774",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "星期一 上钟上钟 再不认真上班要被领导骂了 @kajiweb3 "
                "Bitget Wallet 的美股现货反佣从今天正式生效啦 有超高返佣 "
                "全平台难找一二 是谁还不来和鼠鼠合作? "
                "我们是支持美股种类最多的 Web3 平台，聚合了HypeLiquid、ONDO、Xstocks等多家优质股票的专业供应商。"
            ),
            "matched_keywords": ["返佣"],
            "matched_themes": ["刷单作弊"],
        }
    )

    assert classification.risk_category == "诈骗引流"
    assert classification.secondary_label == "返利引流"
    assert "刷单作弊" not in classification.conflict_categories


def test_classifier_demotes_public_automation_tech_rows_to_low_relevance():
    classifier = FineGrainedIntentClassifier()

    v2ex_bot_site = classifier.classify(
        {
            "trace_id": "heldout-v2ex-bot-site-discussion",
            "source_name": "tech_forum_blackgray_search",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "tg 机器人创建的网站,用的什么原理? - V2EX bot 会邀请 userbot 成为频道普通订阅者,"
                "服务于历史内容的整理。全系统部署在北美云服务上,重视用户隐私。二、创建自己频道并添加机器人。"
            ),
            "matched_keywords": ["tg", "bot"],
            "matched_themes": ["工具交易"],
        }
    )
    automation_ad = classifier.classify(
        {
            "trace_id": "heldout-automation-software-ad",
            "source_name": "tech_forum_blackgray_search",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Top 10 Automation Software - Free List of the Top Products Ad Viewing ads is privacy protected "
                "by DuckDuckGo. Ad clicks are managed by Microsoft ad network."
            ),
            "matched_keywords": ["automation", "software"],
            "matched_themes": ["工具交易"],
        }
    )

    assert v2ex_bot_site.risk_category == "正常业务白噪声"
    assert v2ex_bot_site.secondary_label == "低相关"
    assert v2ex_bot_site.review_required is False
    assert automation_ad.risk_category == "正常业务白噪声"
    assert automation_ad.secondary_label == "低相关"
    assert automation_ad.review_required is False


def test_classifier_demotes_encyclopedia_and_plain_automation_contexts_to_low_relevance():
    classifier = FineGrainedIntentClassifier()

    encyclopedia = classifier.classify(
        {
            "trace_id": "hard-negative-encyclopedia-bot",
            "source_name": "wiki_public_article",
            "source_type": "Article",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "百科词条：Telegram bot 是一种自动化程序，本文介绍历史、架构、API 设计、"
                "频道管理功能和常见开源项目，不提供账号、价格、下单或售后联系方式。"
            ),
            "matched_keywords": ["bot", "automation"],
            "matched_themes": ["工具交易"],
        }
    )
    automation_product = classifier.classify(
        {
            "trace_id": "hard-negative-automation-product",
            "source_name": "automationdirect_public_catalog",
            "source_type": "Vertical",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "AutomationDirect 工业自动化产品介绍：PLC、传感器、控制器和教程文档，"
                "用于工厂自动化选型和维护说明，没有黑灰产交易意图。"
            ),
            "matched_keywords": ["automation", "software"],
            "matched_themes": ["工具交易"],
        }
    )

    assert encyclopedia.risk_category == "正常业务白噪声"
    assert encyclopedia.secondary_label == "低相关"
    assert encyclopedia.review_required is False
    assert automation_product.risk_category == "正常业务白噪声"
    assert automation_product.secondary_label == "低相关"
    assert automation_product.review_required is False


def test_classifier_requires_transaction_or_service_evidence_for_tool_tutorials():
    classifier = FineGrainedIntentClassifier()

    plain_tutorial = classifier.classify(
        {
            "trace_id": "hard-negative-group-send-tutorial",
            "source_name": "tieba_blackgray_search",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "群发脚本功能更新说明：修复发送问题，新增配置文档和使用教程，"
                "说明群发器参数含义；没有报价、下单、售后或联系方式。"
            ),
            "matched_keywords": ["群发", "脚本"],
            "matched_themes": ["工具交易"],
        }
    )
    commercial_tool = classifier.classify(
        {
            "trace_id": "positive-group-send-commercial-tool",
            "source_name": "telegram_public_delivery:risk_tool",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "群发器脚本长期出售，支持批量拉群和自动私信，月卡 399，"
                "下单后提供售后配置，联系 TG:risktool。"
            ),
            "matched_keywords": ["群发", "脚本", "下单"],
            "matched_themes": ["工具交易"],
        }
    )
    sale_word_without_service_evidence = classifier.classify(
        {
            "trace_id": "hard-negative-sale-word-without-service-evidence",
            "source_name": "tieba_group_send_tutorial",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "群发器脚本长期出售这个词经常被教程帖讨论，本文只说明功能更新、"
                "配置文档和参数含义，没有报价、下单、售后或联系方式。"
            ),
            "matched_keywords": ["群发", "脚本", "出售"],
            "matched_themes": ["工具交易"],
        }
    )

    assert plain_tutorial.risk_category == "正常业务白噪声"
    assert plain_tutorial.secondary_label == "低相关"
    assert plain_tutorial.review_required is False
    assert sale_word_without_service_evidence.risk_category == "正常业务白噪声"
    assert sale_word_without_service_evidence.secondary_label == "低相关"
    assert sale_word_without_service_evidence.review_required is False
    assert commercial_tool.risk_category == "工具交易"
    assert commercial_tool.secondary_label == "群控脚本"
    assert commercial_tool.review_bucket == "explicit_risk"


def test_classifier_routes_public_group_send_plugins_to_tool_workflow():
    classifier = FineGrainedIntentClassifier()

    plugin_source_code = classifier.classify(
        {
            "trace_id": "heldout-group-send-plugin-source-code",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "契约官方插件系列-群发插件 (包括源代码)【契约机器人吧】 - 选中群发送 可以根据中间的群列表,"
                "勾选对应的群号 发送消息类型 - 群 在群里发送消息 - 群成员 给群的每个成员发送 群临时会话/消息。"
            ),
            "matched_keywords": ["群发"],
            "matched_themes": ["众包任务"],
        }
    )
    group_sender_tutorial = classifier.classify(
        {
            "trace_id": "heldout-group-send-tutorial-tool",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "群发无忧怎么使用?_群发无忧吧_百度贴吧 今天就给大家讲一下群发无忧的使用方法:"
                "以群发群功能举例,进入群发群设置页面,设置好要发送的内容,开始选要发送的群。"
            ),
            "matched_keywords": ["群发"],
            "matched_themes": ["众包任务"],
        }
    )

    assert plugin_source_code.risk_category == "工具交易"
    assert plugin_source_code.secondary_label == "群控脚本"
    assert "众包服务" not in plugin_source_code.conflict_categories
    assert group_sender_tutorial.risk_category == "工具交易"
    assert group_sender_tutorial.secondary_label == "群控脚本"
    assert "众包服务" not in group_sender_tutorial.conflict_categories


def test_classifier_demotes_finance_ai_manual_order_article_to_low_relevance():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "heldout-ai-quant-manual-order",
            "source_name": "x_blackgray_search",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "为什么告别手工单,拥抱AI量化交易? 情绪零干扰:AI严格执行规则,杜绝追涨杀跌。"
                "数据秒杀:AI瞬时分析行情，自动执行交易策略。"
            ),
            "matched_keywords": ["手工单"],
            "matched_themes": ["刷单作弊"],
        }
    )

    assert classification.risk_category == "正常业务白噪声"
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False


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


def test_source_smoke_report_covers_four_required_smoke_groups_without_changing_source_classes():
    report = build_report(load_sources("config/intel_sources.public.yaml"), network_enabled=False)

    assert report["status"] == "completed"
    assert report["required_smoke_groups"] == [
        "im_or_group",
        "public_account_or_article",
        "social_or_forum",
        "vertical_or_technical",
    ]
    assert set(report["covered_smoke_groups"]) == set(report["required_smoke_groups"])
    assert set(report["covered_source_classes"]) == {"im_or_group", "social_or_forum", "vertical_or_technical"}
    assert {item["smoke_group"] for item in report["per_smoke_group_evidence"]} == set(report["required_smoke_groups"])
    assert report["source_evidence_by_group"]["public_account_or_article"][0]["source_class"] == "social_or_forum"
    assert report["source_evidence_by_group"]["public_account_or_article"][0]["url"]
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
    assert set(report["live_attempted_smoke_groups"]) == {
        "im_or_group",
        "public_account_or_article",
        "social_or_forum",
        "vertical_or_technical",
    }
    assert len(calls) >= 4
    assert "telegram_group_public_timeline" in calls
    assert "wechat_public_account_article_search" in calls
    assert all(
        item["target_met"] or item["collected_count"] == item["configured_source_count"]
        for item in report["per_smoke_group_evidence"]
    )
    assert all(row["live_smoke_attempted"] for row in report["sources"])


def test_authorized_live_source_smoke_collects_loopback_feed():
    report = run_live_source_smoke()

    assert report["status"] == "completed"
    assert report["run_type"] == "live_authorized_loopback_collection_smoke"
    assert report["smoke_scope"] == "four_required_source_evidence_groups"
    assert report["authorization_enforced"] is True
    assert report["required_smoke_groups"] == [
        "im_or_group",
        "public_account_or_article",
        "social_or_forum",
        "vertical_or_technical",
    ]
    assert set(report["covered_smoke_groups"]) == set(report["required_smoke_groups"])
    assert set(report["covered_source_classes"]) == {"im_or_group", "social_or_forum", "vertical_or_technical"}
    assert set(report["source_evidence_by_group"]) == set(report["required_smoke_groups"])
    for group, rows in report["source_evidence_by_group"].items():
        assert rows, group
        assert rows[0]["source_name"]
        assert rows[0]["url"].startswith(("https://", "http://"))
        assert rows[0]["raw_body"] or rows[0]["hydrated_body"]
        assert rows[0]["capture_snapshot_uri"]
        assert rows[0]["raw_payload_uri"]
    assert report["source_evidence_by_group"]["public_account_or_article"][0]["source_class"] == "social_or_forum"
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


def test_heldout_builder_keeps_normal_noise_and_unknown_review_buckets():
    records = [
        {
            "trace_id": "h-noise",
            "source_name": "Automationforum",
            "source_type": "TechForum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Read Full Guide: Siphon Tube Pressure Gauge Steam Service Guide "
                "for process engineers at automationforum.co."
            ),
        },
        {
            "trace_id": "h-unknown",
            "source_name": "misc-public",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "火苗蓝盘今晚继续，暗号777。",
        },
        {
            "trace_id": "h-risk",
            "source_name": "tg",
            "source_type": "IM",
            "content_text": "群控脚本接码，联系 TG:h001",
        },
    ]

    heldout = build_heldout_records(records, limit=3, per_category=2)

    categories = {item["source_trace_id"]: item["expected_risk_categories"][0] for item in heldout}
    assert categories["h-noise"] == "正常业务白噪声"
    assert categories["h-unknown"] == "unknown"
    assert categories["h-risk"] == "工具交易"
    assert {item["source_trace_id"] for item in heldout} == {"h-noise", "h-unknown", "h-risk"}


def test_heldout_builder_cli_defaults_target_100_to_300_manual_review_rows():
    from scripts.build_heldout_eval import parse_args as parse_heldout_args
    from scripts.export_manual_heldout_review import parse_args as parse_review_args
    from scripts.validate_manual_heldout import parse_args as parse_validate_args

    heldout_args = parse_heldout_args([])
    review_args = parse_review_args([])
    validate_args = parse_validate_args([])

    assert 100 <= heldout_args.limit <= 300
    assert review_args.limit == heldout_args.limit
    assert review_args.min_target >= 100
    assert review_args.min_target <= review_args.limit
    assert validate_args.min_records == review_args.min_target


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


def test_manual_heldout_validator_splits_direct_jsonl_semicolon_labels():
    records = [
        {
            "trace_id": "m-direct-split",
            "content_text": "群控脚本 TG:m1",
            "human_review": {
                "status": "confirmed",
                "annotator": "analyst-a",
                "review_date": "2026-06-03",
                "final_risk_categories": "工具交易;账号交易",
                "final_secondary_labels": "群控脚本;接码注册",
                "conflict_handling": "multi_label_confirmed",
                "typical_error": "none",
            },
        }
    ]

    confirmed, report = validate_records(records, min_records=1)

    assert report["status"] == "completed"
    assert confirmed[0]["expected_risk_categories"] == ["工具交易", "账号交易"]
    assert confirmed[0]["expected_secondary_labels"] == ["群控脚本", "接码注册"]


def test_manual_heldout_validator_requires_typical_error_field():
    records = [
        {
            "trace_id": "m-missing-error",
            "content_text": "群控脚本 TG:m1",
            "human_review": {
                "status": "confirmed",
                "annotator": "analyst-a",
                "review_date": "2026-06-03",
                "final_risk_categories": ["工具交易"],
                "conflict_handling": "rule_label_confirmed",
            },
        }
    ]

    confirmed, report = validate_records(records, min_records=1)

    assert confirmed == []
    assert report["status"] == "insufficient_confirmed_records"
    assert report["issues"][0]["fields"] == ["typical_error"]


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
    assert all(record["ocr_text"] == record["content_text"] for record in records)
    assert all(record["ocr_status"] == "completed" for record in records)
    assert all(record["ocr_confidence"] == 1.0 for record in records)
    assert all(record["ocr_engine_confidences"]["bitmap_glyph"] == 1.0 for record in records)
    assert all({"contact", "links", "slang", "tool_names"} <= set(record["manual_labels"]) for record in records)
    entity_types = {
        entity["entity_type"]
        for record in records
        for entity in record["expected_entities"]
    }
    assert {"contact", "tool_name", "invite_code"} <= entity_types
    assert any(entity["entity_type"] == "url" for record in records for entity in record["expected_entities"])


def test_collection_source_class_prefers_structured_vertical_type_over_name_text():
    source = {
        "source_name": "opt_crowdsourcing_telegram_automation",
        "source_type": "Vertical",
        "platform": "crowdsourcing",
    }

    assert source_class_for_record(source) == "vertical_or_technical"


def test_public_account_article_markers_are_non_im_even_with_telegram_text():
    sources = [
        {
            "source_name": "wechat_public_telegram_risk_articles",
            "source_type": "Public_Account",
            "platform": "wechat_public",
            "source_url": "https://article.example/search?q=telegram",
        },
        {
            "source_name": "rss_telegram_security_articles",
            "source_type": "rss",
            "platform": "article",
            "source_url": "https://rss.example/telegram.xml",
        },
        {
            "source_name": "html_article_telegram_watch",
            "source_type": "html_article",
            "platform": "public_account",
            "source_url": "https://article.example/telegram-watch",
        },
    ]

    assert {source_class_for_record(source) for source in sources} == {"social_or_forum"}


def test_runtime_source_diversity_uses_public_account_article_taxonomy():
    from src.agent.runtime_collection_services import _source_diversity_class

    source = {
        "source_name": "wechat_public_telegram_risk_articles",
        "source_type": "Article",
        "platform": "wechat_public",
        "source_url": "https://article.example/search?q=telegram",
    }

    assert source_class_for_record(source) == "social_or_forum"
    assert _source_diversity_class(source) == "social_or_forum"


def test_public_catalog_contains_public_account_article_import_example():
    sources = [
        source
        for source in load_source_catalog("config/intel_sources.public.yaml")
        if source["source_name"] == "wechat_public_account_article_search"
    ]

    assert sources
    assert {source_class_for_record(source) for source in sources} == {"social_or_forum"}
    assert all(source["source_type"] == "Article" for source in sources)
    assert all(source["platform"] == "wechat_public" for source in sources)
    assert all(source["feed_format"] == "html" for source in sources)
    assert all(source["allowed_domains"] == ["r.jina.ai"] for source in sources)


def test_source_smoke_report_keeps_public_account_article_in_social_class():
    report = build_report(
        [
            {
                "source_name": "wechat_public_telegram_risk_articles",
                "source_type": "Article",
                "platform": "wechat_public",
                "source_url": "https://article.example/search?q=telegram",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "robots_allowed": True,
                "terms_allow_security_research": True,
            }
        ],
        network_enabled=False,
    )

    assert report["candidate_source_count"] == 1
    assert report["sources"][0]["source_class"] == "social_or_forum"
    assert report["sources"][0]["smoke_group"] == "public_account_or_article"
    assert report["source_evidence_by_group"]["public_account_or_article"][0]["source_class"] == "social_or_forum"


def test_collect_public_sources_balanced_slice_keeps_non_im_source_groups():
    sources = [
        {"source_name": "forum_a", "source_type": "Forum", "source_url": "https://example.test/a1"},
        {"source_name": "forum_a", "source_type": "Forum", "source_url": "https://example.test/a2"},
        {"source_name": "forum_b", "source_type": "Forum", "source_url": "https://example.test/b1"},
        {"source_name": "vertical_a", "source_type": "Vertical", "source_url": "https://example.test/c1"},
        {"source_name": "vertical_a", "source_type": "Vertical", "source_url": "https://example.test/c2"},
        {"source_name": "vertical_b", "source_type": "Vertical", "source_url": "https://example.test/d1"},
    ]

    selected = balanced_source_slice(sources, max_sources=4)
    selected_groups = {item["source_name"] for item in selected}
    selected_classes = {source_class_for_record(item) for item in selected}

    assert len(selected) == 4
    assert {"social_or_forum", "vertical_or_technical"} <= selected_classes
    assert {"forum_a", "forum_b", "vertical_a", "vertical_b"} == selected_groups


def test_source_default_minimum_quotas_cover_required_non_im_groups():
    quotas = source_minimum_quotas_from_args(
        Namespace(source_min_quota=[], disable_source_min_quotas=False)
    )

    assert quotas["vertical_or_technical"] >= 1
    assert quotas["social_or_forum"] >= 1
    assert quotas["secondhand_market"] >= 1
    assert quotas["crowdsourcing_platform"] >= 1


def test_source_quota_groups_include_forum_tieba_and_granular_non_im_types():
    records = [
        {"source_name": "vertical_security_search", "source_type": "Vertical"},
        {"source_name": "tieba_account_trade_search", "source_type": "Forum", "platform": "tieba"},
        {"source_name": "wechat_public_account_article_search", "source_type": "Article", "platform": "wechat_public"},
        {"source_name": "secondhand_account_trade", "source_type": "Vertical", "platform": "second_hand_market"},
        {"source_name": "crowdsourcing_task_search", "source_type": "Vertical", "platform": "crowdsourcing"},
    ]

    groups = {
        group
        for record in records
        for group in source_quota_groups_for_record(record)
    }

    assert {
        "vertical_or_technical",
        "social_or_forum",
        "public_account_or_article",
        "secondhand_market",
        "crowdsourcing_platform",
    } <= groups


def test_collect_public_sources_per_source_cap_prevents_single_source_dominance(tmp_path):
    catalog_path = tmp_path / "source_catalog_cap.yaml"
    catalog_path.write_text(
        """
sources:
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-1.json
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-2.json
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-3.json
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-4.json
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-5.json
  - source_name: forum_public_search
    source_type: Forum
    source_url: https://forum.example/feed.json
  - source_name: vertical_security_search
    source_type: Vertical
    source_url: https://vertical.example/feed.json
  - source_name: article_public_search
    source_type: Article
    platform: wechat_public
    source_url: https://article.example/feed.json
        """.strip(),
        encoding="utf-8",
    )

    selected, summary = selected_sources_from_args(
        Namespace(
            source_class=[],
            max_sources=5,
            source_min_quota=[],
            disable_source_min_quotas=True,
        ),
        catalog_path,
    )

    selected_name_counts = Counter(source["source_name"] for source in selected or [])
    assert len(selected or []) == 5
    assert selected_name_counts["telegram_public_big"] <= 2
    assert summary["source_name_max_quota"] == 2
    assert summary["source_name_quota_warnings"] == []


def test_source_quota_selection_applies_source_name_cap_when_max_sources_is_unbounded():
    sources = [
        {
            "source_name": "telegram_public_big",
            "source_type": "IM",
            "source_class": "im_or_group",
            "source_url": f"https://telegram.example/feed-{index}.json",
        }
        for index in range(5)
    ] + [
        {"source_name": "forum_public_search", "source_type": "Forum", "source_url": "https://forum.example/feed.json"},
        {"source_name": "vertical_security_search", "source_type": "Vertical", "source_url": "https://vertical.example/feed.json"},
    ]

    selected = quota_balanced_source_slice(
        sources,
        max_sources=0,
        minimum_quotas=DEFAULT_SOURCE_MIN_QUOTAS,
        source_name_max_quota=2,
    )
    selected_name_counts = Counter(source["source_name"] for source in selected)

    assert selected_name_counts["telegram_public_big"] == 2
    assert selected_name_counts["forum_public_search"] == 1
    assert selected_name_counts["vertical_security_search"] == 1


def test_collect_public_sources_balances_default_unbounded_catalog(tmp_path):
    catalog_path = tmp_path / "source_catalog_default_quota.yaml"
    catalog_path.write_text(
        """
sources:
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-1.json
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-2.json
  - source_name: telegram_public_big
    source_type: IM
    source_class: im_or_group
    source_url: https://telegram.example/feed-3.json
  - source_name: forum_public_search
    source_type: Forum
    source_url: https://forum.example/feed.json
  - source_name: vertical_security_search
    source_type: Vertical
    source_url: https://vertical.example/feed.json
        """.strip(),
        encoding="utf-8",
    )

    selected, summary = selected_sources_from_args(
        Namespace(
            source_class=[],
            max_sources=0,
            source_min_quota=[],
            disable_source_min_quotas=True,
        ),
        catalog_path,
    )

    selected_name_counts = Counter(source["source_name"] for source in selected or [])
    assert selected is not None
    assert summary["selection_mode"] == "catalog_expanded_filtered"
    assert summary["max_sources"] == 0
    assert selected_name_counts["telegram_public_big"] == 2
    assert selected_name_counts["forum_public_search"] == 1
    assert selected_name_counts["vertical_security_search"] == 1


def test_acceptance_evidence_export_tracks_high_quality_target_and_source_classes():
    run = {
        "status": "completed",
        "mode": "live_collection_pipeline",
        "query": "验收",
        "input_count": 5,
        "fetched_count": 5,
        "selected_source_count": 2,
        "high_quality_count": 2,
        "candidate_count": 0,
        "selected_sources": [
            {"source_name": "forum", "source_type": "Forum"},
            {"source_name": "vertical", "source_type": "Vertical"},
        ],
        "collection_runs": [
            {"source_name": "forum", "source_type": "Forum", "collection_layer": "theme_core", "fetched_count": 3},
            {"source_name": "vertical", "source_type": "Vertical", "collection_layer": "theme_core", "fetched_count": 2},
        ],
        "execution_summary": {
            "accepted_count": 5,
            "risk_clue_count": 2,
            "refined_clue_count": 2,
        },
        "high_quality_clues": [
            {
                "clue_id": "c1",
                "clue_type": "shared_contact_48h",
                "risk_category": "诈骗引流",
                "evidence_trace_ids": ["a", "b", "c"],
                "source_names": ["forum"],
                "source_types": ["Forum"],
                "quality_score": 0.8,
                "confidence": 0.85,
                "evidence_reviewability": {
                    "source_count": 1,
                    "entity_support_count": 1,
                    "original_snippets": ["forum snippet"],
                    "time_range": {"start": "2026-06-07T08:00:00+00:00", "end": "2026-06-07T08:00:00+00:00"},
                    "false_positive_risk": {"score": 0.45, "level": "medium", "reasons": ["single_source_false_positive_risk"]},
                    "suggested_review_action": "human_verify_single_source_or_weak_entity_support",
                },
            },
            {
                "clue_id": "c2",
                "clue_type": "high_frequency_template",
                "risk_category": "工具交易",
                "evidence_trace_ids": ["d", "e", "f"],
                "source_names": ["vertical"],
                "source_types": ["Vertical"],
                "quality_score": 0.78,
                "confidence": 0.9,
            },
        ],
    }

    record_details = {
        "records": [
            {
                "trace_id": "d",
                "source": "vertical",
                "source_type": "Vertical",
                "summary": "原始样本：群控脚本 接码联系 TG:risk",
                "cleaning_visible": "群控脚本 接码联系 TG:risk",
                "classification_label": "工具交易",
                "confidence": 0.82,
                "review_required": True,
                "entities": [{"type": "contact", "value": "TG:risk"}],
            }
        ]
    }

    evidence = build_evidence(
        run,
        run_path="run.json",
        smoke_path="smoke.txt",
        source_catalog="sources.yaml",
        command="cmd",
        record_details=record_details,
    )

    assert evidence["target"]["high_quality_count_met"] is True
    assert evidence["counts"]["high_quality_count"] == 2
    assert {"social_or_forum", "vertical_or_technical"} <= set(evidence["selected_source_classes"])
    assert [item["evidence_trace_count"] for item in evidence["agent_final_output"]] == [3, 3]
    assert evidence["agent_final_output"][0]["evidence_reviewability"]["source_count"] == 1
    assert evidence["agent_final_output"][0]["suggested_review_action"] == "human_verify_single_source_or_weak_entity_support"
    assert evidence["agent_final_output"][1]["evidence_reviewability"]["source_count"] == 1
    assert evidence["agent_final_output"][1]["evidence_reviewability"]["suggested_review_action"]
    card = evidence["agent_final_output"][1]["evidence_reviewability"]["evidence_cards"][0]
    assert card["trace_id"] == "d"
    assert card["raw_snippet"] == "原始样本：群控脚本 接码联系 TG:risk"
    assert card["clean_text"] == "群控脚本 接码联系 TG:risk"
    assert card["classification"]["risk_category"] == "工具交易"
    assert card["entities"][0]["normalized_value"] == "TG:risk"


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
