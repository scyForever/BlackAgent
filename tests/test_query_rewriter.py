from src.agent.query_rewriter import LLMSourceQueryRewriter
from src.backend.llm_gateway import LLMGatewayResponse


class _RewriteGateway:
    def __init__(self, parsed_json):
        self._parsed_json = parsed_json

    def chat(self, messages, **kwargs):  # noqa: ANN001
        return LLMGatewayResponse(
            ok=True,
            model="test-model",
            content="{}",
            parsed_json=self._parsed_json,
            network_attempted=False,
        )


def test_query_rewriter_rewrites_search_query_and_source_url_from_llm():
    rewriter = LLMSourceQueryRewriter(
        _RewriteGateway(
            {
                "search_query": "site:t.me/s 接码 群控",
                "query_theme": "接码",
                "query_term": "群控",
                "query_term_stage": "core",
                "rewrite_reason": "focus_on_live_signal",
            }
        )
    )

    rewritten, trace = rewriter.rewrite(
        {
            "source_name": "telegram-search",
            "source_url": "https://search.example/?q=old",
            "query_url_template": "https://search.example/?q={query}",
            "search_query": "site:t.me/s 接码",
            "query_seed_terms": ["site:t.me/s"],
            "query_theme": "接码",
            "query_term_stage": "core",
        },
        query="找最近接码群控相关线索",
        intent={"include_keywords": ["接码", "群控"], "risk_types": ["工具交易"]},
        plan={"goal": "collect_high_quality_risk_clues"},
    )

    assert trace.applied is True
    assert trace.used_fallback is False
    assert rewritten["search_query"] == "site:t.me/s 接码 群控"
    assert rewritten["source_url"] == "https://search.example/?q=site%3At.me%2Fs%20%E6%8E%A5%E7%A0%81%20%E7%BE%A4%E6%8E%A7"
    assert rewritten["source_url_before_rewrite"] == "https://search.example/?q=old"
    assert rewritten["query_rewrite_reason"] == "focus_on_live_signal"


def test_query_rewriter_falls_back_to_existing_search_query_when_llm_payload_is_unusable():
    rewriter = LLMSourceQueryRewriter(_RewriteGateway({"rewrite_reason": "missing_search_query"}))

    rewritten, trace = rewriter.rewrite(
        {
            "source_name": "forum-search",
            "source_url": "https://search.example/?q=old",
            "query_url_template": "https://search.example/?q={query}",
            "search_query": "site:tieba.baidu.com/p 私域导流",
            "query_theme": "诈骗引流",
            "query_term": "私域导流",
            "query_term_stage": "core",
        },
        query="找当天诈骗引流线索",
        intent={"include_keywords": ["诈骗引流"], "risk_types": ["诈骗引流"]},
        plan={"goal": "collect_high_quality_risk_clues"},
    )

    assert trace.applied is True
    assert trace.used_fallback is True
    assert rewritten["search_query"] == "site:tieba.baidu.com/p 私域导流"
    assert rewritten["query_rewrite_reason"] == "fallback_existing_search_query"
