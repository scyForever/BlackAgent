from src.agent.exploration_agent import ExplorationAgent
from src.enhancement.engine import PhaseTwoThreeEngine
from src.enhancement.lifecycle import DynamicSlangLifecycleManager, PromptEvaluator
from src.enhancement.source_intake import ComplianceSourceDiscovery, MultimodalTextExtractor
from src.enhancement.text_intelligence import AdaptiveEntropyFilter, AdvancedEntityExtractor, FineGrainedIntentClassifier, SlangVariantNormalizer
from src.classifier.nlp_rule_matcher import (
    ACCOUNT_TRADING,
    CLICK_FARMING,
    CROWD_SERVICE,
    FRAUD_TRAFFIC,
    NORMAL_NOISE,
    TOOL_TRADING,
    RuleFastTrackClassifier,
    review_bucket_for_classification,
)
from src.local_runtime import LocalAgentRuntime
from storage import GraphRepo, VectorRepo


def test_slang_variant_normalizer_confirms_context_before_risk_hint():
    normalizer = SlangVariantNormalizer()

    risky = normalizer.analyze("dy 打粉引流，➕v 咨询，短链 hxxps://risk.top")
    benign = normalizer.analyze("这篇文章介绍 dy 的普通运营经验")

    assert any(item.normalized == "抖音" and item.context_confirmed for item in risky.candidates)
    assert "加v" in risky.expanded_text
    assert not any(item.context_confirmed for item in benign.candidates)


def test_fine_classifier_routes_obvious_public_technical_pages_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "benign-technical-1",
            "source_name": "Automationforum",
            "source_type": "TechForum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Partial Discharge (PD) Testing of electrical equipment. "
                "This public article explains insulation diagnostics, "
                "ElectricalEngineering and ConditionMonitoring concepts for maintenance teams. "
                "Follow Automationforum on Telegram: https://t.me/F0rumElectrical "
                "and read more at https://automationforum.co/electrical-engineering/"
            ),
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_information_no_risk_signal"


def test_fine_classifier_routes_public_steam_guides_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "benign-technical-steam-guide",
            "source_name": "telegram_public_delivery:Automationforum",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Siphon Tube Pressure Gauge Steam Service Guide. "
                "Read Full Guide: https://automationforum.co/siphon-tube-pressure-gauge-steam-service-guide/ "
                "Topic: Siphon tube pressure gauge steam service guide for process engineers."
            ),
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_information_no_risk_signal"
    assert classification.review_bucket == "low_relevance"


def test_fine_classifier_marks_defensive_public_false_positive_as_low_relevance_bucket():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "defensive-low-relevance-risk-keywords",
            "source_name": "security_blog",
            "source_type": "TechForum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "安全研究复盘：群控脚本和接码平台常被用于账号滥用，"
                "本文只做防御检测说明，不提供工具、不交易账号，用户应举报相关广告。"
            ),
            "matched_keywords": ["群控", "接码"],
            "matched_themes": ["工具交易", "接码"],
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.review_required is False
    assert classification.review_bucket == "low_relevance"


def test_fine_classifier_routes_public_steam_verification_discussion_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "manual-fp-steam-verification-discussion",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Steam验证码为何由优速通代发? 因国内接收国际短信需代理。"
                "steam忘记密码时手机验证账号，收到的验证码由优速通发来，"
                "探讨优速通发steam验证码的原因及安全性。"
            ),
            "matched_keywords": ["代发", "验证码"],
            "matched_themes": ["众包任务", "接码"],
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_routes_consumer_discount_rebate_articles_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "manual-fp-consumer-discount",
            "source_name": "consumer_public_article",
            "source_type": "Vertical",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "外卖品牌饭卡优惠，充100得125，高信誉用户免开会员，"
                "平台活动说明里提到返佣和折上折规则。"
            ),
            "matched_keywords": ["返佣"],
            "matched_themes": ["刷单作弊"],
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_routes_public_automation_ads_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "manual-fp-automation-ad",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "source_url": "https://www.easy-automation.com/automationtechnology",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "NexGen Automation Technology Ad. Our Service-Oriented Approach Ensures "
                "You And Your Company Will Always Receive The Best. Advanced Feed Mill, "
                "Grain & Agronomy Automation Systems Built To Boost Efficiency."
            ),
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_information_no_risk_signal"


def test_fine_classifier_routes_game_automation_mod_guides_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "manual-fp-game-automation-mod",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "终极自动化模组介绍及使用方法。英文名: Ultimate Automation。"
                "中文名: 终极自动化。简介: 原版群星后期繁琐无趣的操作太多了，"
                "此模组改变了这一现状，让这些无趣操作交给自动化脚本处理。"
                "steam创意工坊可查看订阅说明。"
            ),
            "matched_keywords": ["脚本", "automation"],
            "matched_themes": ["工具交易"],
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_routes_public_wechat_group_operations_discussion_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "manual-fp-community-group-discussion",
            "source_name": "tech_forum_blackgray_search",
            "source_type": "Forum",
            "source_url": "https://www.v2ex.com/t/821445",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "微信群超过 200 人之后，只能人工邀请，解决方案讨论。"
                "我有一个技术、创意、生活交流群，群二维码会失效，"
                "这个问题很麻烦，想讨论普通社区拉新的产品限制。"
            ),
            "matched_keywords": ["拉新"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_routes_public_platform_group_howto_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "manual-fp-platform-group-howto",
            "source_name": "tieba_blackgray_search",
            "source_type": "Social",
            "source_url": "https://tieba.baidu.com/p/8601108459",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "b站怎么拉群，在B站拉群需要先下载安装哔哩哔哩APP，"
                "登录后点击私信右侧的创建圈子，可以加入已有的圈子和创建圈子。"
            ),
            "matched_keywords": ["拉群", "验证码"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_keeps_sms_platform_business_offer_out_of_normal_noise_override():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "review-risk-herosms-business-offer",
            "source_name": "telegram_public_delivery:herosms_cn",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "HeroSMS 是一个接码平台。我们提供临时号码用于接收验证码。"
                "主要面向批量购买号码的用户，API 与 SMS-Activate 类似。"
                "请访问 HeroSMS-Partners 页面，我们提供优惠条件和快速支付。"
            ),
            "matched_keywords": ["接码", "接码平台", "验证码"],
            "matched_themes": ["接码"],
        }
    )

    assert classification.risk_category == ACCOUNT_TRADING
    assert classification.secondary_label == "接码注册"
    assert classification.review_decision_reason != "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_keeps_lagroup_business_workflow_out_of_normal_noise_override():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "review-risk-lagroup-workflow",
            "source_name": "telegram_public_delivery:HHweb_yk",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "拉群主任务流程：右上角 whatsapp 上号，选择拉群主号，选择拉群拉手，输入进群人数，"
                "导入拉群资源，输入邀请链接。具体方案细节请详细咨询您的经销代理。"
            ),
            "matched_keywords": ["拉群", "拉裙"],
            "matched_themes": ["诈骗引流", "众包任务"],
        }
    )

    assert classification.risk_category == CROWD_SERVICE
    assert classification.secondary_label == "拉群获客"
    assert classification.review_decision_reason != "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_keeps_private_domain_monetization_out_of_normal_noise_override():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "review-risk-private-domain-monetization",
            "source_name": "x_blackgray_search",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "我目前矩阵3个公众号，每篇末尾和评论区植入私域钩子，"
                "自动导流到微信个人号/企业微信群。私域日新增80-120人，"
                "3个月累积5000+，私域成交占比70%以上，月变现稳定10w+。"
            ),
            "matched_keywords": ["导流", "私域"],
            "matched_themes": ["诈骗引流"],
        }
    )

    assert classification.risk_category == FRAUD_TRAFFIC
    assert classification.secondary_label == "私域导流"
    assert classification.review_decision_reason != "ordinary_public_context_overrode_homonym_risk"


def test_fine_classifier_routes_public_guides_with_neutral_telegram_channel_to_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "benign-technical-telegram-channel",
            "source_name": "telegram_public_delivery:Automationforum",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Understanding Boiler Drum Level Transmitters Accurate DP Measurement Explained. "
                "Read Full Guide: https://automationforum.co/understanding-boiler-drum-level-transmitters/ "
                "Follow our Telegram Channel https://t.me/Automationforum and LinkedIn page."
            ),
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_information_no_risk_signal"


def test_fine_classifier_routes_open_source_userbot_release_notes_to_normal_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "benign-userbot-release-notes",
            "source_name": "telegram_public_delivery:moonuserbot",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "#Update #Fix - Add back reverse parameter in shift and fwdall modules. "
                "Fix circle module for python3+. Update your UserBot and custom modules."
            ),
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_information_no_risk_signal"


def test_fine_classifier_routes_release_notes_with_contributor_mentions_to_noise():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "benign-userbot-release-notes-mention",
            "source_name": "telegram_public_delivery:moonuserbot",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "#Update #Fix - Add back reverse parameter in shift and fwdall modules. "
                "Fix circle module for python3+ thanks @xwvux. "
                "Update your UserBot and custom modules."
            ),
        }
    )

    assert classification.risk_category == NORMAL_NOISE
    assert classification.secondary_label == "低相关"
    assert classification.review_required is False
    assert classification.review_decision_reason == "ordinary_public_information_no_risk_signal"


def test_fine_classifier_keeps_contact_trade_solicitation_out_of_normal_noise_fallback():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "risk-adjacent-crowd-1",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "欢迎老板低价接单，联系 TG:demo，群发私信包量",
        }
    )

    assert classification.risk_category == CROWD_SERVICE
    assert classification.risk_category != NORMAL_NOISE
    assert classification.review_decision_reason != "ordinary_public_information_no_risk_signal"


def test_fine_classifier_keeps_weak_direct_contact_signal_in_review_bucket():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "weak-contact-review-1",
            "source_name": "Automationforum",
            "source_type": "TechForum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": (
                "Public technical guide for electrical testing. "
                "Contact TG:demo and read more at https://automationforum.co/category/testing/"
            ),
        }
    )

    assert classification.risk_category != NORMAL_NOISE
    assert classification.review_required is True
    assert classification.review_decision_reason != "ordinary_public_information_no_risk_signal"
    assert classification.review_bucket == "human_review_required"


def test_fine_classifier_marks_weak_trade_contact_as_human_review_bucket():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "weak-trade-contact-review-bucket",
            "source_name": "misc-public-channel",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "今晚低价资源可谈，联系 TG:weakreview，老板私聊。",
        }
    )

    assert classification.review_required is True
    assert classification.review_bucket == "human_review_required"
    assert classification.risk_category != NORMAL_NOISE


def test_review_bucket_prioritizes_manual_review_over_low_relevance_category():
    assert (
        review_bucket_for_classification(
            risk_category=NORMAL_NOISE,
            review_required=True,
            confidence=0.82,
        )
        == "human_review_required"
    )
    assert (
        review_bucket_for_classification(
            risk_category=NORMAL_NOISE,
            review_required=False,
            confidence=0.82,
            conflict_status="CONFLICT_REVIEW",
        )
        == "human_review_required"
    )


def test_fast_classifier_keeps_defensive_weak_trade_contact_in_review_bucket():
    classification = RuleFastTrackClassifier().classify(
        {
            "trace_id": "defensive-weak-trade-contact-review",
            "source_name": "misc-public-channel",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "安全研究复盘：今晚低价资源可谈，联系 TG:weakreview，老板私聊。",
        }
    )

    assert classification.review_required is True
    assert classification.review_bucket == "human_review_required"
    assert classification.risk_category != NORMAL_NOISE


def test_fine_classifier_promotes_low_price_telegram_slang_to_review_bucket():
    classification = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "weak-telegram-slang-trade",
            "source_type": "IM",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "低价飞机 @seller，老板私聊，价格可谈。",
        }
    )

    assert classification.risk_category == ACCOUNT_TRADING
    assert classification.review_required is True
    assert classification.review_decision_reason != "no_category_score"
    assert "slang_context:platform_account_trade" in classification.evidence


def phase_records():
    return [
        {
            "trace_id": "phase-r1",
            "source_name": "tg-authorized-a",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": "2026-05-23T01:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
            "attachments": [{"ocr_text": "海报OCR：跑分代付 USDT"}],
        },
        {
            "trace_id": "phase-r2",
            "source_name": "forum-authorized-b",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": "2026-05-23T02:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
        },
        {
            "trace_id": "phase-r3",
            "source_name": "feed-authorized-c",
            "source_type": "THREAT_INTEL",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": "2026-05-23T03:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
        },
    ]


def test_phase2_phase3_engine_builds_clues_playbooks_strategies_and_indexes():
    engine = PhaseTwoThreeEngine()

    result = engine.run(
        phase_records(),
        prompt_text="Return strict JSON with confidence, evidence, requires_human_review and no production write.",
        source_candidates=[
            {
                "source_name": "public-index",
                "source_url": "https://intel.example/public",
                "robots_allowed": True,
                "terms_allow_security_research": True,
                "rate_limit_per_minute": 6,
            }
        ],
    )
    payload = result.model_dump()

    assert payload["status"] == "completed"
    assert payload["accepted_count"] == 3
    assert payload["dropped_count"] == 0
    assert payload["classification_count"] == 3
    assert all(item["secondary_label"] == "群控脚本" for item in payload["classifications"])
    assert any(item["entity_type"] == "slang_term" and item["normalized_value"] == "抖音" for item in payload["entities"])
    assert any(item["entity_type"] == "settlement" for item in payload["entities"])
    assert {item["clue_type"] for item in payload["risk_clues"]} >= {"shared_contact_48h", "shared_domain_multi_source"}
    assert payload["playbook_count"] == 1
    assert payload["playbooks"][0]["requires_human_approval"] is True
    assert len(payload["playbooks"][0]["lifecycle_elements"]) >= 2
    assert payload["strategy_count"] >= 3
    assert all(strategy["requires_human_approval"] is True for strategy in payload["strategies"])
    assert all("auto_ban" in strategy["forbidden_actions"] or "auto_enforce" in strategy["forbidden_actions"] for strategy in payload["strategies"])
    assert payload["graph_summary"]["node_count"] > 0
    assert payload["vector_summary"]["record_count"] == 3
    assert payload["prompt_eval"]["passed"] is True
    assert any(item.get("status") == "SCHEDULABLE" for item in payload["compliance_decisions"])
    assert engine.semantic_search("群控脚本 接码", top_k=1)[0]["score"] > 0.2


def test_advanced_components_cover_conflict_entropy_compliance_and_lifecycle():
    noisy = {"trace_id": "noise-1", "content_text": "!!!!!!!!!!!!", "legal_basis": "PUBLIC_COMPLIANT_DATA"}
    assert AdaptiveEntropyFilter().evaluate(noisy).action == "DROP"

    classification = FineGrainedIntentClassifier().classify(
        {"trace_id": "conflict-1", "content_text": "接码账号 群控脚本 跑分代付 群发私信", "legal_basis": "PUBLIC_COMPLIANT_DATA"}
    )
    assert classification.review_required is True
    assert classification.conflict_status == "CONFLICT_REVIEW"
    assert classification.conflict_categories

    crowd_lead = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "crowd-1",
            "content_text": "TG私信代发业务上线，支持发送手机号和用户名，提供回执，欢迎老板下单合作。",
            "matched_themes": ["众包任务"],
            "matched_keywords": ["代发", "接单"],
        }
    )
    assert crowd_lead.risk_category == CROWD_SERVICE
    assert crowd_lead.secondary_label == "代投服务"

    crowd_ops = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "crowd-2",
            "content_text": "代运营分成，帮别人跑矩阵，我拿40-60%，私域转化后端卖服务。",
            "matched_themes": ["诈骗引流"],
            "matched_keywords": ["私域", "导流"],
        }
    )
    assert crowd_ops.risk_category == CROWD_SERVICE
    assert crowd_ops.secondary_label == "代运营"

    crowd_ads = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "crowd-3",
            "content_text": "承接TG搜索平台首页展示、SEO排名、关键词推广和广告投放，欢迎咨询。",
            "matched_themes": ["众包任务"],
            "matched_keywords": ["接单"],
        }
    )
    assert crowd_ads.risk_category == CROWD_SERVICE
    assert crowd_ads.secondary_label == "代投服务"

    crowd_lagroup = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "crowd-4",
            "content_text": "WS拉群全球，拉100-400人群，稳定不封，保开群，业务联系 @demo。",
            "matched_themes": ["诈骗引流"],
            "matched_keywords": ["拉群"],
        }
    )
    assert crowd_lagroup.risk_category == CROWD_SERVICE
    assert crowd_lagroup.secondary_label == "拉群获客"

    crowd_powder = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "crowd-5",
            "content_text": "全平台打粉，全品类粉，活粉僵尸粉都可接，粉价稳定，欢迎对接。",
            "matched_themes": ["众包任务"],
            "matched_keywords": ["打粉"],
        }
    )
    assert crowd_powder.risk_category == CROWD_SERVICE
    assert crowd_powder.secondary_label == "打粉卖量"

    crowd_machine = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "crowd-6",
            "content_text": "实力源头机房，WS 24小时接单，速度全网最快。",
            "matched_themes": ["众包任务"],
            "matched_keywords": ["接单"],
        }
    )
    assert crowd_machine.risk_category == CROWD_SERVICE
    assert crowd_machine.secondary_label == "拉群获客"

    tool_delivery = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "tool-2",
            "content_text": "浩瀚拉群端两种推荐配置，一次开控，永久使用。",
            "matched_themes": ["诈骗引流"],
            "matched_keywords": ["拉群"],
        }
    )
    assert tool_delivery.risk_category == TOOL_TRADING
    assert tool_delivery.secondary_label == "群控脚本"

    tool_update = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "tool-3",
            "content_text": "全能版独立群发版已更新，新增私信功能，旧版本停用，请下载最新软件。",
            "matched_themes": ["众包任务"],
            "matched_keywords": ["群发"],
        }
    )
    assert tool_update.risk_category == TOOL_TRADING
    assert tool_update.secondary_label == "群控脚本"

    order_issue = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "click-1",
            "content_text": "支付通道调整后出现卡单和支付失败，下单用户请联系客服处理退款和补发。",
            "matched_themes": ["刷单作弊"],
            "matched_keywords": ["卡单"],
        }
    )
    assert order_issue.risk_category == CLICK_FARMING
    assert order_issue.secondary_label == "订单卡单"
    assert order_issue.review_required is True

    gameplay_issue = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "click-2",
            "content_text": "荒野大镖客2卡单文件，支持单人局和战局玩法，注意模组封号风险。",
            "matched_themes": ["刷单作弊"],
            "matched_keywords": ["卡单"],
        }
    )
    assert gameplay_issue.risk_category == NORMAL_NOISE
    assert gameplay_issue.secondary_label == "低相关"
    assert gameplay_issue.review_required is False
    assert gameplay_issue.review_decision_reason == "ordinary_public_context_overrode_homonym_risk"

    brush_platform = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "click-3",
            "content_text": "东南亚补单真人测评在线接单，价格优惠。",
            "matched_themes": ["刷单作弊", "众包任务"],
            "matched_keywords": ["补单", "接单"],
        }
    )
    assert brush_platform.risk_category == CLICK_FARMING
    assert brush_platform.secondary_label == "刷单返佣"

    captcha = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "captcha-1",
            "content_text": "一手渠道港卡，接海外所有验证码业务，有需求的老板来盘。",
            "matched_themes": ["接码"],
            "matched_keywords": ["验证码"],
        }
    )
    assert captcha.risk_category == ACCOUNT_TRADING
    assert captcha.secondary_label == "接码注册"

    verified = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "acct-verified",
            "content_text": 'Affordable "verified account" for sale，支持实名认证账号过户。',
            "matched_themes": ["账号交易"],
            "matched_keywords": ["verified account"],
        }
    )
    assert verified.risk_category == ACCOUNT_TRADING
    assert verified.secondary_label == "实名账号买卖"

    tooling = FineGrainedIntentClassifier().classify(
        {
            "trace_id": "tool-1",
            "content_text": "火箭更新：群发任务新增过滤禁言账号选项，修复发送问题，查看教程请联系客服。",
            "matched_themes": ["众包任务", "工具交易"],
            "matched_keywords": ["群发"],
        }
    )
    assert tooling.risk_category == TOOL_TRADING
    assert tooling.secondary_label == "群控脚本"

    entities = AdvancedEntityExtractor().extract(
        {"trace_id": "hidden-1", "content_text": "暗号:DY888，落地 hxxps://risk[.]example/a，结算 USDT"}
    )
    assert any(item.extraction_method == "hidden_obfuscated_url" and item.normalized_value.startswith("https://") for item in entities)
    assert any(item.entity_type == "settlement" for item in entities)

    discovery = ComplianceSourceDiscovery()
    assert discovery.evaluate({"source_name": "login-only", "requires_login": True}).status == "REJECTED"
    assert discovery.evaluate({"source_name": "open", "robots_allowed": True, "terms_allow_security_research": True, "rate_limit_per_minute": 3}).status == "SCHEDULABLE"

    lifecycle = DynamicSlangLifecycleManager()
    lifecycle.nominate("音符", "抖音", ["trace-1"])
    lifecycle.review("音符", approved=True, reviewer="analyst")
    lifecycle.gray_rollout("音符", reviewer="analyst")
    assert lifecycle.activate("音符", reviewer="analyst").stage == lifecycle.ACTIVE
    lifecycle.ingest_review_decision(
        {
            "payload": {
                "decision": "MISREPORT",
                "source_trace_id": "trace-negative",
                "edits": {"corrected_entities": []},
            }
        }
    )
    assert lifecycle.negative_samples[0]["source_trace_id"] == "trace-negative"

    prompt_eval = PromptEvaluator().evaluate("bad_prompt", "return text only", [{}])
    assert prompt_eval.passed is False
    assert "JSON" in prompt_eval.missing_requirements


def test_multimodal_text_extractor_merges_nested_image_ocr_and_tracks_sources():
    record = {
        "trace_id": "mm-1",
        "content_text": "主文本：继续招代理",
        "images": [
            {"ocr_text": "海报写着 接码平台"},
            {"text_blocks": [{"text": "➕V联系 demo001"}]},
        ],
        "screenshots": [{"alt_text": "截图内容：拉裙上车"}],
    }

    materialized = MultimodalTextExtractor().materialize(record)

    assert "主文本" in materialized["content_text"]
    assert "继续招代理" in materialized["content_text"]
    assert "海报写着 接码平台" in materialized["content_text"]
    assert "➕V联系 demo001" in materialized["content_text"]
    assert "截图内容" in materialized["content_text"]
    assert "拉裙上车" in materialized["content_text"]
    assert materialized["multimodal_text_extracted"] is True
    assert materialized["multimodal_signal_count"] >= 3
    assert "images.ocr_text" in materialized["multimodal_text_sources"]
    assert "images.text_blocks.text" in materialized["multimodal_text_sources"]


def test_vector_and_graph_repositories_are_adapter_shaped():
    vector_repo = VectorRepo()
    vector_repo.upsert("a", "群控脚本 接码", {"source": "fixture"})
    vector_repo.upsert("b", "普通新闻报道", {"source": "fixture"})
    assert vector_repo.search("接码脚本", top_k=1)[0].item_id == "a"

    graph_repo = GraphRepo()
    graph_repo.upsert_node("sample:a", "risk_sample", {"trace_id": "a"})
    graph_repo.upsert_node("entity:tg", "contact", {"value": "tg"})
    graph_repo.add_edge("sample:a", "entity:tg", "HAS_ENTITY")
    assert graph_repo.neighbors("sample:a")[0].node_id == "entity:tg"


def test_phase_engine_can_expand_related_trace_ids_from_graph_neighbors():
    engine = PhaseTwoThreeEngine()
    engine.run(
        [
            {
                "trace_id": "graph-expand-1",
                "source_name": "tg-authorized-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:expand01，落地 https://expand.example/path 第一条",
            },
            {
                "trace_id": "graph-expand-2",
                "source_name": "forum-authorized-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T02:00:00+00:00",
                "content_text": "普通文本但联系 TG:expand01，落地 https://expand.example/path 第二条",
            },
            {
                "trace_id": "graph-expand-3",
                "source_name": "feed-authorized-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-28T03:00:00+00:00",
                "content_text": "普通文本但指向 https://expand.example/path 第三条",
            },
        ]
    )

    expanded = engine.expand_related_trace_ids(["graph-expand-1"], limit=6)

    assert "graph-expand-1" in expanded
    assert "graph-expand-2" in expanded
    assert "graph-expand-3" in expanded


def test_reviewed_runtime_slang_updates_phase_engine_entity_normalization():
    lifecycle = DynamicSlangLifecycleManager()
    lifecycle.ingest_review_decision(
        {
            "payload": {
                "decision": "APPROVED",
                "source_trace_id": "review-1",
                "reviewer": "analyst",
                "edits": {
                    "add_to_wordlist": True,
                    "edited_risk_type": "账号交易",
                    "corrected_entities": [
                        {
                            "entity_value": "火苗",
                            "normalized_value": "WhatsApp",
                        }
                    ],
                },
            }
        }
    )
    lifecycle.gray_rollout("火苗", reviewer="analyst")
    lifecycle.activate("火苗", reviewer="analyst")
    engine = PhaseTwoThreeEngine(lifecycle_manager=lifecycle)

    result = engine.run(
        [
            {
                "trace_id": "runtime-slang-1",
                "source_name": "tg-runtime-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "content_text": "火苗联系 handle01，欢迎上车。",
            }
        ]
    )
    payload = result.model_dump()

    assert any(
        item["entity_type"] == "slang_term"
        and item["entity_value"] == "火苗"
        and item["normalized_value"] == "WhatsApp"
        for item in payload["entities"]
    )
    runtime_context = engine.runtime_prompt_context(label="账号交易")
    assert runtime_context["slang_terms_mapping"]["火苗"] == "WhatsApp"
    assert runtime_context["few_shot_examples"][0]["term"] == "火苗"


def test_exploration_agent_uses_runtime_slang_candidates_and_normalized_term():
    lifecycle = DynamicSlangLifecycleManager()
    lifecycle.ingest_review_decision(
        {
            "payload": {
                "decision": "APPROVED",
                "source_trace_id": "review-2",
                "reviewer": "analyst",
                "edits": {
                    "add_to_wordlist": True,
                    "edited_risk_type": "诈骗引流",
                    "corrected_entities": [
                        {
                            "entity_value": "火苗",
                            "normalized_value": "WhatsApp",
                        }
                    ],
                },
            }
        }
    )
    lifecycle.gray_rollout("火苗", reviewer="analyst")
    lifecycle.activate("火苗", reviewer="analyst")
    engine = PhaseTwoThreeEngine(lifecycle_manager=lifecycle)
    context = engine.runtime_prompt_context(label="诈骗引流")
    context["history"] = [{"trace_id": "history-1", "content_text": "以前也有人说火苗联系。"}]

    hypothesis = ExplorationAgent().analyze(
        raw={"trace_id": "explore-1", "content_text": "最新样本：火苗联系我。"},
        context=context,
    )

    assert hypothesis.hypothesis_type.value == "NEW_SLANG_VARIANT"
    assert hypothesis.suggested_normalized_term == {"raw": "火苗", "target": "WhatsApp"}
    assert "历史复核样本" in hypothesis.hypothesis_summary


def test_advanced_pipeline_local_runtime_smoke():
    runtime = LocalAgentRuntime()
    try:
        payload = runtime.run_advanced_pipeline(
            phase_records(),
            prompt_text="Return JSON with confidence evidence requires_human_review",
        )
        search_payload = runtime.semantic_search("群控脚本", top_k=1)
    finally:
        runtime.close()

    assert payload["mode"] == "phase2_phase3_enhancement"
    assert payload["risk_clue_count"] >= 2
    assert payload["playbook_count"] == 1
    assert payload["strategy_count"] >= 3
    assert search_payload["count"] == 1
