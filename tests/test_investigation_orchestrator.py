from datetime import datetime, timedelta, timezone

from src.agent import InvestigationOrchestrator
from src.backend import LLMGateway
from src.config_loader import InvestigationConfig, InvestigationPolicyOverride


def _orchestrator() -> InvestigationOrchestrator:
    return InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))


def _utc_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_investigation_orchestrator_collects_sources_by_priority_layer_before_general():
    call_order: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        call_order.append(str(source["source_name"]))
        return [
            {
                "trace_id": f"trace-{source['source_name']}",
                "source_name": source["source_name"],
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "content_text": f"{source['source_name']} 私域导流 TG:traffic01 https://risk.example/path",
            }
        ]

    sources = [
        {
            "source_name": "general-feed",
            "source_type": "Forum",
            "source_url": "https://feed.example/general",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "query_term_stage": "core",
        },
        {
            "source_name": "theme-variant-feed",
            "source_type": "IM",
            "source_url": "https://feed.example/variant",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "query_theme": "诈骗引流",
            "query_term_stage": "variant",
        },
        {
            "source_name": "theme-core-feed",
            "source_type": "IM",
            "source_url": "https://feed.example/core",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "query_theme": "诈骗引流",
            "query_term_stage": "core",
        },
    ]

    result = _orchestrator().run(
        "取一下当天诈骗引流相关的线索信息",
        available_sources=sources,
        collect_source_records=collect_source,
        max_concurrent_sources=1,
    )

    assert result.status == "completed"
    assert call_order == ["theme-core-feed", "theme-variant-feed", "general-feed"]
    assert [item["source_name"] for item in result.selected_sources] == call_order
    assert [item["collection_layer"] for item in result.collection_runs] == [
        "theme_core",
        "theme_variant",
        "global_core",
    ]


def test_investigation_orchestrator_respects_max_concurrent_sources():
    active = {"count": 0, "max_seen": 0}
    release_queue: list[dict[str, object]] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        active["count"] += 1
        active["max_seen"] = max(active["max_seen"], active["count"])
        release_queue.append(source)
        while release_queue and release_queue[0] is not source:
            pass
        release_queue.pop(0)
        active["count"] -= 1
        return [
            {
                "trace_id": f"trace-{source['source_name']}",
                "source_name": source["source_name"],
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "content_text": f"{source['source_name']} 私域导流 TG:traffic01 https://risk.example/path",
            }
        ]

    sources = [
        {
            "source_name": f"feed-{index}",
            "source_type": "IM",
            "source_url": f"https://feed.example/{index}",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "query_theme": "诈骗引流",
            "query_term_stage": "variant" if index else "core",
        }
        for index in range(4)
    ]

    result = _orchestrator().run(
        "取一下当天诈骗引流相关的线索信息",
        available_sources=sources,
        collect_source_records=collect_source,
        max_concurrent_sources=2,
    )

    assert result.status == "completed"
    assert result.input_count == 4
    assert active["max_seen"] <= 2


def test_investigation_orchestrator_rewrites_query_before_collection():
    seen_urls: list[str] = []

    class _RewriteGateway:
        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return LLMGateway(dry_run=True, mock=True).chat(messages, **kwargs)
            if "source=" in user_message:
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "search_query": "site:t.me/s 接码 群控",
                            "query_theme": "接码",
                            "query_term": "群控",
                            "query_term_stage": "core",
                            "rewrite_reason": "focus_on_live_signal",
                        },
                        "error": None,
                    },
                )()
            return LLMGateway(dry_run=True, mock=True).chat(messages, **kwargs)

    orchestrator = InvestigationOrchestrator(llm_gateway=_RewriteGateway())

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen_urls.append(str(source["source_url"]))
        return [
            {
                "trace_id": "trace-1",
                "source_name": source["source_name"],
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "content_text": "接码群控 TG:traffic01 https://risk.example/path",
            }
        ]

    result = orchestrator.run(
        "找最近接码群控相关线索",
        available_sources=[
            {
                "source_name": "telegram-search",
                "source_type": "IM",
                "source_url": "https://search.example/?q=old",
                "query_url_template": "https://search.example/?q={query}",
                "search_query": "site:t.me/s 接码",
                "query_theme": "接码",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
            }
        ],
        collect_source_records=collect_source,
    )

    assert result.status == "completed"
    assert seen_urls == ["https://search.example/?q=site%3At.me%2Fs%20%E6%8E%A5%E7%A0%81%20%E7%BE%A4%E6%8E%A7"]
    assert result.execution_summary["query_rewrite_count"] == 1
    assert result.selected_sources[0]["search_query"] == "site:t.me/s 接码 群控"


def test_investigation_orchestrator_uses_live_collection_when_pool_is_not_fresh_enough():
    orchestrator = _orchestrator()
    orchestrator.clue_repo.save(
        {
            "clue_id": "pool-clue-1",
            "clue_type": "shared_contact_48h",
            "key": "tg-old",
            "risk_category": "诈骗引流",
            "source_names": ["tg-old-a", "forum-old-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["tg-old"],
            "evidence_trace_ids": ["old-1", "old-2"],
            "quality_score": 0.62,
            "confidence": 0.66,
            "last_seen": "2026-05-01T00:00:00+00:00",
        }
    )
    collected_from_sources: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        collected_from_sources.append(str(source["source_name"]))
        return [
            {
                "trace_id": "fresh-1",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:live01，落地 https://risk.example/live 第一条",
            },
            {
                "trace_id": "fresh-2",
                "source_name": "forum-live-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:live01，落地 https://risk.example/live 第二条",
            },
            {
                "trace_id": "fresh-3",
                "source_name": "feed-live-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:live01，落地 https://risk.example/live 第三条",
            },
        ]

    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        available_sources=[
            {
                "source_name": "telegram-live",
                "source_type": "IM",
                "source_url": "https://feed.example/live",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            }
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert collected_from_sources == ["telegram-live"]
    assert result.execution_summary["mode"] == "investigation_processing"
    assert result.execution_summary["used_clue_pool"] is False
    assert result.execution_summary["used_live_collection"] is True
    assert "need_fresh_signals_for_short_time_window" in result.execution_summary["live_collection_reasons"]
    assert "insufficient_high_quality_pool_clues" in result.execution_summary["live_collection_reasons"]
    assert result.execution_summary["orchestration_route"] == "live_collection_only"
    assert result.high_quality_count >= 1


def test_investigation_orchestrator_merges_pool_and_fresh_candidates():
    orchestrator = _orchestrator()
    orchestrator.clue_repo.save(
        {
            "clue_id": "pool-clue-merge",
            "clue_type": "shared_domain_multi_source",
            "key": "risk.example",
            "risk_category": "工具交易",
            "source_names": ["pool-a", "pool-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["risk.example"],
            "evidence_trace_ids": ["pool-1", "pool-2"],
            "quality_score": 0.81,
            "confidence": 0.83,
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )

    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        records=[
            {
                "trace_id": "fresh-a",
                "source_name": "tg-fresh-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:mix01，落地 https://risk.example/path 第一条",
            },
            {
                "trace_id": "fresh-b",
                "source_name": "forum-fresh-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:mix01，落地 https://risk.example/path 第二条",
            },
            {
                "trace_id": "fresh-c",
                "source_name": "feed-fresh-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:mix01，落地 https://risk.example/path 第三条",
            },
        ],
    )

    assert result.status == "completed"
    assert result.execution_summary["mode"] == "hybrid_investigation"
    assert result.execution_summary["used_clue_pool"] is True
    assert result.execution_summary["used_provided_records"] is True
    assert result.execution_summary["orchestration_route"] == "pool_plus_provided_records"
    assert result.execution_summary["merged_candidate_count"] >= 1
    assert any(
        "fresh_processing" in clue.get("orchestration_origins", []) and "clue_pool" in clue.get("orchestration_origins", [])
        for clue in [*result.high_quality_clues, *result.candidate_clues]
    )


def test_investigation_orchestrator_can_use_semantic_local_evidence_before_live_collection():
    orchestrator = _orchestrator()

    seed_records = [
        {
            "trace_id": "semantic-seed-1",
            "source_name": "tg-semantic-a",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": _utc_hours_ago(3),
            "content_text": "群控脚本接码上车，联系 TG:semantic01，落地 https://semantic.example/path 第一条",
        },
        {
            "trace_id": "semantic-seed-2",
            "source_name": "forum-semantic-b",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": _utc_hours_ago(2),
            "content_text": "群控脚本接码上车，联系 TG:semantic01，落地 https://semantic.example/path 第二条",
        },
        {
            "trace_id": "semantic-seed-3",
            "source_name": "feed-semantic-c",
            "source_type": "THREAT_INTEL",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": _utc_hours_ago(1),
            "content_text": "群控脚本接码上车，联系 TG:semantic01，落地 https://semantic.example/path 第三条",
        },
    ]
    orchestrator.phase_engine.run(seed_records)

    collected_sources: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        collected_sources.append(str(source["source_name"]))
        return [
            {
                "trace_id": "live-unexpected-1",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T05:00:00+00:00",
                "content_text": "这条不该被采集到",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时群控脚本接码上车相关的高质量线索",
        available_sources=[
            {
                "source_name": "telegram-live",
                "source_type": "IM",
                "source_url": "https://feed.example/live",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 群控 接码",
            }
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert collected_sources == []
    assert result.execution_summary["used_semantic_local_retrieval"] is True
    assert result.execution_summary["semantic_local_summary"]["record_count"] == 3
    assert result.execution_summary["semantic_local_candidate_count"] >= 1
    assert result.execution_summary["orchestration_route"] == "semantic_local_only"
    assert result.execution_summary["mode"] == "investigation_processing"
    assert result.high_quality_count >= 1


def test_investigation_orchestrator_uses_semantic_local_samples_before_live_collection():
    orchestrator = _orchestrator()

    seed_records = [
        {
            "trace_id": "graph-seed-1",
            "source_name": "tg-graph-a",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": _utc_hours_ago(3),
            "content_text": "群控脚本接码上车，联系 TG:graph01，落地 https://graph.example/path 第一条",
        },
        {
            "trace_id": "graph-seed-2",
            "source_name": "forum-graph-b",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": _utc_hours_ago(2),
            "content_text": "普通讨论文本，但联系 TG:graph01，落地 https://graph.example/path 第二条",
        },
        {
            "trace_id": "graph-seed-3",
            "source_name": "feed-graph-c",
            "source_type": "THREAT_INTEL",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": _utc_hours_ago(1),
            "content_text": "另一条普通讨论文本，也指向 https://graph.example/path 第三条",
        },
    ]
    orchestrator.phase_engine.run(seed_records)

    collected_sources: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        collected_sources.append(str(source["source_name"]))
        return []

    result = orchestrator.run(
        "帮我找近24小时群控脚本接码上车相关的高质量线索",
        available_sources=[
            {
                "source_name": "graph-live-source",
                "source_type": "IM",
                "source_url": "https://feed.example/graph-live",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 群控 接码",
            }
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert collected_sources == []
    assert result.execution_summary["used_semantic_local_retrieval"] is True
    assert result.execution_summary["semantic_local_summary"]["record_count"] == 3
    assert result.execution_summary["orchestration_route"] == "semantic_local_only"
    assert any(trace.get("stage") == "semantic_local_retrieval" for trace in result.llm_traces)


def test_investigation_orchestrator_caps_live_sources_by_policy():
    orchestrator = InvestigationOrchestrator(
        llm_gateway=LLMGateway(dry_run=True, mock=True),
        investigation_config=InvestigationConfig(max_live_sources_when_pool_hit=1),
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "pool-cap-1",
            "clue_type": "shared_contact_48h",
            "key": "tg-cap",
            "risk_category": "诈骗引流",
            "source_names": ["pool-cap-a", "pool-cap-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["tg-cap"],
            "evidence_trace_ids": ["cap-1", "cap-2"],
            "quality_score": 0.62,
            "confidence": 0.66,
            "last_seen": "2026-05-01T00:00:00+00:00",
        }
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": f"trace-{source['source_name']}",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:cap01，落地 https://risk.example/cap",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        available_sources=[
            {
                "source_name": "source-a",
                "source_type": "IM",
                "source_url": "https://feed.example/a",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            },
            {
                "source_name": "source-b",
                "source_type": "Forum",
                "source_url": "https://feed.example/b",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 加v",
            },
        ],
        collect_source_records=collect_source,
    )

    assert result.status == "completed"
    assert len(seen) == 1
    assert result.selected_source_count == 1


def test_irrelevant_review_feedback_does_not_reduce_pool_hit_live_source_cap():
    orchestrator = InvestigationOrchestrator(
        llm_gateway=LLMGateway(dry_run=True, mock=True),
        investigation_config=InvestigationConfig(max_live_sources_when_pool_hit=2),
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "pool-cap-irrelevant-1",
            "clue_type": "shared_contact_48h",
            "key": "irrelevant-cap",
            "risk_category": "诈骗引流",
            "source_names": ["irrelevant-a", "irrelevant-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["irrelevant-cap"],
            "evidence_trace_ids": ["irrelevant-1", "irrelevant-2"],
            "quality_score": 0.62,
            "confidence": 0.66,
            "last_seen": "2026-05-01T00:00:00+00:00",
        }
    )
    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "irrelevant-seed-1",
                "source_name": "tg-irrelevant-seed",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "音符联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    orchestrator.ingest_review_decision(
        seed_result.exploration_hypotheses[0]["hypothesis_id"],
        decision="APPROVED",
        reviewer="analyst",
        edited_risk_type="账号交易",
        corrected_entities=[{"entity_value": "音符", "normalized_value": "抖音"}],
        add_to_wordlist=True,
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": f"trace-{source['source_name']}",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:cap02，落地 https://risk.example/cap",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        available_sources=[
            {
                "source_name": "irrelevant-source-a",
                "source_type": "IM",
                "source_url": "https://feed.example/a",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            },
            {
                "source_name": "irrelevant-source-b",
                "source_type": "Forum",
                "source_url": "https://feed.example/b",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 加v",
            },
        ],
        collect_source_records=collect_source,
    )

    assert result.status == "completed"
    assert len(seen) == 2
    assert result.selected_source_count == 2


def test_review_feedback_does_not_reduce_pool_hit_live_source_cap():
    orchestrator = InvestigationOrchestrator(
        llm_gateway=LLMGateway(dry_run=True, mock=True),
        investigation_config=InvestigationConfig(max_live_sources_when_pool_hit=2),
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "pool-cap-relevant-1",
            "clue_type": "shared_contact_48h",
            "key": "relevant-cap",
            "risk_category": "诈骗引流",
            "source_names": ["relevant-a", "relevant-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["relevant-cap"],
            "evidence_trace_ids": ["relevant-1", "relevant-2"],
            "quality_score": 0.62,
            "confidence": 0.66,
            "last_seen": "2026-05-01T00:00:00+00:00",
        }
    )
    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "relevant-seed-1",
                "source_name": "tg-relevant-seed",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    orchestrator.ingest_review_decision(
        seed_result.exploration_hypotheses[0]["hypothesis_id"],
        decision="APPROVED",
        reviewer="analyst",
        edited_risk_type="诈骗引流",
        corrected_entities=[{"entity_value": "火苗", "normalized_value": "WhatsApp"}],
        add_to_wordlist=True,
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": f"trace-{source['source_name']}",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:cap03，落地 https://risk.example/cap",
            }
        ]

    result = orchestrator.run(
        "帮我找火苗相关的诈骗引流高质量线索",
        available_sources=[
            {
                "source_name": "relevant-source-a",
                "source_type": "IM",
                "source_url": "https://feed.example/a",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            },
            {
                "source_name": "relevant-source-b",
                "source_type": "Forum",
                "source_url": "https://feed.example/b",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 加v",
            },
        ],
        collect_source_records=collect_source,
    )

    assert result.status == "completed"
    assert len(seen) == 2
    assert result.selected_source_count == 2


def test_investigation_orchestrator_emits_telemetry_summary():
    result = _orchestrator().run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        records=[
            {
                "trace_id": "telemetry-1",
                "source_name": "tg-telemetry-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:tele01，落地 https://risk.example/tele 第一条",
            },
            {
                "trace_id": "telemetry-2",
                "source_name": "forum-telemetry-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:tele01，落地 https://risk.example/tele 第二条",
            },
            {
                "trace_id": "telemetry-3",
                "source_name": "feed-telemetry-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:tele01，落地 https://risk.example/tele 第三条",
            },
        ],
    )

    telemetry = result.execution_summary["telemetry"]
    assert telemetry["elapsed_ms"] >= 0
    assert telemetry["provided_record_count"] == 3
    assert telemetry["refined_clue_count"] >= 1
    assert "retrieval_fill_ratio" in telemetry
    assert "refine_budget_utilization" in telemetry


def test_investigation_orchestrator_builds_review_only_exploration_hypotheses_when_no_high_quality_clues():
    orchestrator = _orchestrator()

    result = orchestrator.run(
        "帮我看看这条疑似新黑话的线索，先给我可复核候选",
        records=[
            {
                "trace_id": "explore-runtime-1",
                "source_name": "tg-explore-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )

    assert result.status == "completed"
    assert result.high_quality_count == 0
    assert result.execution_summary["exploration_hypothesis_count"] == 1
    assert result.exploration_hypotheses[0]["requires_human_review"] is True
    assert result.exploration_hypotheses[0]["source"] == "controlled_exploration"
    assert len(orchestrator.review_repo.list_tasks()) == 1


def test_investigation_orchestrator_review_decision_can_feed_lifecycle_runtime_context():
    orchestrator = _orchestrator()

    result = orchestrator.run(
        "先给我这条疑似新黑话样本的可复核候选",
        records=[
            {
                "trace_id": "explore-review-1",
                "source_name": "tg-review-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )

    hypothesis_id = result.exploration_hypotheses[0]["hypothesis_id"]
    feedback = orchestrator.ingest_review_decision(
        hypothesis_id,
        decision="APPROVED",
        reviewer="analyst",
        edited_risk_type="诈骗引流",
        corrected_entities=[{"entity_value": "火苗", "normalized_value": "WhatsApp"}],
        add_to_wordlist=True,
    )

    runtime_mapping = orchestrator.phase_engine.runtime_slang_mapping()
    assert feedback["review_state"]["decision"] == "APPROVED"
    assert runtime_mapping["火苗"] == "WhatsApp"
    assert feedback["lifecycle_context"]["few_shot_examples"][0]["term"] == "火苗"


def test_investigation_orchestrator_can_trigger_controlled_exploration_with_high_quality_clues_when_slang_or_low_confidence_exists():
    orchestrator = _orchestrator()
    orchestrator.phase_engine.lifecycle_manager.ingest_review_decision(
        {
            "payload": {
                "decision": "APPROVED",
                "source_trace_id": "seed-review-1",
                "reviewer": "analyst",
                "edits": {
                    "add_to_wordlist": True,
                    "edited_risk_type": "诈骗引流",
                    "corrected_entities": [
                        {"entity_value": "火苗", "normalized_value": "WhatsApp"}
                    ],
                },
            }
        }
    )
    orchestrator.phase_engine.lifecycle_manager.gray_rollout("火苗", reviewer="analyst")
    orchestrator.phase_engine.lifecycle_manager.activate("火苗", reviewer="analyst")

    result = orchestrator.run(
        "帮我看这条高价值但可能是新变体的样本",
        records=[
            {
                "trace_id": "explore-highquality-1",
                "source_name": "tg-hq-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:hq01，落地 https://risk.example/hq 第一条",
            },
            {
                "trace_id": "explore-highquality-2",
                "source_name": "forum-hq-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T02:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:hq01，落地 https://risk.example/hq 第二条",
            },
            {
                "trace_id": "explore-highquality-3",
                "source_name": "feed-hq-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-28T03:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:hq01，落地 https://risk.example/hq 第三条",
            },
        ],
    )

    assert result.high_quality_count >= 1
    assert result.execution_summary["exploration_hypothesis_count"] >= 1
    assert any(item["requires_human_review"] is True for item in result.exploration_hypotheses)


def test_investigation_orchestrator_passes_runtime_context_into_llm_refiner():
    captured = {}

    class _CaptureGateway:
        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return LLMGateway(dry_run=True, mock=True).chat(messages, **kwargs)
            captured["messages"] = messages
            return type(
                "Resp",
                (),
                {
                    "ok": True,
                    "parsed_json": {
                        "refined_summary": "runtime context used",
                        "confidence_delta": 0.05,
                        "review_required": False,
                        "refinement_reasons": ["runtime_slang_context"],
                    },
                    "error": None,
                },
            )()

    orchestrator = InvestigationOrchestrator(llm_gateway=_CaptureGateway())
    orchestrator.phase_engine.lifecycle_manager.ingest_review_decision(
        {
            "payload": {
                "decision": "APPROVED",
                "source_trace_id": "seed-review-2",
                "reviewer": "analyst",
                "edits": {
                    "add_to_wordlist": True,
                    "edited_risk_type": "诈骗引流",
                    "corrected_entities": [
                        {"entity_value": "火苗", "normalized_value": "WhatsApp"}
                    ],
                },
            }
        }
    )

    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        records=[
            {
                "trace_id": "refine-runtime-1",
                "source_name": "tg-refine-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:runtime01，落地 https://risk.example/runtime 第一条",
            },
            {
                "trace_id": "refine-runtime-2",
                "source_name": "forum-refine-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:runtime01，落地 https://risk.example/runtime 第二条",
            },
            {
                "trace_id": "refine-runtime-3",
                "source_name": "feed-refine-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:runtime01，落地 https://risk.example/runtime 第三条",
            },
        ],
    )

    assert result.high_quality_count >= 1
    user_content = str(captured["messages"][-1]["content"])
    assert "runtime_slang_terms" in user_content
    assert "runtime_few_shot_examples" in user_content
    assert any(trace.get("runtime_slang_term_count", 0) >= 1 for trace in result.llm_traces if trace.get("stage") == "clue_refine")


def test_investigation_orchestrator_passes_runtime_context_into_intent_parser_and_planner():
    captured = {"intent": None, "plan": None}

    class _CaptureGateway:
        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if user_message.startswith("query="):
                captured["intent"] = user_message
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "goal": "collect_high_quality_risk_clues",
                            "risk_types": ["诈骗引流"],
                            "source_preferences": ["telegram"],
                            "include_keywords": ["火苗"],
                            "exclude_keywords": [],
                            "time_range_hours": 24,
                            "quality_profile": "balanced",
                            "output_type": "clue_cards",
                            "require_cross_source": False,
                            "require_evidence_chain": True,
                        },
                        "error": None,
                    },
                )()
            if "available_sources=" in user_message:
                captured["plan"] = user_message
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "goal": "collect_high_quality_risk_clues",
                            "agent_steps": [{"agent": "intent_planner", "action": "parse_request_to_structured_intent"}],
                            "source_selection_strategy": {"preferred_source_types": ["telegram"]},
                            "execution_notes": ["refine_policy=off"],
                            "quality_gate": {
                                "quality_profile": "balanced",
                                "minimum_quality_score": 0.65,
                                "require_cross_source": False,
                                "require_evidence_chain": True,
                            },
                            "budget": {
                                "max_sources": 2,
                                "max_raw_records": 20,
                                "max_candidate_clues": 10,
                                "max_llm_refine_clues": 0,
                                "max_elapsed_seconds": 20,
                            },
                        },
                        "error": None,
                    },
                )()
            return LLMGateway(dry_run=True, mock=True).chat(messages, **kwargs)

    orchestrator = InvestigationOrchestrator(llm_gateway=_CaptureGateway())
    orchestrator.phase_engine.lifecycle_manager.ingest_review_decision(
        {
            "payload": {
                "decision": "APPROVED",
                "source_trace_id": "seed-runtime-parse-1",
                "reviewer": "analyst",
                "edits": {
                    "add_to_wordlist": True,
                    "edited_risk_type": "诈骗引流",
                    "corrected_entities": [
                        {"entity_value": "火苗", "normalized_value": "WhatsApp"}
                    ],
                },
            }
        }
    )

    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "parse-seed-1",
                "source_name": "tg-parse-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    assert seed_result.exploration_hypotheses

    orchestrator.run(
        "帮我找火苗相关线索",
        records=[
            {
                "trace_id": "parse-runtime-1",
                "source_name": "tg-runtime-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T02:00:00+00:00",
                "content_text": "火苗联系，继续上车。",
            }
        ],
    )

    assert captured["intent"] is not None
    assert captured["plan"] is not None
    assert "runtime_slang_terms" in captured["intent"]
    assert "runtime_few_shot_examples" in captured["intent"]
    assert "runtime_slang_terms" in captured["plan"]


def test_review_feedback_can_change_fallback_intent_without_changing_plan_shape():
    class _UnusableGateway:
        def chat(self, messages, **kwargs):  # noqa: ANN001
            return type(
                "Resp",
                (),
                {
                    "ok": True,
                    "parsed_json": {},
                    "error": None,
                },
            )()

    orchestrator = InvestigationOrchestrator(llm_gateway=_UnusableGateway())
    orchestrator.phase_engine.lifecycle_manager.ingest_review_decision(
        {
            "payload": {
                "decision": "APPROVED",
                "source_trace_id": "seed-fallback-1",
                "reviewer": "analyst",
                "edits": {
                    "add_to_wordlist": True,
                    "edited_risk_type": "诈骗引流",
                    "corrected_entities": [
                        {"entity_value": "火苗", "normalized_value": "WhatsApp"}
                    ],
                },
            }
        }
    )

    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "fallback-seed-1",
                "source_name": "tg-fallback-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    assert seed_result.exploration_hypotheses

    result = orchestrator.run(
        "帮我看火苗这类样本",
        records=[
            {
                "trace_id": "fallback-runtime-1",
                "source_name": "tg-fallback-b",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T02:00:00+00:00",
                "content_text": "火苗联系继续上车。",
            }
        ],
    )

    assert result.intent["include_keywords"]
    assert "火苗" in result.intent["include_keywords"] or "WhatsApp" in result.intent["include_keywords"]
    assert all(
        step["agent"] != "exploration_agent"
        for step in result.investigation_plan["agent_steps"]
    )
    assert result.investigation_plan["source_selection_strategy"].get("collection_mode") == "adaptive"


def test_review_feedback_does_not_reduce_refine_budget_before_llm_refinement():
    orchestrator = _orchestrator()
    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "refine-seed-1",
                "source_name": "tg-refine-seed",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    orchestrator.ingest_review_decision(
        seed_result.exploration_hypotheses[0]["hypothesis_id"],
        decision="APPROVED",
        reviewer="analyst",
        edited_risk_type="诈骗引流",
        corrected_entities=[{"entity_value": "火苗", "normalized_value": "WhatsApp"}],
        add_to_wordlist=True,
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "refine-budget-1",
            "clue_type": "shared_contact_48h",
            "key": "refine-budget-a",
            "risk_category": "诈骗引流",
            "source_names": ["budget-a", "budget-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["火苗", "WhatsApp"],
            "evidence_trace_ids": ["budget-1", "budget-2"],
            "quality_score": 0.74,
            "confidence": 0.71,
            "quality": {"pass_threshold": True, "quality_score": 0.74, "cross_source_count": 2, "evidence_count": 2},
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "refine-budget-2",
            "clue_type": "shared_domain_multi_source",
            "key": "other-budget.example",
            "risk_category": "账号交易",
            "source_names": ["budget-c", "budget-d"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["other-budget.example"],
            "evidence_trace_ids": ["budget-3", "budget-4"],
            "quality_score": 0.73,
            "confidence": 0.7,
            "quality": {"pass_threshold": True, "quality_score": 0.73, "cross_source_count": 2, "evidence_count": 2},
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "refine-budget-3",
            "clue_type": "high_frequency_template",
            "key": "another-template",
            "risk_category": "诈骗引流",
            "source_names": ["budget-e", "budget-f"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["another-template"],
            "evidence_trace_ids": ["budget-5", "budget-6"],
            "quality_score": 0.72,
            "confidence": 0.69,
            "quality": {"pass_threshold": True, "quality_score": 0.72, "cross_source_count": 2, "evidence_count": 2},
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )

    result = orchestrator.run(
        "帮我找火苗相关的高质量线索",
        policy_override={"max_llm_refine_clues": 5},
    )

    assert result.status == "completed"
    assert result.execution_summary["requested_max_llm_refine_clues"] == 5
    assert result.execution_summary["effective_max_llm_refine_clues"] == 5
    assert result.execution_summary["refine_budget_reasons"] == []
    assert result.execution_summary["refined_clue_count"] <= result.execution_summary["effective_max_llm_refine_clues"]
    assert result.execution_summary["merged_candidate_count"] >= result.execution_summary["refined_clue_count"]
    assert result.execution_summary["telemetry"]["requested_max_llm_refine_clues"] == 5
    assert (
        result.execution_summary["telemetry"]["effective_max_llm_refine_clues"]
        == result.execution_summary["effective_max_llm_refine_clues"]
    )


def test_review_feedback_does_not_suppress_live_collection_when_quality_gap_remains():
    orchestrator = _orchestrator()
    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "live-suppress-seed-1",
                "source_name": "tg-live-suppress-seed",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    orchestrator.ingest_review_decision(
        seed_result.exploration_hypotheses[0]["hypothesis_id"],
        decision="APPROVED",
        reviewer="analyst",
        edited_risk_type="诈骗引流",
        corrected_entities=[{"entity_value": "火苗", "normalized_value": "WhatsApp"}],
        add_to_wordlist=True,
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "live-suppress-pool-1",
            "clue_type": "shared_contact_48h",
            "key": "live-suppress-a",
            "risk_category": "诈骗引流",
            "source_names": ["suppress-a", "suppress-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["火苗", "WhatsApp"],
            "evidence_trace_ids": ["suppress-1", "suppress-2"],
            "quality_score": 0.62,
            "confidence": 0.68,
            "quality": {"pass_threshold": False, "quality_score": 0.62, "cross_source_count": 2, "evidence_count": 2},
            "last_seen": "2026-05-28T03:00:00+00:00",
        }
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": "should-not-collect",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T04:00:00+00:00",
                "content_text": "never collected",
            }
        ]

    result = orchestrator.run(
        "帮我找火苗相关的诈骗引流高质量线索",
        available_sources=[
            {
                "source_name": "live-suppress-source-a",
                "source_type": "IM",
                "source_url": "https://feed.example/live-suppress-a",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            }
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 72},
    )

    assert result.status == "completed"
    assert seen == ["live-suppress-source-a"]
    assert result.execution_summary["used_live_collection"] is True
    assert result.execution_summary["orchestration_route"] == "pool_plus_live_collection"
    assert "insufficient_high_quality_pool_clues" in result.execution_summary["live_collection_reasons"]


def test_relevant_approved_exploration_does_not_suppress_live_collection_when_freshness_is_required():
    orchestrator = _orchestrator()
    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "live-fresh-seed-1",
                "source_name": "tg-live-fresh-seed",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    orchestrator.ingest_review_decision(
        seed_result.exploration_hypotheses[0]["hypothesis_id"],
        decision="APPROVED",
        reviewer="analyst",
        edited_risk_type="诈骗引流",
        corrected_entities=[{"entity_value": "火苗", "normalized_value": "WhatsApp"}],
        add_to_wordlist=True,
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "live-fresh-pool-1",
            "clue_type": "shared_contact_48h",
            "key": "live-fresh-a",
            "risk_category": "诈骗引流",
            "source_names": ["fresh-a", "fresh-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["火苗", "WhatsApp"],
            "evidence_trace_ids": ["fresh-1", "fresh-2"],
            "quality_score": 0.62,
            "confidence": 0.68,
            "quality": {"pass_threshold": False, "quality_score": 0.62, "cross_source_count": 2, "evidence_count": 2},
            "last_seen": "2026-05-01T03:00:00+00:00",
        }
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": "live-fresh-collect-1",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T04:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:livefresh01，落地 https://risk.example/livefresh",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时火苗相关的诈骗引流高质量线索",
        available_sources=[
            {
                "source_name": "live-fresh-source-a",
                "source_type": "IM",
                "source_url": "https://feed.example/live-fresh-a",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            }
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert seen == ["live-fresh-source-a"]
    assert result.execution_summary["used_live_collection"] is True
    assert "need_fresh_signals_for_short_time_window" in result.execution_summary["live_collection_reasons"]
    assert "need_recent_high_quality_signals" in result.execution_summary["live_collection_reasons"]




def test_review_feedback_keeps_live_collection_simple_when_cross_source_is_required():
    orchestrator = InvestigationOrchestrator(
        llm_gateway=LLMGateway(dry_run=True, mock=True),
        investigation_config=InvestigationConfig(max_live_sources_when_pool_hit=3),
    )
    seed_result = orchestrator.run(
        "先给我一条可复核的新黑话候选",
        records=[
            {
                "trace_id": "focus-cross-seed-1",
                "source_name": "tg-focus-cross-seed",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-28T01:00:00+00:00",
                "content_text": "火苗联系，欢迎上车。",
            }
        ],
        policy_override={"minimum_quality_score": 0.95},
    )
    orchestrator.ingest_review_decision(
        seed_result.exploration_hypotheses[0]["hypothesis_id"],
        decision="APPROVED",
        reviewer="analyst",
        edited_risk_type="诈骗引流",
        corrected_entities=[{"entity_value": "火苗", "normalized_value": "WhatsApp"}],
        add_to_wordlist=True,
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": f"focus-cross-{source['source_name']}",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T04:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:focuscross01，落地 https://risk.example/focuscross",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时火苗相关的诈骗引流高质量线索",
        available_sources=[
            {
                "source_name": "focus-cross-theme-core",
                "source_type": "IM",
                "source_url": "https://feed.example/focus-cross-theme-core",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "query_term_stage": "core",
                "search_query": "site:t.me/s 火苗",
            },
            {
                "source_name": "focus-cross-theme-variant",
                "source_type": "Forum",
                "source_url": "https://feed.example/focus-cross-theme-variant",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "query_term_stage": "variant",
                "search_query": "site:t.me/s WhatsApp",
            },
            {
                "source_name": "focus-cross-general",
                "source_type": "THREAT_INTEL",
                "source_url": "https://feed.example/focus-cross-general",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "search_query": "site:t.me/s 私域导流",
            },
        ],
        collect_source_records=collect_source,
        policy_override={"require_cross_source": True},
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert result.execution_summary["used_live_collection"] is True
    assert len(seen) >= 2
    assert "focus-cross-theme-core" in seen
    assert "focus-cross-theme-variant" in seen




def test_single_live_source_still_rewrites_normally_after_review_feedback():
    seen_urls: list[str] = []

    class _SingleRewriteGateway:
        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return LLMGateway(dry_run=True, mock=True).chat(messages, **kwargs)
            return type(
                "Resp",
                (),
                {
                    "ok": True,
                    "parsed_json": {
                        "search_query": "site:t.me/s 火苗 群控",
                        "query_theme": "诈骗引流",
                        "query_term": "火苗",
                        "query_term_stage": "core",
                        "rewrite_reason": "single_runtime_rewrite",
                    },
                    "error": None,
                },
            )()

    orchestrator = InvestigationOrchestrator(llm_gateway=_SingleRewriteGateway())

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen_urls.append(str(source["source_url"]))
        return [
            {
                "trace_id": "single-rewrite-1",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T04:00:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:singlerewrite01，落地 https://risk.example/singlerewrite",
            }
        ]

    result = orchestrator.run(
        "找近24小时火苗相关线索",
        available_sources=[
            {
                "source_name": "single-rewrite-source",
                "source_type": "IM",
                "source_url": "https://search.example/?q=old",
                "query_url_template": "https://search.example/?q={query}",
                "search_query": "site:t.me/s 接码",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
            }
        ],
        collect_source_records=collect_source,
    )

    assert result.status == "completed"
    assert result.execution_summary["query_rewrite_count"] == 1
    assert seen_urls == ["https://search.example/?q=site%3At.me%2Fs%20%E7%81%AB%E8%8B%97%20%E7%BE%A4%E6%8E%A7"]


def test_live_collection_uses_priority_layers_in_order():
    orchestrator = InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        if str(source["source_name"]) == "earlystop-theme-core":
            return [
                {
                    "trace_id": "earlystop-1",
                    "source_name": "earlystop-theme-core",
                    "source_type": "IM",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-28T04:00:00+00:00",
                    "content_text": "群控脚本接码火苗上车，联系 TG:earlystop01，落地 https://risk.example/earlystop 第一条",
                },
                {
                    "trace_id": "earlystop-2",
                    "source_name": "earlystop-forum-b",
                    "source_type": "Forum",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-28T04:10:00+00:00",
                    "content_text": "群控脚本接码火苗上车，联系 TG:earlystop01，落地 https://risk.example/earlystop 第二条",
                },
                {
                    "trace_id": "earlystop-3",
                    "source_name": "earlystop-intel-c",
                    "source_type": "THREAT_INTEL",
                    "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                    "publish_time": "2026-05-28T04:20:00+00:00",
                    "content_text": "群控脚本接码火苗上车，联系 TG:earlystop01，落地 https://risk.example/earlystop 第三条",
                },
            ]
        return [
            {
                "trace_id": f"late-{source['source_name']}",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T05:00:00+00:00",
                "content_text": "should not run",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时火苗相关的诈骗引流高质量线索",
        available_sources=[
            {
                "source_name": "earlystop-theme-core",
                "source_type": "IM",
                "source_url": "https://feed.example/earlystop-theme-core",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "query_term_stage": "core",
                "search_query": "site:t.me/s 火苗",
            },
            {
                "source_name": "earlystop-theme-variant",
                "source_type": "Forum",
                "source_url": "https://feed.example/earlystop-theme-variant",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "query_term_stage": "variant",
                "search_query": "site:t.me/s WhatsApp",
            },
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert seen == ["earlystop-theme-core", "earlystop-theme-variant"]
    assert result.execution_summary["used_live_collection"] is True
    assert result.execution_summary["orchestration_route"] == "live_collection_only"


def test_live_collection_continues_when_priority_layer_fresh_evidence_is_still_insufficient():
    orchestrator = InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        if str(source["source_name"]) == "continue-theme-core":
            return [
                {
                    "trace_id": "continue-1",
                    "source_name": "continue-theme-core",
                    "source_type": "IM",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-28T04:00:00+00:00",
                    "content_text": "火苗联系，欢迎上车。",
                }
            ]
        return [
            {
                "trace_id": "continue-2",
                "source_name": "continue-theme-variant",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-28T04:10:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:continue01，落地 https://risk.example/continue 第二条",
            },
            {
                "trace_id": "continue-3",
                "source_name": "continue-intel-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-28T04:20:00+00:00",
                "content_text": "群控脚本接码火苗上车，联系 TG:continue01，落地 https://risk.example/continue 第三条",
            },
        ]

    result = orchestrator.run(
        "帮我找近24小时火苗相关的诈骗引流高质量线索",
        available_sources=[
            {
                "source_name": "continue-theme-core",
                "source_type": "IM",
                "source_url": "https://feed.example/continue-theme-core",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "query_term_stage": "core",
                "search_query": "site:t.me/s 火苗",
            },
            {
                "source_name": "continue-theme-variant",
                "source_type": "Forum",
                "source_url": "https://feed.example/continue-theme-variant",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "query_term_stage": "variant",
                "search_query": "site:t.me/s WhatsApp",
            },
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert seen == ["continue-theme-core", "continue-theme-variant"]
    assert result.execution_summary["used_live_collection"] is True
















def test_llm_plan_can_use_semantic_local_before_live_collection():
    class _SemanticPolicyGateway:
        def __init__(self) -> None:
            self._fallback = LLMGateway(dry_run=True, mock=True)

        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "goal": "collect_high_quality_risk_clues",
                            "agent_steps": [
                                {"agent": "source_planner", "action": "expand_semantic_local_first"},
                            ],
                            "source_selection_strategy": {
                                "preferred_source_types": ["telegram"],
                                "match_query_keywords": ["诈骗引流"],
                                "collection_mode": "adaptive",
                            },
                            "execution_notes": ["refine_policy=budgeted"],
                            "quality_gate": {
                                "quality_profile": "balanced",
                                "minimum_quality_score": 0.65,
                                "require_cross_source": True,
                                "require_evidence_chain": True,
                            },
                            "budget": {
                                "max_sources": 1,
                                "max_raw_records": 10,
                                "max_candidate_clues": 10,
                                "max_llm_refine_clues": 2,
                                "max_elapsed_seconds": 20,
                            },
                        },
                        "error": None,
                    },
                )()
            return self._fallback.chat(messages, **kwargs)

    orchestrator = InvestigationOrchestrator(llm_gateway=_SemanticPolicyGateway())
    orchestrator.phase_engine.run(
        [
            {
                "trace_id": "plan-semantic-1",
                "source_name": "tg-plan-semantic-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": _utc_hours_ago(3),
                "content_text": "群控脚本接码上车，联系 TG:plansemantic01，落地 https://plansemantic.example/path 第一条",
            },
            {
                "trace_id": "plan-semantic-2",
                "source_name": "forum-plan-semantic-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": _utc_hours_ago(2),
                "content_text": "群控脚本接码上车，联系 TG:plansemantic01，落地 https://plansemantic.example/path 第二条",
            },
            {
                "trace_id": "plan-semantic-3",
                "source_name": "feed-plan-semantic-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": _utc_hours_ago(1),
                "content_text": "群控脚本接码上车，联系 TG:plansemantic01，落地 https://plansemantic.example/path 第三条",
            },
        ]
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return []

    result = orchestrator.run(
        "帮我找近24小时群控脚本接码上车相关的高质量线索",
        available_sources=[
            {
                "source_name": "plan-semantic-live",
                "source_type": "IM",
                "source_url": "https://feed.example/plan-semantic-live",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 群控 接码",
            }
        ],
        collect_source_records=collect_source,
        retrieval_filters={"time_range_hours": 24},
    )

    assert result.status == "completed"
    assert result.execution_summary["semantic_local_summary"]["query_limit"] >= 3
    assert result.execution_summary["orchestration_route"] == "semantic_local_only"
    assert seen == []








def test_fast_routing_profile_caps_pool_hit_live_collection_more_aggressively():
    orchestrator = InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))
    orchestrator.clue_repo.save(
        {
            "clue_id": "profile-fast-1",
            "clue_type": "shared_contact_48h",
            "key": "fast-pool",
            "risk_category": "诈骗引流",
            "source_names": ["fast-a", "fast-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["fast-pool"],
            "evidence_trace_ids": ["fast-1", "fast-2"],
            "quality_score": 0.61,
            "confidence": 0.66,
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": f"fast-{source['source_name']}",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:fast01，落地 https://risk.example/fast",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        available_sources=[
            {"source_name": "fast-source-a", "source_type": "IM", "source_url": "https://feed.example/a", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 私域导流"},
            {"source_name": "fast-source-b", "source_type": "Forum", "source_url": "https://feed.example/b", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 加v"},
        ],
        collect_source_records=collect_source,
        routing_profile="fast",
    )

    assert result.status == "completed"
    assert result.execution_summary["routing_profile"] == "fast"
    assert len(seen) == 1


def test_high_recall_routing_profile_expands_pool_hit_live_collection_budget():
    orchestrator = InvestigationOrchestrator(
        llm_gateway=LLMGateway(dry_run=True, mock=True),
        investigation_config=InvestigationConfig(max_live_sources_when_pool_hit=2),
    )
    orchestrator.clue_repo.save(
        {
            "clue_id": "profile-recall-1",
            "clue_type": "shared_contact_48h",
            "key": "recall-pool",
            "risk_category": "诈骗引流",
            "source_names": ["recall-a", "recall-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["recall-pool"],
            "evidence_trace_ids": ["recall-1", "recall-2"],
            "quality_score": 0.61,
            "confidence": 0.66,
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": f"recall-{source['source_name']}",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:recall01，落地 https://risk.example/recall",
            }
        ]

    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        available_sources=[
            {"source_name": "recall-source-a", "source_type": "IM", "source_url": "https://feed.example/a", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 私域导流"},
            {"source_name": "recall-source-b", "source_type": "Forum", "source_url": "https://feed.example/b", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 加v"},
            {"source_name": "recall-source-c", "source_type": "THREAT_INTEL", "source_url": "https://feed.example/c", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 拉群"},
        ],
        collect_source_records=collect_source,
        routing_profile="high_recall",
    )

    assert result.status == "completed"
    assert result.execution_summary["routing_profile"] == "high_recall"
    assert len(seen) == 3


def test_request_policy_override_can_disable_live_collection():
    orchestrator = InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))
    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        available_sources=[
            {"source_name": "override-source-a", "source_type": "IM", "source_url": "https://feed.example/a", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 私域导流"},
        ],
        collect_source_records=lambda _source: [{"trace_id": "should-not-run", "content_text": "never"}],
        policy_override=InvestigationPolicyOverride(live_collection_enabled=False),
    )

    assert result.status == "no_data"
    assert result.execution_summary["routing_profile"] == "balanced"
    assert result.execution_summary["used_live_collection"] is False


def test_request_policy_override_can_limit_budget_dimensions():
    orchestrator = InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))
    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        available_sources=[
            {"source_name": "budget-source-a", "source_type": "IM", "source_url": "https://feed.example/a", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 私域导流"},
            {"source_name": "budget-source-b", "source_type": "Forum", "source_url": "https://feed.example/b", "legal_basis": "PUBLIC_COMPLIANT_DATA", "query_theme": "诈骗引流", "search_query": "site:t.me/s 加v"},
        ],
        records=[
            {
                "trace_id": "budget-fresh-1",
                "source_name": "tg-budget-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:budget01，落地 https://risk.example/budget 第一条",
            },
            {
                "trace_id": "budget-fresh-2",
                "source_name": "forum-budget-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:budget01，落地 https://risk.example/budget 第二条",
            },
            {
                "trace_id": "budget-fresh-3",
                "source_name": "feed-budget-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:budget01，落地 https://risk.example/budget 第三条",
            },
        ],
        policy_override=InvestigationPolicyOverride(
            max_sources=1,
            max_raw_records=2,
            max_candidate_clues=1,
            max_llm_refine_clues=1,
        ),
    )

    assert result.status == "completed"
    assert result.selected_source_count == 1
    assert result.input_count == 2
    assert result.execution_summary["budget"]["max_sources"] == 1
    assert result.execution_summary["budget"]["max_raw_records"] == 2
    assert result.execution_summary["budget"]["max_candidate_clues"] == 1
    assert result.execution_summary["budget"]["max_llm_refine_clues"] == 1


def test_policy_override_can_relax_runtime_quality_gate_for_pool_only_clues():
    orchestrator = _orchestrator()
    orchestrator.clue_repo.save(
        {
            "clue_id": "quality-gate-relax-1",
            "clue_type": "shared_contact_48h",
            "key": "tg-relax",
            "risk_category": "诈骗引流",
            "source_names": ["relax-a", "relax-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["tg-relax"],
            "evidence_trace_ids": ["relax-1", "relax-2"],
            "quality_score": 0.7,
            "confidence": 0.72,
            "quality": {
                "pass_threshold": False,
                "quality_score": 0.7,
                "cross_source_count": 2,
                "evidence_count": 2,
            },
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )

    default_result = orchestrator.run("帮我找诈骗引流高质量线索")
    relaxed_result = orchestrator.run(
        "帮我找诈骗引流高质量线索",
        policy_override=InvestigationPolicyOverride(minimum_quality_score=0.65),
    )

    assert default_result.status == "completed"
    assert default_result.high_quality_count == 0
    assert relaxed_result.status == "completed"
    assert relaxed_result.high_quality_count == 1
    assert relaxed_result.execution_summary["runtime_quality_gate"]["minimum_quality_score"] == 0.65


def test_policy_override_can_tighten_runtime_quality_gate_beyond_saved_pass_threshold():
    orchestrator = _orchestrator()
    orchestrator.clue_repo.save(
        {
            "clue_id": "quality-gate-strict-1",
            "clue_type": "shared_domain_multi_source",
            "key": "strict.example",
            "risk_category": "诈骗引流",
            "source_names": ["strict-a"],
            "source_types": ["IM"],
            "entity_values": ["strict.example"],
            "evidence_trace_ids": ["strict-1", "strict-2"],
            "quality_score": 0.83,
            "confidence": 0.81,
            "quality": {
                "pass_threshold": True,
                "quality_score": 0.83,
                "cross_source_count": 1,
                "evidence_count": 2,
            },
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )

    default_result = orchestrator.run("帮我找诈骗引流线索")
    strict_result = orchestrator.run(
        "帮我找诈骗引流线索",
        policy_override=InvestigationPolicyOverride(
            minimum_quality_score=0.8,
            require_cross_source=True,
        ),
    )

    assert default_result.status == "completed"
    assert default_result.high_quality_count == 1
    assert strict_result.status == "completed"
    assert strict_result.high_quality_count == 0
    assert strict_result.candidate_count >= 1
    assert strict_result.execution_summary["runtime_quality_gate"]["require_cross_source"] is True


def test_llm_plan_quality_gate_can_force_live_collection_when_pool_quality_is_insufficient():
    class _PlanGateGateway:
        def __init__(self) -> None:
            self._fallback = LLMGateway(dry_run=True, mock=True)

        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "goal": "collect_high_quality_risk_clues",
                            "agent_steps": [
                                {"agent": "intent_planner", "action": "parse_request_to_structured_intent"},
                                {"agent": "source_planner", "action": "select_authorized_sources_and_query_variants"},
                            ],
                            "selected_source_names": ["plan-source-a"],
                            "source_selection_strategy": {
                                "preferred_source_types": ["telegram", "forum"],
                                "match_query_keywords": ["诈骗引流"],
                            },
                            "execution_notes": ["planner_tightens_quality_gate_before_live_collection"],
                            "quality_gate": {
                                "quality_profile": "balanced",
                                "minimum_quality_score": 0.75,
                                "require_cross_source": True,
                                "require_evidence_chain": True,
                            },
                            "budget": {
                                "max_sources": 1,
                                "max_raw_records": 20,
                                "max_candidate_clues": 10,
                                "max_llm_refine_clues": 3,
                                "max_elapsed_seconds": 20,
                            },
                        },
                        "error": None,
                    },
                )()
            return self._fallback.chat(messages, **kwargs)

    orchestrator = InvestigationOrchestrator(llm_gateway=_PlanGateGateway())
    orchestrator.clue_repo.save(
        {
            "clue_id": "plan-gate-pool-1",
            "clue_type": "shared_contact_48h",
            "key": "tg-plan",
            "risk_category": "诈骗引流",
            "source_names": ["pool-a", "pool-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["tg-plan"],
            "evidence_trace_ids": ["plan-1", "plan-2"],
            "quality_score": 0.7,
            "confidence": 0.72,
            "quality": {
                "pass_threshold": True,
                "quality_score": 0.7,
                "cross_source_count": 2,
                "evidence_count": 2,
            },
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": "plan-fresh-1",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T04:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:plan01，落地 https://risk.example/plan 第一条",
            },
            {
                "trace_id": "plan-fresh-2",
                "source_name": "plan-forum-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T05:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:plan01，落地 https://risk.example/plan 第二条",
            },
        ]

    result = orchestrator.run(
        "帮我找诈骗引流线索",
        available_sources=[
            {
                "source_name": "plan-source-a",
                "source_type": "IM",
                "source_url": "https://feed.example/plan",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            }
        ],
        collect_source_records=collect_source,
    )

    assert result.status == "completed"
    assert seen == ["plan-source-a"]
    assert result.execution_summary["used_live_collection"] is True
    assert "insufficient_high_quality_pool_clues" in result.execution_summary["live_collection_reasons"]
    assert result.execution_summary["runtime_quality_gate"]["minimum_quality_score"] == 0.75


def test_llm_plan_can_force_pool_only_route_even_when_live_collection_is_available():
    class _PoolOnlyGateway:
        def __init__(self) -> None:
            self._fallback = LLMGateway(dry_run=True, mock=True)

        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "goal": "collect_high_quality_risk_clues",
                            "agent_steps": [
                                {"agent": "intent_planner", "action": "parse_request_to_structured_intent"},
                                {"agent": "source_planner", "action": "retrieve_clue_pool_only"},
                            ],
                            "source_selection_strategy": {
                                "preferred_source_types": ["telegram"],
                                "match_query_keywords": ["诈骗引流"],
                                "collection_mode": "pool_only",
                            },
                            "execution_notes": ["refine_policy=budgeted"],
                            "quality_gate": {
                                "quality_profile": "balanced",
                                "minimum_quality_score": 0.65,
                                "require_cross_source": False,
                                "require_evidence_chain": False,
                            },
                            "budget": {
                                "max_sources": 2,
                                "max_raw_records": 20,
                                "max_candidate_clues": 10,
                                "max_llm_refine_clues": 2,
                                "max_elapsed_seconds": 20,
                            },
                        },
                        "error": None,
                    },
                )()
            return self._fallback.chat(messages, **kwargs)

    orchestrator = InvestigationOrchestrator(llm_gateway=_PoolOnlyGateway())
    orchestrator.clue_repo.save(
        {
            "clue_id": "pool-only-1",
            "clue_type": "shared_contact_48h",
            "key": "pool-only",
            "risk_category": "诈骗引流",
            "source_names": ["pool-a", "pool-b"],
            "source_types": ["IM", "Forum"],
            "entity_values": ["pool-only"],
            "evidence_trace_ids": ["pool-only-1", "pool-only-2"],
            "quality_score": 0.72,
            "confidence": 0.73,
            "quality": {
                "pass_threshold": True,
                "quality_score": 0.72,
                "cross_source_count": 2,
                "evidence_count": 2,
            },
            "last_seen": "2026-05-23T03:00:00+00:00",
        }
    )

    result = orchestrator.run(
        "帮我找诈骗引流线索",
        available_sources=[
            {
                "source_name": "pool-only-source-a",
                "source_type": "IM",
                "source_url": "https://feed.example/pool-only",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "诈骗引流",
                "search_query": "site:t.me/s 私域导流",
            }
        ],
        collect_source_records=lambda _source: [{"trace_id": "should-not-run", "content_text": "never"}],
    )

    assert result.status == "completed"
    assert result.execution_summary["used_live_collection"] is False
    assert result.execution_summary["mode"] == "candidate_clue_retrieval"
    assert result.execution_summary["plan_execution_controls"]["collection_mode"] == "pool_only"
    assert "plan_prefers_pool_only" in result.execution_summary["live_collection_reasons"]


def test_llm_plan_can_disable_query_rewrite_for_live_collection_sources():
    seen_urls: list[str] = []

    class _NoRewriteGateway:
        def __init__(self) -> None:
            self._fallback = LLMGateway(dry_run=True, mock=True)

        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "goal": "collect_high_quality_risk_clues",
                            "agent_steps": [
                                {"agent": "intent_planner", "action": "parse_request_to_structured_intent"},
                                {"agent": "source_planner", "action": "skip_query_rewrite_and_collect_authorized_records"},
                            ],
                            "selected_source_names": ["no-rewrite-source"],
                            "source_selection_strategy": {
                                "preferred_source_types": ["telegram"],
                                "match_query_keywords": ["接码"],
                                "collection_mode": "live_only",
                                "query_rewrite_policy": "off",
                            },
                            "execution_notes": ["disable_query_rewrite"],
                            "quality_gate": {
                                "quality_profile": "balanced",
                                "minimum_quality_score": 0.65,
                                "require_cross_source": False,
                                "require_evidence_chain": False,
                            },
                            "budget": {
                                "max_sources": 1,
                                "max_raw_records": 20,
                                "max_candidate_clues": 10,
                                "max_llm_refine_clues": 2,
                                "max_elapsed_seconds": 20,
                            },
                        },
                        "error": None,
                    },
                )()
            return self._fallback.chat(messages, **kwargs)

    orchestrator = InvestigationOrchestrator(llm_gateway=_NoRewriteGateway())

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen_urls.append(str(source["source_url"]))
        return [
            {
                "trace_id": "no-rewrite-1",
                "source_name": str(source["source_name"]),
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "接码群控 TG:norewrite01 https://risk.example/norewrite",
            }
        ]

    result = orchestrator.run(
        "找最近接码群控相关线索",
        available_sources=[
            {
                "source_name": "no-rewrite-source",
                "source_type": "IM",
                "source_url": "https://search.example/?q=old",
                "query_url_template": "https://search.example/?q={query}",
                "search_query": "site:t.me/s 接码",
                "query_theme": "接码",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
            }
        ],
        collect_source_records=collect_source,
    )

    assert result.status == "completed"
    assert seen_urls == ["https://search.example/?q=old"]
    assert result.execution_summary["query_rewrite_count"] == 0
    assert result.execution_summary["plan_execution_controls"]["query_rewrite_policy"] == "off"
    assert result.llm_traces[2]["applied"] is False


def test_llm_plan_can_disable_llm_refine_while_preserving_candidate_output():
    class _NoRefineGateway:
        def __init__(self) -> None:
            self._fallback = LLMGateway(dry_run=True, mock=True)

        def chat(self, messages, **kwargs):  # noqa: ANN001
            user_message = str(messages[-1].get("content") or "")
            if "available_sources=" in user_message:
                return type(
                    "Resp",
                    (),
                    {
                        "ok": True,
                        "parsed_json": {
                            "goal": "collect_high_quality_risk_clues",
                            "agent_steps": [
                                {"agent": "intent_planner", "action": "parse_request_to_structured_intent"},
                                {"agent": "quality_review_agent", "action": "skip_llm_refine_and_return_candidate_clues"},
                            ],
                            "source_selection_strategy": {
                                "preferred_source_types": ["telegram", "forum"],
                                "match_query_keywords": ["诈骗引流"],
                            },
                            "execution_notes": ["refine_policy=off"],
                            "quality_gate": {
                                "quality_profile": "balanced",
                                "minimum_quality_score": 0.65,
                                "require_cross_source": True,
                                "require_evidence_chain": True,
                            },
                            "budget": {
                                "max_sources": 2,
                                "max_raw_records": 20,
                                "max_candidate_clues": 10,
                                "max_llm_refine_clues": 3,
                                "max_elapsed_seconds": 20,
                            },
                        },
                        "error": None,
                    },
                )()
            return self._fallback.chat(messages, **kwargs)

    orchestrator = InvestigationOrchestrator(llm_gateway=_NoRefineGateway())
    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        records=[
            {
                "trace_id": "norefine-1",
                "source_name": "tg-norefine-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:norefine01，落地 https://risk.example/norefine 第一条",
            },
            {
                "trace_id": "norefine-2",
                "source_name": "forum-norefine-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:norefine01，落地 https://risk.example/norefine 第二条",
            },
        ],
    )

    assert result.status == "completed"
    assert result.execution_summary["plan_execution_controls"]["refine_policy"] == "off"
    assert result.execution_summary["refined_clue_count"] == 0
    assert all("refinement" not in clue for clue in [*result.high_quality_clues, *result.candidate_clues])






def test_elapsed_budget_can_stop_clue_refine_before_any_llm_refinement(monkeypatch):
    orchestrator = InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))
    ticks = iter([0.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(
        "src.agent.investigation_orchestrator.time.perf_counter",
        lambda: next(ticks, 2.0),
    )
    result = orchestrator.run(
        "帮我找近24小时诈骗引流相关的高质量线索",
        records=[
            {
                "trace_id": "deadline-1",
                "source_name": "tg-deadline-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:deadline01，落地 https://risk.example/deadline 第一条",
            },
            {
                "trace_id": "deadline-2",
                "source_name": "forum-deadline-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:deadline01，落地 https://risk.example/deadline 第二条",
            },
            {
                "trace_id": "deadline-3",
                "source_name": "feed-deadline-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:deadline01，落地 https://risk.example/deadline 第三条",
            },
        ],
        policy_override=InvestigationPolicyOverride(max_elapsed_seconds=1, max_llm_refine_clues=5),
    )

    assert result.status == "completed"
    assert result.execution_summary["elapsed_budget_exhausted"] is True
    assert result.execution_summary["refined_clue_count"] == 0
    assert result.execution_summary["telemetry"]["elapsed_budget_exhausted"] is True


def test_expired_planning_budget_does_not_skip_first_live_collection(monkeypatch):
    orchestrator = InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))
    ticks = iter([0.0, 30.0, 30.0, 30.1, 30.1, 30.1, 30.1, 30.1, 31.2, 31.2])
    monkeypatch.setattr(
        "src.agent.investigation_orchestrator.time.perf_counter",
        lambda: next(ticks, 31.0),
    )
    seen: list[str] = []

    def collect_source(source: dict[str, object]) -> list[dict[str, object]]:
        seen.append(str(source["source_name"]))
        return [
            {
                "trace_id": "planning-budget-live-1",
                "source_name": source["source_name"],
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:budgetlive01，落地 https://risk.example/live 第一条",
            },
            {
                "trace_id": "planning-budget-live-2",
                "source_name": source["source_name"],
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:budgetlive01，落地 https://risk.example/live 第二条",
            },
            {
                "trace_id": "planning-budget-live-3",
                "source_name": source["source_name"],
                "source_type": source.get("source_type") or "IM",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:budgetlive01，落地 https://risk.example/live 第三条",
            },
        ]

    result = orchestrator.run(
        "找接码和群控相关线索",
        available_sources=[
            {
                "source_name": "planning-budget-source",
                "source_type": "IM",
                "source_url": "https://feed.example/planning-budget",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "query_theme": "接码",
                "query_term_stage": "core",
            }
        ],
        collect_source_records=collect_source,
        policy_override=InvestigationPolicyOverride(max_elapsed_seconds=1, max_llm_refine_clues=1),
    )

    assert seen == ["planning-budget-source"]
    assert result.execution_summary["used_live_collection"] is True
    assert result.execution_summary["orchestration_route"] == "live_collection_only"
    assert "elapsed_budget_reset_for_first_live_collection" in result.execution_summary["live_collection_reasons"]
