"""Storage contracts and in-memory repositories for BlackAgent MVP."""

from .audit_repo import AuditRepo, InMemoryAuditRepo
from .cleaned_repo import CleanedTextRepo, InMemoryCleanedTextRepo
from .clue_repo import ClueRepo, InMemoryClueRepo
from .entity_repo import EntityRepo, InMemoryEntityRepo
from src.storage.entity_graph import EntityAsset, EntityGraphStore, EntityObservation, EntityRelation
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
    "ClueRepo",
    "CleanedText",
    "CleanedTextRepo",
    "EntityExtractionResult",
    "EntityAsset",
    "EntityGraphStore",
    "EntityObservation",
    "EntityRepo",
    "EntityRelation",
    "ExplorationHypothesis",
    "GraphEdge",
    "GraphNode",
    "GraphRepo",
    "HypothesisType",
    "InMemoryAuditRepo",
    "InMemoryCleanedTextRepo",
    "InMemoryClueRepo",
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
