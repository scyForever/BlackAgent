"""Current investigation-agent components for BlackAgent."""

from .budget_manager import BudgetExceeded, BudgetManager, BudgetSnapshot
from .budget_controller import BudgetController, BudgetLedger, BudgetLease, RuntimeBudget, StageBudgetStats
from .clue_ranker import ClueRanker, RankedClue
from .exploration_agent import ExplorationAgent
from .model_router import ModelRouteDecision, ModelRouter, RouteAction
from .policy_guard import PolicyGuard, SafetyPolicyViolation
from .query_rewriter import LLMSourceQueryRewriter, QueryRewriteTrace
from .runtime_services import (
    ExecutionSummaryService,
    ExecutionSummaryDependencies,
    FreshProcessingService,
    FreshProcessingDependencies,
    LiveCollectionService,
    LiveCollectionDependencies,
    PhaseDependency,
    RefinementDependencies,
    ResultRenderService,
    SemanticLocalRetrievalDependencies,
    SemanticLocalRetrievalService,
)
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
    "BudgetLedger",
    "BudgetLease",
    "BudgetManager",
    "BudgetSnapshot",
    "ClueRanker",
    "ClueMergeService",
    "ClueRefinementService",
    "ExplorationAgent",
    "ExecutionSummaryService",
    "ExecutionSummaryDependencies",
    "FreshProcessingService",
    "FreshProcessingDependencies",
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
    "LiveCollectionService",
    "LiveCollectionDependencies",
    "ModelRouteDecision",
    "ModelRouter",
    "PhaseDependency",
    "RefinementDependencies",
    "PolicyGuard",
    "QueryRewriteTrace",
    "RankedClue",
    "ResultRenderService",
    "RouteAction",
    "RuntimeBudget",
    "StageBudgetStats",
    "SafetyPolicyViolation",
    "SemanticLocalRetrievalService",
    "SemanticLocalRetrievalDependencies",
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
