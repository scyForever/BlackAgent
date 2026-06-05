from src.agent.policy_guard import SafetyPolicyViolation
from src.agent.user_request_parser import (
    LLMInvestigationPlanner,
    LLMUserRequestParser,
    UserIntent,
)


class _Resp:
    def __init__(self, parsed_json, *, ok=True, error=None):
        self.ok = ok
        self.parsed_json = parsed_json
        self.error = error


def test_simple_query_uses_rule_parser_without_llm_call():
    class _NoCallGateway:
        def chat(self, *_args, **_kwargs):  # noqa: ANN001
            raise AssertionError("simple query should not call LLM")

    intent, trace = LLMUserRequestParser(_NoCallGateway()).parse("找接码线索")

    assert intent.risk_types == ["接码"]
    assert intent.include_keywords
    assert trace.stage == "intent_parse"
    assert trace.llm_ok is False
    assert trace.used_fallback is False
    assert trace.parsed_json["parser_mode"] == "rule"
    assert trace.parsed_json["reason"] == "simple_query_rule_parser"


def test_complex_query_uses_fixed_intent_schema_response_format():
    captured = {}

    class _SchemaGateway:
        def chat(self, messages, **kwargs):  # noqa: ANN001
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return _Resp(
                {
                    "goal": "collect_high_quality_risk_clues",
                    "risk_types": ["诈骗引流"],
                    "source_preferences": ["telegram", "forum"],
                    "include_keywords": ["火苗", "跨源"],
                    "exclude_keywords": [],
                    "time_range_hours": 72,
                    "quality_profile": "high_precision",
                    "output_type": "clue_cards",
                    "require_cross_source": True,
                    "require_evidence_chain": True,
                }
            )

    intent, trace = LLMUserRequestParser(_SchemaGateway()).parse(
        "请比较 telegram 和论坛近72小时火苗相关线索，要求跨源证据链和可复核报告。"
    )

    assert intent.risk_types == ["诈骗引流"]
    assert intent.require_cross_source is True
    assert trace.llm_ok is True
    assert trace.used_fallback is False
    response_format = captured["kwargs"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "blackagent_intent_parse"


def test_planner_policy_guard_rejects_unsafe_llm_actions_and_falls_back():
    class _UnsafePlanGateway:
        def chat(self, _messages, **_kwargs):  # noqa: ANN001
            return _Resp(
                {
                    "goal": "collect_high_quality_risk_clues",
                    "agent_steps": [
                        {"agent": "intent_planner", "action": "parse_request_to_structured_intent"},
                        {"agent": "writer", "action": "自动写入正式实体库并下发线上封禁"},
                    ],
                    "source_selection_strategy": {"preferred_source_types": ["telegram"]},
                    "execution_notes": [],
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
                        "max_llm_refine_clues": 2,
                        "max_elapsed_seconds": 20,
                    },
                }
            )

    intent = UserIntent(
        goal="collect_high_quality_risk_clues",
        risk_types=["诈骗引流"],
        source_preferences=["telegram"],
        include_keywords=["诈骗引流"],
        exclude_keywords=[],
        time_range_hours=24,
        quality_profile="balanced",
        output_type="clue_cards",
        require_cross_source=False,
        require_evidence_chain=True,
        raw_query="帮我找诈骗引流线索",
    )

    plan, trace = LLMInvestigationPlanner(_UnsafePlanGateway()).plan("帮我找诈骗引流线索", intent)

    assert plan.llm_ok is False
    assert plan.llm_reason == "fallback_plan"
    assert trace.used_fallback is True
    assert "policy_guard" in str(trace.error)
    assert all(step["agent"] != "writer" for step in plan.agent_steps)
