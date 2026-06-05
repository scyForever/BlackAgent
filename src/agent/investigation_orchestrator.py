"""Public investigation orchestrator wrapper.

The workflow/runtime implementation lives in ``investigation_runtime`` so this
public class stays small and only owns API construction plus compatibility
exports used by tests and callers.
"""

from __future__ import annotations

import time  # re-exported for existing monkeypatch targets

from .investigation_runtime import (
    EvidenceGap,
    InvestigationRunResult,
    InvestigationRuntime,
    PlanExecutionControls,
    RuntimeQualityGate,
    SourceCollector,
    _FreshProcessingState,
    _LiveCollectionState,
    _RefinementState,
    _RetrievalState,
    _RunPlanningState,
    _SemanticLocalState,
)


class InvestigationOrchestrator(InvestigationRuntime):
    """Thin public orchestrator over the workflow-backed runtime."""


__all__ = [
    "InvestigationOrchestrator",
    "EvidenceGap",
    "InvestigationRunResult",
    "PlanExecutionControls",
    "RuntimeQualityGate",
    "SourceCollector",
    "_FreshProcessingState",
    "_LiveCollectionState",
    "_RefinementState",
    "_RetrievalState",
    "_RunPlanningState",
    "_SemanticLocalState",
]
