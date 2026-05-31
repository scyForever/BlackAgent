"""Workflow-facing exports under the product package namespace."""

from blackagent.agent import InvestigationOrchestrator, InvestigationRunResult

__all__ = ["InvestigationOrchestrator", "InvestigationRunResult"]
