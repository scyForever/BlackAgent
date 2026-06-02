"""Phase II/III enhancement components for BlackAgent.

These modules implement deterministic, local equivalents of the PRD's advanced
capabilities. External systems such as pgvector and Neo4j are represented by
adapter-shaped in-memory stores so tests can verify contracts without requiring
production infrastructure.
"""

from .engine import PhaseTwoThreeEngine
from .strategy import (
    CountermeasureStrategy,
    CountermeasureSummary,
    CountermeasureSummaryBuilder,
    CheatingPlaybook,
    EvidenceChain,
    EvidenceChainRenderer,
    RiskClue,
)
from .lifecycle import DynamicSlangLifecycleManager, PromptEvaluator
from .source_intake import AuthorizedSourcePolicy, ComplianceSourceDiscovery, MultimodalTextExtractor
from .text_intelligence import SlangDictionary

__all__ = [
    "AuthorizedSourcePolicy",
    "ComplianceSourceDiscovery",
    "CountermeasureStrategy",
    "CountermeasureSummary",
    "CountermeasureSummaryBuilder",
    "CheatingPlaybook",
    "DynamicSlangLifecycleManager",
    "EvidenceChain",
    "EvidenceChainRenderer",
    "MultimodalTextExtractor",
    "PhaseTwoThreeEngine",
    "PromptEvaluator",
    "RiskClue",
    "SlangDictionary",
]
