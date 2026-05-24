"""Storage contracts and in-memory repositories for BlackAgent MVP."""

from .audit_repo import AuditRepo, InMemoryAuditRepo
from .cleaned_repo import CleanedTextRepo, InMemoryCleanedTextRepo
from .entity_repo import EntityRepo, InMemoryEntityRepo
from .graph_repo import GraphEdge, GraphNode, GraphRepo, InMemoryGraphRepo
from .raw_repo import InMemoryRawIntelligenceRepo, RawIntelligenceRepo
from .review_repo import InMemoryReviewRepo, ReviewRepo, ReviewState
from .schemas import (
    AuditEvent,
    BudgetConsumed,
    ClassificationResult,
    CleanedText,
    EntityExtractionResult,
    ExplorationHypothesis,
    HypothesisType,
    LegalBasis,
    RawIntelligence,
    ReviewDecision,
)
from .sql_backend import SQLBackend, connect
from .vector_repo import InMemoryVectorRepo, VectorRecord, VectorRepo, VectorSearchResult

__all__ = [
    "AuditEvent",
    "AuditRepo",
    "BudgetConsumed",
    "ClassificationResult",
    "CleanedText",
    "CleanedTextRepo",
    "EntityExtractionResult",
    "EntityRepo",
    "ExplorationHypothesis",
    "GraphEdge",
    "GraphNode",
    "GraphRepo",
    "HypothesisType",
    "InMemoryAuditRepo",
    "InMemoryCleanedTextRepo",
    "InMemoryEntityRepo",
    "InMemoryGraphRepo",
    "InMemoryRawIntelligenceRepo",
    "InMemoryReviewRepo",
    "InMemoryVectorRepo",
    "LegalBasis",
    "RawIntelligence",
    "RawIntelligenceRepo",
    "ReviewDecision",
    "ReviewRepo",
    "ReviewState",
    "SQLBackend",
    "VectorRecord",
    "VectorRepo",
    "VectorSearchResult",
    "connect",
]
