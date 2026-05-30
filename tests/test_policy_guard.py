import pytest

from src.agent.budget_manager import BudgetExceeded, BudgetManager
from src.agent.policy_guard import PolicyGuard, SafetyPolicyViolation
from src.agent.tool_registry import ToolRegistry, ToolRegistryViolation


def test_policy_guard_blocks_hard_red_lines():
    guard = PolicyGuard()
    forbidden_actions = [
        {"type": "write", "target": "formal_entity_repo", "payload": "自动写入正式库"},
        {"type": "online_enforcement", "payload": "自动拉黑并下发线上封禁"},
        {"type": "send", "destination": "external_api", "payload": "对外发送账号 PII 和手机号"},
        {"type": "collect", "payload": "越权扩展未授权采集源并绕过验证码"},
    ]

    for action in forbidden_actions:
        with pytest.raises(SafetyPolicyViolation):
            guard.check_action_safety(action)


def test_policy_guard_allows_review_only_sandbox_actions():
    guard = PolicyGuard()

    assert guard.check_action_safety({"type": "tool_call", "tool": "local_db_lookup", "target": "local_sandbox"})
    assert guard.check_action_safety({"type": "write", "target": "review_repo", "payload": {"requires_human_review": True}})
    assert guard.assert_review_only({"requires_human_review": True, "target_repo": "review_repo"})


def test_policy_guard_allows_local_review_repo_to_store_contact_evidence():
    guard = PolicyGuard()

    assert guard.check_action_safety(
        {
            "type": "write",
            "target": "review_repo",
            "payload": {
                "requires_human_review": True,
                "hypothesis_summary": "样本进入受控探索：实体线索 contact:core01，来源 external public source。",
            },
        }
    )


def test_policy_guard_rejects_non_review_hypothesis():
    with pytest.raises(SafetyPolicyViolation):
        PolicyGuard().assert_review_only({"requires_human_review": False, "target_repo": "review_repo"})


def test_tool_registry_enforces_local_whitelist():
    registry = ToolRegistry()

    assert "local_db_lookup" in registry.list_tools()
    assert registry.call("local_db_lookup", "接码", corpus=[{"trace_id": "r1", "text": "接码平台"}]) == [
        {"trace_id": "r1", "text": "接码平台"}
    ]
    with pytest.raises(ToolRegistryViolation):
        registry.call("external_http_request", "https://example.com")


def test_budget_manager_stops_when_limits_are_exceeded():
    manager = BudgetManager(max_rounds=1, max_tokens=10, max_elapsed_ms=1000)

    manager.consume(rounds=1, tokens=5)
    with pytest.raises(BudgetExceeded):
        manager.consume(rounds=1)

    manager.reset()
    with pytest.raises(BudgetExceeded):
        manager.consume(tokens=11)
