"""Controlled production-enforcement gateway.

BlackAgent may recommend defensive actions, but executing bans, blocks,
blacklists, or interception rules is high impact.  This module provides the
production-shaped adapter while keeping hard gates around approval, dry-run,
confidence thresholds, connector configuration, and auditability.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import uuid4


ACTION_ALIASES = {
    "封禁": "ban",
    "拉黑": "blacklist",
    "拦截": "intercept",
    "阻断": "block",
    "ban": "ban",
    "block": "block",
    "blacklist": "blacklist",
    "intercept": "intercept",
}
DEFAULT_ALLOWED_ACTIONS = ("ban", "block", "blacklist", "intercept")
DEFAULT_ALLOWED_TARGET_TYPES = ("account", "domain", "url", "ip", "phone", "device", "merchant", "group")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EnforcementAction:
    """One candidate production action generated after review/risk scoring."""

    action_id: str
    action_type: str
    target_type: str
    target_value: str
    reason: str
    evidence_trace_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source_strategy_id: str | None = None
    human_approved: bool = False
    approval_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EnforcementPolicy:
    """Runtime gates for high-impact defensive enforcement."""

    enabled: bool = False
    dry_run: bool = True
    require_human_approval: bool = True
    min_confidence: float = 0.95
    max_actions_per_run: int = 50
    allowed_actions: tuple[str, ...] = DEFAULT_ALLOWED_ACTIONS
    allowed_target_types: tuple[str, ...] = DEFAULT_ALLOWED_TARGET_TYPES
    connector: str = "audit"
    webhook_url: str | None = None
    webhook_token: str | None = None
    require_production_token: bool = True
    production_safety_token: str | None = None
    request_safety_token: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | Any, *, request_safety_token: str | None = None) -> "EnforcementPolicy":
        if hasattr(data, "model_dump"):
            raw = data.model_dump()
        elif is_dataclass(data):
            raw = asdict(data)
        else:
            raw = dict(data or {})
        return cls(
            enabled=bool(raw.get("enabled", False)),
            dry_run=bool(raw.get("dry_run", True)),
            require_human_approval=bool(raw.get("require_human_approval", True)),
            min_confidence=float(raw.get("min_confidence", 0.95)),
            max_actions_per_run=int(raw.get("max_actions_per_run", 50)),
            allowed_actions=tuple(_normalize_action(value) for value in raw.get("allowed_actions", DEFAULT_ALLOWED_ACTIONS)),
            allowed_target_types=tuple(str(value).lower() for value in raw.get("allowed_target_types", DEFAULT_ALLOWED_TARGET_TYPES)),
            connector=str(raw.get("connector", "audit") or "audit").lower(),
            webhook_url=raw.get("webhook_url"),
            webhook_token=raw.get("webhook_token"),
            require_production_token=bool(raw.get("require_production_token", True)),
            production_safety_token=raw.get("production_safety_token"),
            request_safety_token=request_safety_token,
        )


@dataclass(frozen=True)
class EnforcementResult:
    """Decision for one enforcement action."""

    action_id: str
    status: str
    reason: str
    dry_run: bool
    network_attempted: bool
    action: dict[str, Any]
    adapter_response: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class EnforcementAdapter(Protocol):
    def execute(self, action: EnforcementAction) -> dict[str, Any]:
        """Execute an already-authorized action against the production control plane."""


class WebhookEnforcementAdapter:
    """POST actions to a configured enforcement webhook."""

    def __init__(self, webhook_url: str, *, token: str | None = None, timeout_seconds: float = 10.0) -> None:
        self.webhook_url = webhook_url
        self.token = token
        self.timeout_seconds = timeout_seconds

    def execute(self, action: EnforcementAction) -> dict[str, Any]:
        payload = json.dumps(action.model_dump(), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "BlackAgent-EnforcementGateway/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib_request.Request(self.webhook_url, data=payload, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as response:  # noqa: S310 - URL is explicit config
                body = response.read().decode("utf-8", errors="replace")
                parsed = json.loads(body) if body else {}
                return {"ok": True, "status_code": getattr(response, "status", None), "body": parsed}
        except urllib_error.HTTPError as exc:
            return {"ok": False, "status_code": exc.code, "error": exc.read().decode("utf-8", errors="replace")}
        except urllib_error.URLError as exc:
            return {"ok": False, "error": str(exc.reason)}


class EnforcementGateway:
    """Apply deterministic gates before any high-impact production action."""

    def __init__(self, policy: EnforcementPolicy | None = None, *, adapter: EnforcementAdapter | None = None) -> None:
        self.policy = policy or EnforcementPolicy()
        self.adapter = adapter

    def execute(
        self,
        actions: Iterable[Mapping[str, Any] | EnforcementAction],
        *,
        policy: EnforcementPolicy | None = None,
    ) -> list[EnforcementResult]:
        active_policy = policy or self.policy
        normalized = [_coerce_action(action) for action in actions]
        if len(normalized) > active_policy.max_actions_per_run:
            overflow = normalized[active_policy.max_actions_per_run :]
            normalized = normalized[: active_policy.max_actions_per_run]
            results = [self._evaluate(action, active_policy) for action in normalized]
            results.extend(
                self._result(action, "BLOCKED", "max_actions_per_run_exceeded", active_policy, network_attempted=False)
                for action in overflow
            )
            return results
        return [self._evaluate(action, active_policy) for action in normalized]

    def _evaluate(self, action: EnforcementAction, policy: EnforcementPolicy) -> EnforcementResult:
        if not policy.enabled:
            return self._result(action, "BLOCKED", "enforcement_disabled", policy, network_attempted=False)
        if action.action_type not in policy.allowed_actions:
            return self._result(action, "BLOCKED", "action_not_allowed", policy, network_attempted=False)
        if action.target_type not in policy.allowed_target_types:
            return self._result(action, "BLOCKED", "target_type_not_allowed", policy, network_attempted=False)
        if action.confidence < policy.min_confidence:
            return self._result(action, "REVIEW_REQUIRED", "confidence_below_minimum", policy, network_attempted=False)
        if policy.require_human_approval and not action.human_approved:
            return self._result(action, "REVIEW_REQUIRED", "missing_human_approval", policy, network_attempted=False)
        if policy.dry_run:
            return self._result(action, "DRY_RUN", "dry_run_only_no_production_effect", policy, network_attempted=False)
        if policy.require_production_token:
            if not policy.production_safety_token or policy.request_safety_token != policy.production_safety_token:
                return self._result(action, "BLOCKED", "missing_or_invalid_production_safety_token", policy, network_attempted=False)
        adapter = self._adapter_for(policy)
        if adapter is None:
            return self._result(action, "BLOCKED", "no_production_enforcement_connector_configured", policy, network_attempted=False)
        adapter_response = adapter.execute(action)
        if adapter_response.get("ok") is False:
            return self._result(action, "FAILED", "connector_error", policy, network_attempted=True, adapter_response=adapter_response)
        return self._result(action, "EXECUTED", "connector_acknowledged", policy, network_attempted=True, adapter_response=adapter_response)

    def _adapter_for(self, policy: EnforcementPolicy) -> EnforcementAdapter | None:
        if self.adapter is not None:
            return self.adapter
        if policy.connector == "webhook" and policy.webhook_url:
            return WebhookEnforcementAdapter(policy.webhook_url, token=policy.webhook_token)
        return None

    def _result(
        self,
        action: EnforcementAction,
        status: str,
        reason: str,
        policy: EnforcementPolicy,
        *,
        network_attempted: bool,
        adapter_response: Mapping[str, Any] | None = None,
    ) -> EnforcementResult:
        return EnforcementResult(
            action_id=action.action_id,
            status=status,
            reason=reason,
            dry_run=policy.dry_run,
            network_attempted=network_attempted,
            action=action.model_dump(),
            adapter_response=dict(adapter_response or {}),
        )


def policy_with_request(policy: EnforcementPolicy, *, request_safety_token: str | None = None, force_dry_run: bool | None = None) -> EnforcementPolicy:
    """Return a per-request policy without weakening configured dry-run gates."""

    dry_run = policy.dry_run if force_dry_run is None else (policy.dry_run or force_dry_run)
    return replace(policy, request_safety_token=request_safety_token, dry_run=dry_run)


def _coerce_action(value: Mapping[str, Any] | EnforcementAction) -> EnforcementAction:
    if isinstance(value, EnforcementAction):
        return value
    data = dict(value)
    action_type = _normalize_action(data.get("action_type") or data.get("action") or data.get("type") or "")
    target_type = str(data.get("target_type") or data.get("target_kind") or "").lower().strip()
    target_value = str(data.get("target_value") or data.get("target") or data.get("value") or "").strip()
    if not action_type:
        raise ValueError("enforcement action requires action_type")
    if not target_type:
        raise ValueError("enforcement action requires target_type")
    if not target_value:
        raise ValueError("enforcement action requires target_value")
    confidence = float(data.get("confidence", 0.0) or 0.0)
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be between 0 and 1")
    evidence = data.get("evidence_trace_ids") or data.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    return EnforcementAction(
        action_id=str(data.get("action_id") or f"enforce_{uuid4().hex[:12]}"),
        action_type=action_type,
        target_type=target_type,
        target_value=target_value,
        reason=str(data.get("reason") or data.get("recommendation") or "risk_strategy_candidate"),
        evidence_trace_ids=[str(item) for item in evidence],
        confidence=confidence,
        source_strategy_id=str(data.get("source_strategy_id")) if data.get("source_strategy_id") else None,
        human_approved=bool(data.get("human_approved") or data.get("approved")),
        approval_id=str(data.get("approval_id")) if data.get("approval_id") else None,
        metadata=dict(metadata),
    )


def _normalize_action(value: Any) -> str:
    text = str(value or "").strip().lower()
    return ACTION_ALIASES.get(text, text)


__all__ = [
    "EnforcementAction",
    "EnforcementAdapter",
    "EnforcementGateway",
    "EnforcementPolicy",
    "EnforcementResult",
    "WebhookEnforcementAdapter",
    "policy_with_request",
]
