"""Storage contracts and in-memory repositories for BlackAgent MVP."""

from .audit_repo import AuditRepo, InMemoryAuditRepo
from .cleaned_repo import CleanedTextRepo, InMemoryCleanedTextRepo
from .clue_repo import ClueRepo, InMemoryClueRepo
from .entity_repo import EntityRepo, InMemoryEntityRepo
from .entity_graph import EntityAsset, EntityGraphStore, EntityObservation, EntityRiskProfile, EntityRelation
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
from .sql_repos import (
    AuditSQLRepo,
    CleanedSQLRepo,
    ClueBatchSQLRepo,
    ClueSQLRepo,
    EntitySQLRepo,
    QueueSQLRepo,
    RawSQLRepo,
    ReviewSQLRepo,
    SchedulerSQLRepo,
    TaskSQLRepo,
    sql_repositories,
)
from .vector_repo import InMemoryVectorRepo, VectorRecord, VectorRepo, VectorSearchResult

__all__ = [
    "AuditEvent",
    "AuditRepo",
    "AuditSQLRepo",
    "BudgetConsumed",
    "ClassificationResult",
    "CleanedSQLRepo",
    "ClueBatchSQLRepo",
    "ClueRepo",
    "ClueSQLRepo",
    "CleanedText",
    "CleanedTextRepo",
    "EntityExtractionResult",
    "EntityAsset",
    "EntityGraphStore",
    "EntityObservation",
    "EntityRiskProfile",
    "EntityRepo",
    "EntityRelation",
    "EntitySQLRepo",
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
    "QueueSQLRepo",
    "RawIntelligence",
    "RawIntelligenceRepo",
    "RawSQLRepo",
    "ReviewDecision",
    "ReviewRepo",
    "ReviewSQLRepo",
    "ReviewState",
    "SchedulerSQLRepo",
    "SQLBackend",
    "TaskSQLRepo",
    "VectorRecord",
    "VectorRepo",
    "VectorSearchResult",
    "connect",
    "sql_repositories",
]
