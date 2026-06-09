from src.enhancement.clue_quality import ClueQualityEvaluator, build_evidence_reviewability
from src.enhancement.strategy import RiskClueAggregator
from src.pipeline.stages import ScoreStage


def test_clue_quality_exposes_freshness_score_and_reason():
    assessment = ClueQualityEvaluator().evaluate_one(
        {
            "clue_id": "fresh-clue",
            "confidence": 0.85,
            "evidence_trace_ids": ["r1", "r2", "r3"],
            "source_names": ["tg-a", "forum-b"],
            "entity_values": ["TG:fresh001"],
            "last_seen": "2026-06-07T08:00:00+00:00",
            "quality_reference_time": "2026-06-07T12:00:00+00:00",
        },
        conf_by_trace={"r1": 0.82, "r2": 0.8, "r3": 0.78},
        entities_by_trace={
            "r1": [{"entity_type": "contact", "normalized_value": "fresh001"}],
            "r2": [{"entity_type": "url", "normalized_value": "https://fresh.example/a"}],
        },
        quality_profile="balanced",
        require_cross_source=True,
        require_evidence_chain=True,
    )

    dumped = assessment.model_dump()

    assert assessment.freshness_score == 1.0
    assert dumped["freshness_score"] == 1.0
    assert assessment.false_positive_risk_score < 0.4
    assert "fresh_evidence_window" in assessment.quality_reasons
    assert "false_positive_risk_low" in dumped["false_positive_risk_reasons"]


def test_clue_quality_penalizes_stale_weak_entity_false_positive_risk():
    assessment = ClueQualityEvaluator().evaluate_one(
        {
            "clue_id": "stale-weak-clue",
            "confidence": 0.86,
            "evidence_trace_ids": ["r1"],
            "source_names": ["single-source"],
            "entity_values": [],
            "last_seen": "2026-05-20T12:00:00+00:00",
            "quality_reference_time": "2026-06-07T12:00:00+00:00",
        },
        conf_by_trace={"r1": 0.45},
        entities_by_trace={},
        quality_profile="balanced",
        require_cross_source=True,
        require_evidence_chain=True,
    )

    dumped = assessment.model_dump()

    assert assessment.freshness_score == 0.25
    assert assessment.false_positive_risk_score >= 0.7
    assert assessment.pass_threshold is False
    assert "stale_evidence_window" in dumped["freshness_reasons"]
    assert "single_source_false_positive_risk" in dumped["false_positive_risk_reasons"]
    assert "weak_entity_support_false_positive_risk" in dumped["false_positive_risk_reasons"]


def test_clue_quality_matches_classifications_by_trace_id_fallback():
    assessment = ClueQualityEvaluator().evaluate_many(
        [
            {
                "clue_id": "trace-id-clue",
                "confidence": 0.86,
                "evidence_trace_ids": ["trace-a", "trace-b"],
                "source_names": ["tg-a", "forum-b"],
                "entity_values": ["TG:traceid"],
                "last_seen": "2026-06-07T08:00:00+00:00",
                "quality_reference_time": "2026-06-07T12:00:00+00:00",
            }
        ],
        classifications=[
            {"trace_id": "trace-a", "risk_category": "工具交易", "confidence": 0.8},
            {"trace_id": "trace-b", "risk_category": "工具交易", "confidence": 0.7},
        ],
        entities=[],
        quality_profile="balanced",
        require_cross_source=True,
        require_evidence_chain=True,
    )[0]

    assert assessment.avg_classification_confidence == 0.75
    assert "classification_confidence_stable" in assessment.quality_reasons


def test_score_stage_adds_reviewability_metadata_for_weak_single_source_clue():
    scored = ScoreStage().run_batch(
        [
            {
                "clue_id": "weak-review",
                "clue_type": "shared_contact_48h",
                "key": "unknown-contact",
                "risk_category": "工具交易",
                "confidence": 0.72,
                "evidence_trace_ids": ["weak-r1"],
                "source_names": ["single-source"],
                "entity_values": [],
                "quality_reference_time": "2026-06-07T12:00:00+00:00",
            }
        ],
        context={
            "classifications": [
                {"trace_id": "weak-r1", "risk_category": "工具交易", "confidence": 0.42},
            ],
            "entities": [],
            "quality_profile": "balanced",
            "require_cross_source": True,
            "require_evidence_chain": True,
        },
    )

    reviewability = scored[0]["evidence_reviewability"]

    assert reviewability["source_count"] == 1
    assert reviewability["entity_support_count"] == 0
    assert reviewability["original_snippets"] == []
    assert reviewability["time_range"] == {"start": None, "end": None}
    assert reviewability["false_positive_risk"]["level"] == "high"
    assert reviewability["false_positive_risk"]["score"] >= 0.7
    assert reviewability["suggested_review_action"] == "human_verify_single_source_or_weak_entity_support"
    assert "verify_original_snippets_missing" in reviewability["review_action_reasons"]
    assert "verify_observed_time_missing" in reviewability["review_action_reasons"]


def test_score_stage_adds_reviewability_metadata_for_multi_source_entity_supported_clue():
    scored = ScoreStage().run_batch(
        [
            {
                "clue_id": "strong-review",
                "clue_type": "shared_contact_48h",
                "key": "TG:core01",
                "risk_category": "工具交易",
                "confidence": 0.9,
                "evidence_trace_ids": ["strong-r1", "strong-r2", "strong-r3"],
                "source_names": ["tg-a", "forum-b"],
                "entity_values": ["TG:core01", "risk.example"],
                "original_snippets": [
                    "群控脚本接码 TG:core01",
                    "论坛复现风险域名 risk.example",
                ],
                "first_seen": "2026-06-07T08:00:00+00:00",
                "last_seen": "2026-06-07T10:00:00+00:00",
                "quality_reference_time": "2026-06-07T12:00:00+00:00",
            }
        ],
        context={
            "classifications": [
                {"trace_id": "strong-r1", "risk_category": "工具交易", "confidence": 0.86},
                {"trace_id": "strong-r2", "risk_category": "工具交易", "confidence": 0.82},
                {"trace_id": "strong-r3", "risk_category": "工具交易", "confidence": 0.8},
            ],
            "entities": [
                {"source_trace_id": "strong-r1", "entity_type": "contact", "normalized_value": "core01"},
                {"source_trace_id": "strong-r2", "entity_type": "domain", "normalized_value": "risk.example"},
                {"source_trace_id": "strong-r3", "entity_type": "tool_name", "normalized_value": "群控脚本"},
            ],
            "quality_profile": "balanced",
            "require_cross_source": True,
            "require_evidence_chain": True,
        },
    )

    reviewability = scored[0]["evidence_reviewability"]

    assert reviewability["source_count"] == 2
    assert reviewability["entity_support_count"] >= 2
    assert reviewability["original_snippets"] == [
        "群控脚本接码 TG:core01",
        "论坛复现风险域名 risk.example",
    ]
    assert reviewability["time_range"] == {
        "start": "2026-06-07T08:00:00+00:00",
        "end": "2026-06-07T10:00:00+00:00",
    }
    assert reviewability["false_positive_risk"]["level"] == "low"
    assert reviewability["false_positive_risk"]["score"] < 0.4
    assert reviewability["suggested_review_action"] == "review_original_snippets_and_confirm_entity_linkage"


def test_score_stage_adds_trace_evidence_cards_for_clue_review():
    scored = ScoreStage().run_batch(
        [
            {
                "clue_id": "card-review",
                "clue_type": "shared_invite_code_multi_source",
                "key": "KM-MH-15",
                "risk_category": "账号交易",
                "confidence": 0.9,
                "evidence_trace_ids": ["card-r1", "card-r2"],
                "source_names": ["tg-card", "forum-card"],
                "entity_values": ["KM-MH-15"],
                "first_seen": "2026-06-07T08:00:00+00:00",
                "last_seen": "2026-06-07T10:00:00+00:00",
            }
        ],
        context={
            "classifications": [
                {
                    "source_trace_id": "card-r1",
                    "risk_category": "账号交易",
                    "secondary_label": "卡密交易",
                    "confidence": 0.86,
                },
                {
                    "trace_id": "card-r2",
                    "risk_category": "账号交易",
                    "secondary_label": "卡密交易",
                    "confidence": 0.82,
                },
            ],
            "entities": [
                {"source_trace_id": "card-r1", "entity_type": "invite_code", "normalized_value": "KM-MH-15"},
                {"source_trace_id": "card-r1", "entity_type": "contact", "normalized_value": "Telegram:card01"},
                {"source_trace_id": "card-r2", "entity_type": "invite_code", "normalized_value": "KM-MH-15"},
            ],
            "records": [
                {
                    "trace_id": "card-r1",
                    "source_name": "tg-card",
                    "source_type": "IM",
                    "publish_time": "2026-06-07T08:00:00+00:00",
                    "content_text": "原始文本：卡密邀请码 KM-MH-15 联系 TG:card01",
                    "clean_text": "卡密邀请码 KM-MH-15 联系 TG:card01",
                },
                {
                    "trace_id": "card-r2",
                    "source_name": "forum-card",
                    "source_type": "Forum",
                    "publish_time": "2026-06-07T10:00:00+00:00",
                    "content_text": "论坛原文：同款邀请码 KM-MH-15",
                    "clean_text": "同款邀请码 KM-MH-15",
                },
            ],
            "quality_profile": "balanced",
            "require_cross_source": True,
            "require_evidence_chain": True,
        },
    )

    reviewability = scored[0]["evidence_reviewability"]
    cards = reviewability["evidence_cards"]

    assert scored[0]["evidence_cards"] == cards
    assert [card["trace_id"] for card in cards] == ["card-r1", "card-r2"]
    assert cards[0]["raw_snippet"] == "原始文本：卡密邀请码 KM-MH-15 联系 TG:card01"
    assert cards[0]["clean_text"] == "卡密邀请码 KM-MH-15 联系 TG:card01"
    assert cards[0]["classification"]["risk_category"] == "账号交易"
    assert cards[0]["classification"]["secondary_label"] == "卡密交易"
    assert {entity["entity_type"] for entity in cards[0]["entities"]} == {"invite_code", "contact"}
    assert cards[0]["source_name"] == "tg-card"
    assert cards[1]["source_type"] == "Forum"


def test_evidence_reviewability_uses_record_times_before_clue_created_at():
    reviewability = build_evidence_reviewability(
        {
            "clue_id": "record-time-review",
            "clue_type": "shared_contact_48h",
            "key": "Telegram:core01",
            "evidence_trace_ids": ["record-time-a", "record-time-b"],
            "source_names": ["tg-a", "forum-b"],
            "entity_values": ["Telegram:core01"],
            "created_at": "2026-06-08T06:43:39+00:00",
        },
        records=[
            {"trace_id": "record-time-a", "publish_time": "2026-05-31T01:00:00+00:00"},
            {"trace_id": "record-time-b", "publish_time": "2026-05-31T02:00:00+00:00"},
        ],
    )

    assert reviewability["time_range"] == {
        "start": "2026-05-31T01:00:00+00:00",
        "end": "2026-05-31T02:00:00+00:00",
    }


def test_contact_clue_aggregator_merges_telegram_prefixed_and_bare_handles():
    records = [
        {"trace_id": "contact-alias-1", "source_name": "tg-a", "publish_time": "2026-06-07T01:00:00+00:00"},
        {"trace_id": "contact-alias-2", "source_name": "forum-a", "publish_time": "2026-06-07T02:00:00+00:00"},
        {"trace_id": "contact-alias-3", "source_name": "feed-a", "publish_time": "2026-06-07T03:00:00+00:00"},
    ]
    classifications = [
        {"source_trace_id": "contact-alias-1", "risk_category": "工具交易"},
        {"source_trace_id": "contact-alias-2", "risk_category": "工具交易"},
        {"source_trace_id": "contact-alias-3", "risk_category": "工具交易"},
    ]
    entities = [
        {"source_trace_id": "contact-alias-1", "entity_type": "contact", "normalized_value": "Telegram:core01"},
        {"source_trace_id": "contact-alias-2", "entity_type": "contact", "normalized_value": "TG:core01"},
        {"source_trace_id": "contact-alias-3", "entity_type": "contact", "normalized_value": "core01"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    contact_clue = next(item for item in clues if item.clue_type == "shared_contact_48h")
    assert contact_clue.key == "Telegram:core01"
    assert contact_clue.evidence_trace_ids == ["contact-alias-1", "contact-alias-2", "contact-alias-3"]


def test_risk_clue_aggregator_emits_shared_invite_code_multi_source():
    records = [
        {"trace_id": "invite-a", "source_name": "tg-invite", "publish_time": "2026-06-07T01:00:00+00:00"},
        {"trace_id": "invite-b", "source_name": "forum-invite", "publish_time": "2026-06-07T02:00:00+00:00"},
    ]
    classifications = [
        {"source_trace_id": "invite-a", "risk_category": "账号交易"},
        {"source_trace_id": "invite-b", "risk_category": "账号交易"},
    ]
    entities = [
        {"source_trace_id": "invite-a", "entity_type": "invite_code", "normalized_value": "KM-MH-15"},
        {"source_trace_id": "invite-b", "entity_type": "invite_code", "normalized_value": "KM-MH-15"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    invite_clue = next(item for item in clues if item.clue_type == "shared_invite_code_multi_source")
    assert invite_clue.key == "KM-MH-15"
    assert invite_clue.evidence_trace_ids == ["invite-a", "invite-b"]
    assert invite_clue.source_names == ["forum-invite", "tg-invite"]
    assert invite_clue.entity_values == ["KM-MH-15"]
    assert invite_clue.risk_category == "账号交易"
    assert invite_clue.confidence > 0
    assert invite_clue.threshold_reason == "same_invite_code_appears_in_at_least_2_sources"


def test_risk_clue_aggregator_bridges_singleton_invite_with_authorized_corroboration():
    records = [
        {
            "trace_id": "invite-bridge-a",
            "source_name": "authorized-forum",
            "source_type": "Forum",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "授权样本：账号交易贴公开邀请码 INV-MH-01，人工标注为可复核线索。",
        },
        {
            "trace_id": "invite-bridge-b",
            "source_name": "authorized-feed",
            "source_type": "THREAT_INTEL",
            "publish_time": "2026-06-07T01:40:00+00:00",
            "content_text": "授权样本：证据链来自授权 IM 与情报源，人工确认同一账号交易线索。",
        },
    ]
    classifications = [
        {"source_trace_id": "invite-bridge-a", "risk_category": "账号交易"},
        {"source_trace_id": "invite-bridge-b", "risk_category": "账号交易"},
    ]
    entities = [
        {"source_trace_id": "invite-bridge-a", "entity_type": "invite_code", "normalized_value": "INV-MH-01"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    invite_clue = next(item for item in clues if item.clue_type == "shared_invite_code_multi_source")
    assert invite_clue.key == "INV-MH-01"
    assert invite_clue.evidence_trace_ids == ["invite-bridge-a", "invite-bridge-b"]
    assert invite_clue.source_names == ["authorized-feed", "authorized-forum"]
    assert invite_clue.entity_values == ["INV-MH-01"]
    assert invite_clue.threshold_reason == "single_identifier_with_authorized_cross_source_corroboration"


def test_risk_clue_aggregator_does_not_bridge_singleton_invite_without_corroboration():
    records = [
        {
            "trace_id": "invite-single-a",
            "source_name": "forum",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "账号交易贴公开邀请码 INV-MH-01。",
        },
        {
            "trace_id": "invite-single-b",
            "source_name": "feed",
            "publish_time": "2026-06-07T01:40:00+00:00",
            "content_text": "普通账号交易讨论，没有复核、授权或证据链描述。",
        },
    ]
    classifications = [
        {"source_trace_id": "invite-single-a", "risk_category": "账号交易"},
        {"source_trace_id": "invite-single-b", "risk_category": "账号交易"},
    ]
    entities = [
        {"source_trace_id": "invite-single-a", "entity_type": "invite_code", "normalized_value": "INV-MH-01"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    assert "shared_invite_code_multi_source" not in {item.clue_type for item in clues}


def test_risk_clue_aggregator_prefers_forward_corroboration_when_distance_ties():
    records = [
        {
            "trace_id": "invite-forward-a",
            "source_name": "authorized-prev",
            "source_type": "Forum",
            "publish_time": "2026-06-07T00:20:00+00:00",
            "content_text": "授权样本：前序证据链来自授权记录，人工确认同一账号交易线索。",
        },
        {
            "trace_id": "invite-forward-b",
            "source_name": "authorized-middle",
            "source_type": "IM",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "授权样本：账号交易贴公开邀请码 INV-MH-02，人工标注为可复核线索。",
        },
        {
            "trace_id": "invite-forward-c",
            "source_name": "authorized-next",
            "source_type": "THREAT_INTEL",
            "publish_time": "2026-06-07T01:40:00+00:00",
            "content_text": "授权样本：后序证据链来自授权记录，人工确认同一账号交易线索。",
        },
    ]
    classifications = [
        {"source_trace_id": "invite-forward-a", "risk_category": "账号交易"},
        {"source_trace_id": "invite-forward-b", "risk_category": "账号交易"},
        {"source_trace_id": "invite-forward-c", "risk_category": "账号交易"},
    ]
    entities = [
        {"source_trace_id": "invite-forward-b", "entity_type": "invite_code", "normalized_value": "INV-MH-02"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    invite_clue = next(item for item in clues if item.clue_type == "shared_invite_code_multi_source")
    assert invite_clue.evidence_trace_ids == ["invite-forward-b", "invite-forward-c"]


def test_risk_clue_aggregator_bridges_singleton_contact_with_authorized_corroboration():
    records = [
        {
            "trace_id": "contact-bridge-a",
            "source_name": "authorized-im",
            "source_type": "IM",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "授权样本：私域导流广告留 WeChat:mhlead01，与前后两条授权记录相互印证。",
        },
        {
            "trace_id": "contact-bridge-b",
            "source_name": "authorized-forum",
            "source_type": "Forum",
            "publish_time": "2026-06-07T01:35:00+00:00",
            "content_text": "授权样本：账号交易贴公开邀请码 INV-MH-01，人工标注为可复核线索。",
        },
    ]
    classifications = [
        {"source_trace_id": "contact-bridge-a", "risk_category": "诈骗引流"},
        {"source_trace_id": "contact-bridge-b", "risk_category": "诈骗引流"},
    ]
    entities = [
        {"source_trace_id": "contact-bridge-a", "entity_type": "contact", "normalized_value": "WeChat:mhlead01"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    contact_clue = next(item for item in clues if item.clue_type == "shared_contact_48h")
    assert contact_clue.key == "WeChat:mhlead01"
    assert contact_clue.evidence_trace_ids == ["contact-bridge-a", "contact-bridge-b"]
    assert contact_clue.threshold_reason == "single_identifier_with_authorized_cross_source_corroboration"


def test_risk_clue_aggregator_bridges_singleton_account_tool_overlap_with_authorized_corroboration():
    records = [
        {
            "trace_id": "account-tool-bridge-a",
            "source_name": "authorized-im",
            "source_type": "IM",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "授权样本：账号批发节点 acct-mh-01 与卡密关键词同时出现，需图谱复核。",
        },
        {
            "trace_id": "account-tool-bridge-b",
            "source_name": "authorized-forum",
            "source_type": "Forum",
            "publish_time": "2026-06-07T01:45:00+00:00",
            "content_text": "授权样本：众包拉新任务重复留下 QQ:88442211，人工标注为共享联系人。",
        },
    ]
    classifications = [
        {"source_trace_id": "account-tool-bridge-a", "risk_category": "账号交易"},
        {"source_trace_id": "account-tool-bridge-b", "risk_category": "账号交易"},
    ]
    entities = [
        {"source_trace_id": "account-tool-bridge-a", "entity_type": "account", "normalized_value": "acct-mh-01"},
        {"source_trace_id": "account-tool-bridge-a", "entity_type": "tool_name", "normalized_value": "卡密"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    overlap_clue = next(item for item in clues if item.clue_type == "entity_graph_account_tool_overlap")
    assert overlap_clue.key == "acct-mh-01"
    assert overlap_clue.evidence_trace_ids == ["account-tool-bridge-a", "account-tool-bridge-b"]
    assert overlap_clue.entity_values == ["acct-mh-01", "卡密"]
    assert overlap_clue.threshold_reason == "single_account_tool_overlap_with_authorized_cross_source_corroboration"


def test_risk_clue_aggregator_bridges_account_tool_overlap_from_account_pool_text():
    records = [
        {
            "trace_id": "account-pool-bridge-a",
            "source_name": "authorized-forum",
            "source_type": "Forum",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "授权样本：论坛账号租售贴复用账号池标识 pool-mh-11，证据链可追溯。",
        },
        {
            "trace_id": "account-pool-bridge-b",
            "source_name": "authorized-feed",
            "source_type": "THREAT_INTEL",
            "publish_time": "2026-06-07T01:45:00+00:00",
            "content_text": "授权样本：二跳落地域 mh-panel12.example 同时出现在工具面板与引流模板中。",
        },
    ]
    classifications = [
        {"source_trace_id": "account-pool-bridge-a", "risk_category": "账号交易"},
        {"source_trace_id": "account-pool-bridge-b", "risk_category": "账号交易"},
    ]
    entities = [
        {"source_trace_id": "account-pool-bridge-a", "entity_type": "account", "normalized_value": "pool-mh-11"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    overlap_clue = next(item for item in clues if item.clue_type == "entity_graph_account_tool_overlap")
    assert overlap_clue.key == "pool-mh-11"
    assert overlap_clue.evidence_trace_ids == ["account-pool-bridge-a", "account-pool-bridge-b"]
    assert overlap_clue.entity_values == ["pool-mh-11", "账号池"]


def test_risk_clue_aggregator_bridges_singleton_tool_trade_cluster_with_authorized_corroboration():
    records = [
        {
            "trace_id": "tool-cluster-bridge-a",
            "source_name": "authorized-feed",
            "source_type": "THREAT_INTEL",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "授权样本：情报源确认 Telegram:mhgraph01 与群控脚本售卖节点有关。",
        },
        {
            "trace_id": "tool-cluster-bridge-b",
            "source_name": "authorized-im",
            "source_type": "IM",
            "publish_time": "2026-06-07T01:55:00+00:00",
            "content_text": "授权样本：私域导流广告留 WeChat:mhlead01，与前后两条授权记录相互印证。",
        },
    ]
    classifications = [
        {"source_trace_id": "tool-cluster-bridge-a", "risk_category": "工具交易"},
        {"source_trace_id": "tool-cluster-bridge-b", "risk_category": "工具交易"},
    ]
    entities = [
        {"source_trace_id": "tool-cluster-bridge-a", "entity_type": "contact", "normalized_value": "Telegram:mhgraph01"},
        {"source_trace_id": "tool-cluster-bridge-a", "entity_type": "tool_name", "normalized_value": "群控"},
        {"source_trace_id": "tool-cluster-bridge-a", "entity_type": "tool_name", "normalized_value": "脚本"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    cluster_clue = next(item for item in clues if item.clue_type == "entity_graph_tool_trade_cluster")
    assert cluster_clue.key == "Telegram:mhgraph01"
    assert cluster_clue.evidence_trace_ids == ["tool-cluster-bridge-a", "tool-cluster-bridge-b"]
    assert cluster_clue.entity_values == ["Telegram:mhgraph01", "群控", "脚本"]
    assert cluster_clue.threshold_reason == "single_tool_trade_cluster_with_authorized_cross_source_corroboration"


def test_risk_clue_aggregator_bridges_tool_identifier_cluster_without_contact():
    records = [
        {
            "trace_id": "tool-id-bridge-a",
            "source_name": "authorized-forum",
            "source_type": "Forum",
            "publish_time": "2026-06-07T01:00:00+00:00",
            "content_text": "授权样本：群发器售后贴和教程贴都出现 tool-mh14，人工确认不是普通教程。",
        },
        {
            "trace_id": "tool-id-bridge-b",
            "source_name": "authorized-feed",
            "source_type": "THREAT_INTEL",
            "publish_time": "2026-06-07T01:45:00+00:00",
            "content_text": "授权样本：卡密批发贴给出批次码 KM-MH-15，两个来源记录时间相差 40 分钟。",
        },
    ]
    classifications = [
        {"source_trace_id": "tool-id-bridge-a", "risk_category": "工具交易"},
        {"source_trace_id": "tool-id-bridge-b", "risk_category": "工具交易"},
    ]
    entities = [
        {"source_trace_id": "tool-id-bridge-a", "entity_type": "tool_name", "normalized_value": "tool-mh14"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    cluster_clue = next(item for item in clues if item.clue_type == "entity_graph_tool_trade_cluster")
    assert cluster_clue.key == "tool-mh14"
    assert cluster_clue.evidence_trace_ids == ["tool-id-bridge-a", "tool-id-bridge-b"]
    assert cluster_clue.entity_values == ["tool-mh14", "群发器"]


def test_risk_clue_aggregator_emits_shared_settlement_multi_source():
    records = [
        {"trace_id": "settlement-a", "source_name": "tg-pay", "publish_time": "2026-06-07T01:00:00+00:00"},
        {"trace_id": "settlement-b", "source_name": "feed-pay", "publish_time": "2026-06-07T02:00:00+00:00"},
    ]
    classifications = [
        {"source_trace_id": "settlement-a", "risk_category": "众包服务"},
        {"source_trace_id": "settlement-b", "risk_category": "众包服务"},
    ]
    entities = [
        {"source_trace_id": "settlement-a", "entity_type": "settlement", "normalized_value": "escrow-mh16"},
        {"source_trace_id": "settlement-b", "entity_type": "settlement", "normalized_value": "escrow-mh16"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    settlement_clue = next(item for item in clues if item.clue_type == "shared_settlement_multi_source")
    assert settlement_clue.key == "escrow-mh16"
    assert settlement_clue.evidence_trace_ids == ["settlement-a", "settlement-b"]
    assert settlement_clue.source_names == ["feed-pay", "tg-pay"]
    assert settlement_clue.entity_values == ["escrow-mh16"]
    assert settlement_clue.risk_category == "众包服务"
    assert settlement_clue.confidence > 0
    assert settlement_clue.threshold_reason == "same_settlement_method_appears_in_at_least_2_sources"


def test_risk_clue_aggregator_emits_account_tool_overlap_multi_trace():
    records = [
        {"trace_id": "overlap-a", "source_name": "forum-account", "publish_time": "2026-06-07T01:00:00+00:00"},
        {"trace_id": "overlap-b", "source_name": "feed-account", "publish_time": "2026-06-07T02:00:00+00:00"},
    ]
    classifications = [
        {"source_trace_id": "overlap-a", "risk_category": "账号交易"},
        {"source_trace_id": "overlap-b", "risk_category": "账号交易"},
    ]
    entities = [
        {"source_trace_id": "overlap-a", "entity_type": "account", "normalized_value": "pool-mh-11"},
        {"source_trace_id": "overlap-a", "entity_type": "tool_name", "normalized_value": "账号池"},
        {"source_trace_id": "overlap-a", "entity_type": "settlement", "normalized_value": "USDT"},
        {"source_trace_id": "overlap-b", "entity_type": "account", "normalized_value": "pool-mh-11"},
        {"source_trace_id": "overlap-b", "entity_type": "tool_name", "normalized_value": "账号池"},
        {"source_trace_id": "overlap-b", "entity_type": "url", "normalized_value": "https://panel.example/order"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    overlap_clue = next(item for item in clues if item.clue_type == "entity_graph_account_tool_overlap")
    assert overlap_clue.key == "pool-mh-11"
    assert overlap_clue.evidence_trace_ids == ["overlap-a", "overlap-b"]
    assert overlap_clue.source_names == ["feed-account", "forum-account"]
    assert overlap_clue.entity_values == ["pool-mh-11", "账号池", "USDT", "panel.example"]
    assert overlap_clue.risk_category == "账号交易"
    assert overlap_clue.confidence > 0
    assert overlap_clue.threshold_reason == "same_contact_or_account_overlaps_tool_and_trade_entities_in_at_least_2_traces_or_sources"


def test_risk_clue_aggregator_does_not_treat_generic_contact_tool_url_as_account_overlap():
    records = [
        {"trace_id": "generic-overlap-a", "source_name": "tg-generic", "publish_time": "2026-06-07T01:00:00+00:00"},
        {"trace_id": "generic-overlap-b", "source_name": "forum-generic", "publish_time": "2026-06-07T02:00:00+00:00"},
    ]
    classifications = [
        {"source_trace_id": "generic-overlap-a", "risk_category": "工具交易"},
        {"source_trace_id": "generic-overlap-b", "risk_category": "工具交易"},
    ]
    entities = [
        {"source_trace_id": "generic-overlap-a", "entity_type": "contact", "normalized_value": "Telegram:generic"},
        {"source_trace_id": "generic-overlap-a", "entity_type": "tool_name", "normalized_value": "群控脚本"},
        {"source_trace_id": "generic-overlap-a", "entity_type": "url", "normalized_value": "https://generic.example/a"},
        {"source_trace_id": "generic-overlap-b", "entity_type": "contact", "normalized_value": "Telegram:generic"},
        {"source_trace_id": "generic-overlap-b", "entity_type": "tool_name", "normalized_value": "群控脚本"},
        {"source_trace_id": "generic-overlap-b", "entity_type": "url", "normalized_value": "https://generic.example/b"},
    ]

    clues = RiskClueAggregator().aggregate(records=records, classifications=classifications, entities=entities)

    assert "shared_contact_48h" in {clue.clue_type for clue in clues}
    assert "entity_graph_account_tool_overlap" not in {clue.clue_type for clue in clues}
