"""Current investigation-agent components for BlackAgent."""

from .budget_manager import BudgetExceeded, BudgetManager, BudgetSnapshot
from .budget_controller import BudgetController, RuntimeBudget
from .clue_ranker import ClueRanker, RankedClue
from .exploration_agent import ExplorationAgent
from .model_router import ModelRouteDecision, ModelRouter, RouteAction
from .policy_guard import PolicyGuard, SafetyPolicyViolation
from .query_rewriter import LLMSourceQueryRewriter, QueryRewriteTrace
from .services import (
    ClueMergeService,
    ClueRefinementService,
    InitialCandidateRetrievalService,
    IntentPlanningService,
    InvestigationTelemetryService,
    RunStatePreparationService,
    SourceSelectionService,
)
from .tool_registry import ToolRegistry, ToolRegistryViolation
from .user_request_parser import InvestigationPlan, LLMDecisionTrace, LLMInvestigationPlanner, LLMUserRequestParser, UserIntent

__all__ = [
    "BudgetExceeded",
    "BudgetController",
    "BudgetManager",
    "BudgetSnapshot",
    "ClueRanker",
    "ClueMergeService",
    "ClueRefinementService",
    "ExplorationAgent",
    "InitialCandidateRetrievalService",
    "InvestigationOrchestrator",
    "InvestigationPlan",
    "InvestigationRunResult",
    "InvestigationTelemetryService",
    "IntentPlanningService",
    "LLMDecisionTrace",
    "LLMInvestigationPlanner",
    "LLMSourceQueryRewriter",
    "LLMUserRequestParser",
    "ModelRouteDecision",
    "ModelRouter",
    "PolicyGuard",
    "QueryRewriteTrace",
    "RankedClue",
    "RouteAction",
    "RuntimeBudget",
    "SafetyPolicyViolation",
    "RunStatePreparationService",
    "SourceSelectionService",
    "ToolRegistry",
    "ToolRegistryViolation",
    "UserIntent",
]


def __getattr__(name: str):
    """Load the top-level orchestrator lazily to avoid pipeline import cycles."""

    if name in {"InvestigationOrchestrator", "InvestigationRunResult"}:
        from .investigation_orchestrator import InvestigationOrchestrator, InvestigationRunResult

        return {
            "InvestigationOrchestrator": InvestigationOrchestrator,
            "InvestigationRunResult": InvestigationRunResult,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
