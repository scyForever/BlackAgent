"""Small event helpers for audit-friendly application boundaries."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .models import AuditEvent


def make_audit_event(event_type: str, *, actor: str = "system", target_id: str | None = None, **payload: Any) -> AuditEvent:
    """Create a typed audit event with an explicit event type."""

    return AuditEvent(event_id=uuid4(), event_type=event_type, actor=actor, target_id=target_id, payload=payload)


__all__ = ["make_audit_event"]
