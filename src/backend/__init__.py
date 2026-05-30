"""Backend adapters for production-facing BlackAgent services.

This package intentionally stays independent from ``main.py`` so backend
workers can be wired into the API layer later without changing the existing
MVP and phase-2/3 pipelines.
"""

from .llm_gateway import LLMCallStats, LLMGateway, LLMGatewayConfig, LLMGatewayResponse
from .task_backend import TaskBackend, TaskError, TaskRecord, TaskStatus
from .enforcement import (
    EnforcementAction,
    EnforcementGateway,
    EnforcementPolicy,
    EnforcementResult,
    WebhookEnforcementAdapter,
    policy_with_request,
)

__all__ = [
    "EnforcementAction",
    "EnforcementGateway",
    "EnforcementPolicy",
    "EnforcementResult",
    "LLMCallStats",
    "LLMGateway",
    "LLMGatewayConfig",
    "LLMGatewayResponse",
    "TaskBackend",
    "TaskError",
    "TaskRecord",
    "TaskStatus",
    "WebhookEnforcementAdapter",
    "policy_with_request",
]
