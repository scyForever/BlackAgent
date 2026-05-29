from src.backend import EnforcementGateway, EnforcementPolicy
from src.config_loader import Settings
from src.local_runtime import LocalAgentRuntime


class FakeAdapter:
    def __init__(self):
        self.actions = []

    def execute(self, action):
        self.actions.append(action)
        return {"ok": True, "ticket_id": "prod-ack-1"}


def candidate_action(**overrides):
    action = {
        "action_type": "blacklist",
        "target_type": "domain",
        "target_value": "risk.example",
        "reason": "confirmed shared malicious landing domain",
        "confidence": 0.99,
        "human_approved": True,
        "approval_id": "review-1",
        "evidence_trace_ids": ["trace-a", "trace-b"],
    }
    action.update(overrides)
    return action


def test_enforcement_gateway_blocks_by_default_and_dry_runs_when_enabled():
    disabled = EnforcementGateway().execute([candidate_action()])[0]
    assert disabled.status == "BLOCKED"
    assert disabled.reason == "enforcement_disabled"

    dry_policy = EnforcementPolicy(enabled=True, dry_run=True, require_human_approval=True)
    dry_result = EnforcementGateway(dry_policy).execute([candidate_action()])[0]
    assert dry_result.status == "DRY_RUN"
    assert dry_result.network_attempted is False


def test_enforcement_gateway_requires_confidence_approval_token_and_connector():
    policy = EnforcementPolicy(enabled=True, dry_run=False, production_safety_token="token-1")
    low_confidence = EnforcementGateway(policy).execute([candidate_action(confidence=0.5)])[0]
    assert low_confidence.status == "REVIEW_REQUIRED"
    assert low_confidence.reason == "confidence_below_minimum"

    missing_approval = EnforcementGateway(policy).execute([candidate_action(human_approved=False)])[0]
    assert missing_approval.status == "REVIEW_REQUIRED"
    assert missing_approval.reason == "missing_human_approval"

    missing_token = EnforcementGateway(policy).execute([candidate_action()])[0]
    assert missing_token.status == "BLOCKED"
    assert missing_token.reason == "missing_or_invalid_production_safety_token"

    no_connector_policy = EnforcementPolicy(
        enabled=True,
        dry_run=False,
        production_safety_token="token-1",
        request_safety_token="token-1",
    )
    no_connector = EnforcementGateway(no_connector_policy).execute([candidate_action()])[0]
    assert no_connector.status == "BLOCKED"
    assert no_connector.reason == "no_production_enforcement_connector_configured"

    adapter = FakeAdapter()
    execute_policy = EnforcementPolicy(
        enabled=True,
        dry_run=False,
        production_safety_token="token-1",
        request_safety_token="token-1",
    )
    executed = EnforcementGateway(execute_policy, adapter=adapter).execute([candidate_action()])[0]
    assert executed.status == "EXECUTED"
    assert executed.network_attempted is True
    assert executed.adapter_response["ticket_id"] == "prod-ack-1"
    assert len(adapter.actions) == 1


def test_enforcement_runtime_preserves_configured_dry_run_gate(tmp_path):
    settings = Settings(
        storage={"backend": "sql", "dsn": f"sqlite:///{(tmp_path / 'enforcement.db').as_posix()}", "auto_create_schema": True},
        enforcement={"enabled": True, "dry_run": True, "require_human_approval": True, "min_confidence": 0.9},
    )
    runtime = LocalAgentRuntime(settings)
    try:
        response = runtime.execute_enforcement([candidate_action()], approved=True, dry_run=False)
    finally:
        runtime.close()

    result = response["results"][0]
    assert result["status"] == "DRY_RUN"
    assert result["dry_run"] is True
