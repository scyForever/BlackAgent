"""Human-review application service."""

from __future__ import annotations

from typing import Any

from src.agent import InvestigationOrchestrator


class ReviewService:
    """Route review decisions back into the controlled exploration lifecycle."""

    def __init__(self, orchestrator: InvestigationOrchestrator) -> None:
        self.orchestrator = orchestrator

    def review(self, hypothesis_id: str, *, decision: str, reviewer: str = "system", **edits: Any) -> dict[str, Any]:
        return self.orchestrator.ingest_review_decision(hypothesis_id, decision=decision, reviewer=reviewer, **edits)


__all__ = ["ReviewService"]
