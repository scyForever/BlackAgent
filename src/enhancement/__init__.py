"""Phase II/III enhancement components for BlackAgent.

These modules implement deterministic, local equivalents of the PRD's advanced
capabilities. External systems such as pgvector and Neo4j are represented by
adapter-shaped in-memory stores so tests can verify contracts without requiring
production infrastructure.
"""

from .engine import PhaseTwoThreeEngine
from .strategy import CountermeasureStrategy, CheatingPlaybook, RiskClue
from .lifecycle import DynamicSlangLifecycleManager, PromptEvaluator
from .source_intake import AuthorizedSourcePolicy, ComplianceSourceDiscovery, MultimodalTextExtractor

__all__ = [
    "AuthorizedSourcePolicy",
    "ComplianceSourceDiscovery",
    "CountermeasureStrategy",
    "CheatingPlaybook",
    "DynamicSlangLifecycleManager",
    "MultimodalTextExtractor",
    "PhaseTwoThreeEngine",
    "PromptEvaluator",
    "RiskClue",
]
