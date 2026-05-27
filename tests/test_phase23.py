from fastapi.testclient import TestClient

from main import create_app
from src.enhancement.engine import PhaseTwoThreeEngine
from src.enhancement.lifecycle import DynamicSlangLifecycleManager, PromptEvaluator
from src.enhancement.source_intake import ComplianceSourceDiscovery, MultimodalTextExtractor
from src.enhancement.text_intelligence import AdaptiveEntropyFilter, AdvancedEntityExtractor, FineGrainedIntentClassifier
from src.classifier.nlp_rule_matcher import ACCOUNT_TRADING, CLICK_FARMING, CROWD_SERVICE, TOOL_TRADING
from storage import GraphRepo, VectorRepo


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
    assert gameplay_issue.risk_category == CLICK_FARMING
    assert gameplay_issue.secondary_label == "卡单玩法"
    assert gameplay_issue.review_required is True

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


def test_advanced_pipeline_api_smoke():
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/pipeline/advanced/run",
        json={
            "fixture_items": phase_records(),
            "prompt_text": "Return JSON with confidence evidence requires_human_review",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "phase2_phase3_enhancement"
    assert payload["risk_clue_count"] >= 2
    assert payload["playbook_count"] == 1
    assert payload["strategy_count"] >= 3

    search_response = client.get("/api/v1/semantic/search", params={"query": "群控脚本", "top_k": 1})
    assert search_response.status_code == 200
    assert search_response.json()["count"] == 1
