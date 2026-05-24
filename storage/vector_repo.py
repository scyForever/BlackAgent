"""In-memory pgvector-like semantic repository for Phase II."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from src.cleaner.text_filter import canonicalize_for_dedup, normalize_text


@dataclass(frozen=True)
class VectorRecord:
    item_id: str
    text: str
    vector: dict[str, float]
    metadata: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VectorSearchResult:
    item_id: str
    score: float
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class InMemoryVectorRepo:
    """Small adapter-shaped stand-in for PostgreSQL pgvector text search."""

    def __init__(self) -> None:
        self._records: dict[str, VectorRecord] = {}

    def upsert(self, item_id: str, text: str, metadata: Mapping[str, Any] | None = None) -> VectorRecord:
        record = VectorRecord(str(item_id), normalize_text(text), embed_text(text), dict(metadata or {}))
        self._records[record.item_id] = record
        return record

    def search(self, query: str, *, top_k: int = 5, min_score: float = 0.0) -> list[VectorSearchResult]:
        query_vector = embed_text(query)
        results: list[VectorSearchResult] = []
        for record in self._records.values():
            score = cosine_similarity(query_vector, record.vector)
            if score >= min_score:
                results.append(VectorSearchResult(record.item_id, round(score, 4), record.text, dict(record.metadata)))
        return sorted(results, key=lambda item: item.score, reverse=True)[:top_k]

    def list(self) -> list[VectorRecord]:
        return list(self._records.values())

    def clear(self) -> None:
        self._records.clear()


VectorRepo = InMemoryVectorRepo


def embed_text(text: str) -> dict[str, float]:
    canonical = canonicalize_for_dedup(text)
    if not canonical:
        return {}
    tokens: list[str] = []
    tokens.extend(canonical[index : index + 2] for index in range(max(1, len(canonical) - 1)))
    tokens.extend(part.lower() for part in normalize_text(text).replace("/", " ").replace(":", " ").split() if part)
    counts = Counter(tokens)
    norm = math.sqrt(sum(count * count for count in counts.values())) or 1.0
    return {token: count / norm for token, count in counts.items()}


def cosine_similarity(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    return sum(left[token] * right[token] for token in common)


__all__ = ["InMemoryVectorRepo", "VectorRepo", "VectorRecord", "VectorSearchResult", "cosine_similarity", "embed_text"]
