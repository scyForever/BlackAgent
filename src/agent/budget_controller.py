"""Runtime budget controller for investigation and LLM stages."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RuntimeBudget:
    max_elapsed_seconds: int = 180
    max_sources: int | None = None
    max_raw_records: int = 5000
    max_candidate_clues: int = 50
    max_llm_calls: int = 20
    max_llm_tokens: int = 20_000
    max_llm_classify_records: int = 20
    max_llm_extract_records: int = 20
    max_llm_refine_clues: int = 20
    max_query_rewrite_sources: int = 5

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RuntimeBudget":
        return cls(
            max_elapsed_seconds=_positive_int(payload.get("max_elapsed_seconds"), cls.max_elapsed_seconds),
            max_sources=_optional_positive_int(payload.get("max_sources")),
            max_raw_records=_positive_int(payload.get("max_raw_records"), cls.max_raw_records),
            max_candidate_clues=_positive_int(payload.get("max_candidate_clues"), cls.max_candidate_clues),
            max_llm_calls=_positive_int(payload.get("max_llm_calls"), cls.max_llm_calls),
            max_llm_tokens=_positive_int(payload.get("max_llm_tokens"), cls.max_llm_tokens),
            max_llm_classify_records=_positive_int(
                payload.get("max_llm_classify_records"),
                cls.max_llm_classify_records,
            ),
            max_llm_extract_records=_positive_int(
                payload.get("max_llm_extract_records"),
                cls.max_llm_extract_records,
            ),
            max_llm_refine_clues=_positive_int(payload.get("max_llm_refine_clues"), cls.max_llm_refine_clues),
            max_query_rewrite_sources=_positive_int(
                payload.get("max_query_rewrite_sources"),
                cls.max_query_rewrite_sources,
            ),
        )

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class BudgetController:
    """Hard budget checks for LLM call count and estimated tokens."""

    def __init__(self, budget: RuntimeBudget) -> None:
        self.budget = budget
        self.llm_calls = 0
        self.llm_refine_calls = 0
        self.estimated_tokens = 0
        self.refined_clues = 0
        self.classified_by_llm = 0
        self.extracted_by_llm = 0
        self.started_at = time.perf_counter()

    def allow_llm_call(self, *, stage: str, estimated_tokens: int, item_count: int = 1) -> bool:
        item_count = max(1, int(item_count or 1))
        if self.elapsed_seconds() > self.budget.max_elapsed_seconds:
            return False
        if self.llm_calls + 1 > self.budget.max_llm_calls:
            return False
        if self.estimated_tokens + max(0, int(estimated_tokens or 0)) > self.budget.max_llm_tokens:
            return False
        if stage == "clue_refine" and self.refined_clues + item_count > self.budget.max_llm_refine_clues:
            return False
        if stage == "llm_classify" and self.classified_by_llm + item_count > self.budget.max_llm_classify_records:
            return False
        if stage == "llm_extract" and self.extracted_by_llm + item_count > self.budget.max_llm_extract_records:
            return False
        return True

    def consume_llm(self, *, stage: str, estimated_tokens: int, item_count: int = 1) -> None:
        item_count = max(1, int(item_count or 1))
        self.llm_calls += 1
        self.estimated_tokens += max(0, int(estimated_tokens or 0))
        if stage == "clue_refine":
            self.llm_refine_calls += 1
            self.refined_clues += item_count
        elif stage == "llm_classify":
            self.classified_by_llm += item_count
        elif stage == "llm_extract":
            self.extracted_by_llm += item_count

    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def snapshot(self) -> dict[str, Any]:
        return {
            "budget": self.budget.model_dump(),
            "llm_calls": self.llm_calls,
            "estimated_tokens": self.estimated_tokens,
            "refined_clues": self.refined_clues,
            "llm_refine_calls": self.llm_refine_calls,
            "llm_refined_clue_count": self.refined_clues,
            "classified_by_llm": self.classified_by_llm,
            "extracted_by_llm": self.extracted_by_llm,
            "elapsed_seconds": round(self.elapsed_seconds(), 4),
        }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


__all__ = ["BudgetController", "RuntimeBudget"]
