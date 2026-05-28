from __future__ import annotations

import unittest

from src.classifier.nlp_rule_matcher import CLICK_FARMING, CROWD_SERVICE, TOOL_TRADING, RuleFastTrackClassifier
from src.cleaner.pipeline import CleanerPipeline
from src.cleaner.text_filter import (
    MAX_CLEAN_TEXT_CHARS,
    is_blank_or_garbled,
    normalize_intel_text,
    normalize_text,
    text_similarity,
)
from src.collector.im_collector import MockCollector
from src.collector.relevance import (
    DEFAULT_BLACKGRAY_INCLUDE_KEYWORDS,
    decide_text_relevance,
    get_theme_search_variants,
    get_theme_search_terms,
    load_theme_synonym_registry,
)
from src.extractor.entity_extractor import CONTACT, TOOL_NAME, URL, BasicEntityExtractor


TRACE_1 = "00000000-0000-0000-0000-000000000001"
TRACE_2 = "00000000-0000-0000-0000-000000000002"
TRACE_3 = "00000000-0000-0000-0000-000000000003"


def _field(obj, name):
    if hasattr(obj, "model_dump"):
        return obj.model_dump().get(name)
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name)


class TextFilterPipelineTest(unittest.TestCase):
    def test_mock_collector_streams_fixture_as_raw_intelligence(self) -> None:
        rows = list(MockCollector("tests/fixtures/sample_raw.jsonl").stream())

        self.assertEqual(6, len(rows))
        self.assertEqual(TRACE_1, str(_field(rows[0], "trace_id")))
        self.assertTrue(_field(rows[0], "hash_id"))
        self.assertIn("刷单兼职", _field(rows[0], "content_text"))

    def test_cleaner_filters_garbage_and_groups_exact_and_near_duplicates(self) -> None:
        rows = list(MockCollector("tests/fixtures/sample_raw.jsonl").stream())
        result = CleanerPipeline().clean(rows)

        self.assertEqual(3, len(result.cleaned))
        dropped_reasons = [item.reason for item in result.dropped]
        self.assertEqual(2, dropped_reasons.count("duplicate"))
        self.assertIn("blank_or_garbled", dropped_reasons)

        duplicate_drops = [item for item in result.dropped if item.reason == "duplicate"]
        self.assertEqual(1, len({item.dedup_group_id for item in duplicate_drops}))
        first_group = _field(result.cleaned[0], "dedup_group_id")
        self.assertEqual(first_group, duplicate_drops[0].dedup_group_id)
        self.assertEqual([TRACE_1, TRACE_2, TRACE_3], result.dedup_groups[first_group])
        self.assertGreater(_field(result.cleaned[0], "quality_score"), 0.6)
        self.assertIn(_field(result.cleaned[0], "risk_level"), {"HIGH", "CRITICAL"})
        self.assertTrue({"诈骗引流", "刷单作弊"} & set(_field(result.cleaned[0], "risk_categories")))

    def test_cleaner_truncates_long_text_to_4000_chars(self) -> None:
        long_text = "刷单兼职日结返佣，联系微信 vxLong2026。" + ("安全字符" * 1600)
        raw = {
            "trace_id": "raw-long",
            "content_text": long_text,
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
        }

        result = CleanerPipeline().clean([raw])

        self.assertEqual(1, len(result.cleaned))
        self.assertEqual(MAX_CLEAN_TEXT_CHARS, len(_field(result.cleaned[0], "clean_text")))
        self.assertEqual("raw-long", _field(result.cleaned[0], "source_trace_id"))

    def test_cleaner_filters_semantic_noise_and_keeps_high_risk_corpus(self) -> None:
        rows = [
            {
                "trace_id": "guide-noise",
                "content_text": "分享2025年8个实用的接码平台使用指南，推荐收藏，帮助你快速注册和选择建议。",
                "matched_themes": ["接码"],
            },
            {
                "trace_id": "defensive-noise",
                "content_text": "警方通报提醒：所谓接码平台和刷单返佣均涉嫌违法，请勿参与。",
                "matched_keywords": ["接码", "刷单", "返佣"],
            },
            {
                "trace_id": "risk-keep",
                "content_text": "接码平台继续放单，支持批量注册，联系 TG:captcha01，落地 https://risk.example/captcha",
                "matched_themes": ["接码"],
                "matched_keywords": ["接码", "批量注册"],
            },
        ]

        result = CleanerPipeline().clean(rows)

        self.assertEqual(1, len(result.cleaned))
        self.assertEqual("risk-keep", _field(result.cleaned[0], "source_trace_id"))
        self.assertIn(_field(result.cleaned[0], "risk_level"), {"HIGH", "CRITICAL"})
        self.assertGreater(_field(result.cleaned[0], "risk_score"), 0.6)
        self.assertGreater(_field(result.cleaned[0], "quality_score"), 0.7)

        dropped_reasons = {item.source_trace_id: item.reason for item in result.dropped}
        self.assertEqual("generic_guide_noise", dropped_reasons["guide-noise"])
        self.assertEqual("defensive_context_noise", dropped_reasons["defensive-noise"])

    def test_text_filter_helpers_cover_blank_garbage_and_similarity(self) -> None:
        self.assertEqual("招募 刷单", normalize_text(" 招募\t刷单\n"))
        self.assertTrue(is_blank_or_garbled("����%%%%@@@@"))
        self.assertFalse(is_blank_or_garbled("招募刷单兼职，日结返佣"))
        self.assertGreaterEqual(
            text_similarity("招募刷单兼职，日结返佣，联系微信 vxTask2026", "招募刷单兼职 日结返佣 联系微信 vxTask2026!!!"),
            0.92,
        )
        decision = decide_text_relevance(
            "接码平台继续放单，日结返佣；警方通报不要碰。",
            include_keywords=("接码", "放单", "返佣"),
            exclude_keywords=("警方通报",),
            min_keyword_hits=2,
        )
        self.assertFalse(decision.relevant)
        self.assertEqual(("接码", "放单", "返佣"), decision.matched_keywords)
        self.assertEqual(("警方通报",), decision.excluded_keywords)

    def test_rule_classifier_and_entity_extractor_cover_backbone_fast_path(self) -> None:
        rows = list(MockCollector("tests/fixtures/sample_raw.jsonl").stream())
        cleaned = CleanerPipeline().clean(rows).cleaned

        classifications = RuleFastTrackClassifier().classify_batch(cleaned)
        categories = [_field(item, "risk_category") for item in classifications]
        self.assertIn(CLICK_FARMING, categories)
        self.assertIn(TOOL_TRADING, categories)

        entities = [entity for batch in BasicEntityExtractor().extract_batch(cleaned) for entity in batch]
        entity_types = {_field(entity, "entity_type") for entity in entities}
        entity_values = {_field(entity, "entity_value") for entity in entities}
        self.assertIn(URL, entity_types)
        self.assertIn(CONTACT, entity_types)
        self.assertIn(TOOL_NAME, entity_types)
        self.assertIn("vxTask2026", entity_values)
        self.assertIn("群控", entity_values)

    def test_entity_extractor_handles_homophone_and_emoji_contact_variants(self) -> None:
        entities = BasicEntityExtractor().extract(
            {
                "trace_id": "variant-contact-1",
                "content_text": "加薇 Alphaabc6，纸飞机 @plane777，🐧 1234567",
            }
        )

        values = {_field(entity, "entity_value") for entity in entities}
        self.assertIn("Alphaabc6", values)
        self.assertIn("plane777", values)
        self.assertIn("1234567", values)

    def test_rule_classifier_can_promote_crowd_service_from_service_delivery_markers(self) -> None:
        result = RuleFastTrackClassifier().classify(
            {
                "trace_id": "crowd-rule-1",
                "content_text": "TG私信代发业务上线，支持用户名和手机号，提供结果回执，欢迎老板下单合作。",
                "matched_themes": ["众包任务"],
                "matched_keywords": ["代发", "接单"],
            }
        )

        self.assertEqual(CROWD_SERVICE, _field(result, "risk_category"))
        self.assertTrue(_field(result, "review_required"))

    def test_rule_classifier_recognizes_tool_delivery_and_verified_account_markers(self) -> None:
        tool_result = RuleFastTrackClassifier().classify(
            {
                "trace_id": "tool-rule-2",
                "content_text": "浩瀚拉群端推荐配置，一次开控永久使用，后台可自动注册账号。",
                "matched_themes": ["诈骗引流"],
                "matched_keywords": ["拉群"],
            }
        )
        self.assertEqual(TOOL_TRADING, _field(tool_result, "risk_category"))

        account_result = RuleFastTrackClassifier().classify(
            {
                "trace_id": "account-rule-2",
                "content_text": 'Affordable "verified account" for sale，支持实名认证。',
                "matched_themes": ["账号交易"],
                "matched_keywords": ["verified account"],
            }
        )
        self.assertEqual("账号交易", _field(account_result, "risk_category"))

    def test_default_blackgray_keywords_cover_new_fraud_account_and_crowdsourcing_rules(self) -> None:
        decision = decide_text_relevance(
            "诈骗引流话术，私聊进群拉新；支持账号买卖、卖号收号，也接众包任务接单。",
            include_keywords=DEFAULT_BLACKGRAY_INCLUDE_KEYWORDS,
            exclude_keywords=("警方通报", "反诈"),
            min_keyword_hits=3,
        )

        self.assertTrue(decision.relevant)
        self.assertEqual("keyword_relevance_v6", decision.policy_version)
        self.assertIn("诈骗引流", decision.matched_keywords)
        self.assertIn("私聊进群", decision.matched_keywords)
        self.assertIn("账号买卖", decision.matched_keywords)
        self.assertIn("卖号", decision.matched_keywords)
        self.assertIn("众包任务", decision.matched_keywords)
        self.assertIn("接单", decision.matched_keywords)

    def test_theme_synonyms_can_match_black_slang_without_exact_topic_word(self) -> None:
        decision = decide_text_relevance(
            "这边长期做私域拉新和高佣导流，也收老号白号料子，飞机群里继续接任务。",
            include_themes=("诈骗引流", "账号交易", "众包任务"),
            exclude_keywords=("警方通报", "反诈"),
            min_keyword_hits=4,
        )

        self.assertTrue(decision.relevant)
        self.assertEqual(("诈骗引流", "账号交易", "众包任务"), decision.matched_themes)
        self.assertIn("私域", decision.matched_keywords)
        self.assertIn("高佣", decision.matched_keywords)
        self.assertIn("老号", decision.matched_keywords)
        self.assertIn("白号", decision.matched_keywords)
        self.assertIn("料子", decision.matched_keywords)
        self.assertIn("接任务", decision.matched_keywords)

    def test_requested_fraud_and_crowdsourcing_terms_match_theme_synonyms(self) -> None:
        decision = decide_text_relevance(
            "这边走私域导流，加V拉群，高佣转化；另外接 userbot automation 群发、采集群成员和拉人任务。",
            include_themes=("诈骗引流", "众包任务"),
            exclude_keywords=("警方通报", "反诈"),
            min_keyword_hits=6,
        )

        self.assertTrue(decision.relevant)
        self.assertEqual(("诈骗引流", "众包任务"), decision.matched_themes)
        self.assertIn("私域导流", decision.matched_keywords)
        self.assertIn("加v", tuple(keyword.lower() for keyword in decision.matched_keywords))
        self.assertIn("拉群", decision.matched_keywords)
        self.assertIn("高佣", decision.matched_keywords)
        self.assertIn("userbot", tuple(keyword.lower() for keyword in decision.matched_keywords))
        self.assertIn("automation", tuple(keyword.lower() for keyword in decision.matched_keywords))
        self.assertIn("群发", decision.matched_keywords)
        self.assertIn("采集群成员", decision.matched_keywords)
        self.assertIn("拉人", decision.matched_keywords)

    def test_homophone_and_emoji_variants_can_match_relevance_rules(self) -> None:
        decision = decide_text_relevance(
            "这边继续私域导流，➕V后拉裙，小飞机里发暗号上车。",
            include_themes=("诈骗引流",),
            exclude_keywords=("警方通报", "反诈"),
            min_keyword_hits=3,
        )

        self.assertTrue(decision.relevant)
        self.assertEqual(("诈骗引流",), decision.matched_themes)
        self.assertIn("私域导流", decision.matched_keywords)
        self.assertIn("加v", tuple(keyword.lower() for keyword in decision.matched_keywords))
        self.assertIn("拉群", decision.matched_keywords)

    def test_spaced_ocr_like_variants_are_normalized_for_matching(self) -> None:
        normalized = normalize_intel_text("图里写着 加 微进 裙，纸 飞 机和音 符暗号联系。")

        self.assertIn("加v", normalized.lower())
        self.assertIn("进群", normalized)
        self.assertIn("纸飞机", normalized)
        self.assertIn("音符", normalized)

        decision = decide_text_relevance(
            "图里写着 加 微进 裙，纸 飞 机和音 符暗号联系。",
            include_themes=("诈骗引流",),
            exclude_keywords=("警方通报", "反诈"),
            min_keyword_hits=3,
        )

        self.assertTrue(decision.relevant)
        self.assertEqual(("诈骗引流",), decision.matched_themes)
        lowered_keywords = tuple(keyword.lower() for keyword in decision.matched_keywords)
        self.assertIn("加v", lowered_keywords)
        self.assertIn("进裙", decision.matched_keywords)

    def test_low_value_guides_and_recommendations_can_be_excluded(self) -> None:
        decision = decide_text_relevance(
            "分享2025年8个实用的接码平台使用指南，推荐收藏，帮助你快速注册和选择建议。",
            include_themes=("接码",),
            exclude_keywords=("推荐", "指南", "实用", "收藏", "注册"),
            min_keyword_hits=1,
        )

        self.assertFalse(decision.relevant)
        self.assertIn("接码平台", decision.matched_keywords)
        self.assertEqual(("推荐", "指南", "实用", "收藏", "注册"), decision.excluded_keywords)

    def test_theme_search_terms_are_loaded_from_external_config(self) -> None:
        terms = get_theme_search_terms("账号交易", limit=3)

        self.assertEqual(("收号", "实名号", "号商"), terms)

    def test_requested_brush_order_black_slang_chain_is_loaded_for_search(self) -> None:
        terms = get_theme_search_terms("刷单作弊", limit=5)

        self.assertEqual(("卡单", "手工单", "垫付单", "补单", "做单"), terms)

    def test_theme_search_variants_can_expose_second_wave_collection_terms(self) -> None:
        variants = get_theme_search_variants("诈骗引流", limit=6)

        self.assertEqual("私域导流", variants[0]["term"])
        self.assertEqual("core", variants[0]["stage"])
        self.assertEqual("加薇", variants[4]["term"])
        self.assertEqual("variant", variants[4]["stage"])

    def test_theme_search_variants_cover_black_slang_emoji_and_ocr_friendly_terms(self) -> None:
        variants = get_theme_search_variants("诈骗引流", limit=20)
        terms = tuple(item["term"] for item in variants)

        self.assertIn("进裙", terms)
        self.assertIn("纸飞机", terms)
        self.assertIn("音符", terms)


    def test_telegram_theme_has_been_removed_from_external_synonym_config(self) -> None:
        registry = load_theme_synonym_registry()

        self.assertNotIn("Telegram", registry)


if __name__ == "__main__":
    unittest.main()
