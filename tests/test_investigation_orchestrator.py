from src.agent import InvestigationOrchestrator
from src.backend import LLMGateway


def _orchestrator() -> InvestigationOrchestrator:
    return InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))


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
