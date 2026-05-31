"""Workflow result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class WorkflowResult:
    """Container returned by workflow execution before API rendering."""

    context: Any
    payload: Any


__all__ = ["WorkflowResult"]
