"""Workflow context contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkflowContext:
    """Explicit context passed across investigation workflow steps."""

    query: str
    run_state: Any
    retrieval_state: Any
    semantic_state: Any | None = None
    live_state: Any | None = None
    fresh_state: Any | None = None
    refinement_state: Any | None = None
    execution_summary: dict[str, Any] = field(default_factory=dict)
    main_flow_stages: list[dict[str, Any]] = field(default_factory=list)


__all__ = ["WorkflowContext"]
