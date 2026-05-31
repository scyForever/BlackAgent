"""Runtime budget controller for investigation and LLM stages."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
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


@dataclass
class StageBudgetStats:
    attempted_calls: int = 0
    allowed_calls: int = 0
    denied_calls: int = 0
    cache_hit_calls: int = 0
    network_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    attempted_tokens: int = 0
    consumed_tokens: int = 0
    denied_tokens: int = 0
    cached_tokens: int = 0

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BudgetLedger:
    attempted_calls: int = 0
    allowed_calls: int = 0
    denied_calls: int = 0
    cache_hit_calls: int = 0
    network_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    attempted_tokens: int = 0
    consumed_tokens: int = 0
    denied_tokens: int = 0
    cached_tokens: int = 0
    by_stage: dict[str, StageBudgetStats] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["by_stage"] = {stage: stats.model_dump() for stage, stats in self.by_stage.items()}
        return payload

    def _stage(self, stage: str) -> StageBudgetStats:
        return self.by_stage.setdefault(str(stage or "unknown"), StageBudgetStats())

    def record_attempt(self, *, stage: str, estimated_tokens: int) -> None:
        tokens = max(0, int(estimated_tokens or 0))
        bucket = self._stage(stage)
        self.attempted_calls += 1
        self.attempted_tokens += tokens
        bucket.attempted_calls += 1
        bucket.attempted_tokens += tokens

    def record_denial(self, *, stage: str, estimated_tokens: int) -> None:
        tokens = max(0, int(estimated_tokens or 0))
        bucket = self._stage(stage)
        self.denied_calls += 1
        self.denied_tokens += tokens
        bucket.denied_calls += 1
        bucket.denied_tokens += tokens

    def record_allowance(self, *, stage: str, estimated_tokens: int, allowed: bool) -> None:
        """Backward-compatible combined allowance record.

        New budget callers should use ``reserve``/``consume`` so pre-checks do
        not inflate the ledger.
        """

        self.record_attempt(stage=stage, estimated_tokens=estimated_tokens)
        if allowed:
            bucket = self._stage(stage)
            self.allowed_calls += 1
            bucket.allowed_calls += 1
        else:
            self.record_denial(stage=stage, estimated_tokens=estimated_tokens)

    def record_consumed(
        self,
        *,
        stage: str,
        estimated_tokens: int,
        cache_hit: bool = False,
        ok: bool = True,
        network: bool = False,
    ) -> None:
        tokens = max(0, int(estimated_tokens or 0))
        bucket = self._stage(stage)
        self.consumed_tokens += tokens
        bucket.consumed_tokens += tokens
        if cache_hit:
            self.cache_hit_calls += 1
            self.cached_tokens += tokens
            bucket.cache_hit_calls += 1
            bucket.cached_tokens += tokens
        if network:
            self.network_calls += 1
            bucket.network_calls += 1
        if ok:
            self.successful_calls += 1
            bucket.successful_calls += 1
        else:
            self.failed_calls += 1
            bucket.failed_calls += 1


@dataclass(frozen=True)
class BudgetLease:
    stage: str
    estimated_tokens: int
    item_count: int = 1
    lease_id: str = ""

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
        self.ledger = BudgetLedger()

    def peek(self, *, stage: str, estimated_tokens: int, item_count: int = 1) -> bool:
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

    def reserve(self, *, stage: str, estimated_tokens: int, item_count: int = 1) -> BudgetLease | None:
        item_count = max(1, int(item_count or 1))
        estimated_tokens = max(0, int(estimated_tokens or 0))
        self.ledger.record_attempt(stage=stage, estimated_tokens=estimated_tokens)
        if not self.peek(stage=stage, estimated_tokens=estimated_tokens, item_count=item_count):
            self.ledger.record_denial(stage=stage, estimated_tokens=estimated_tokens)
            return None
        bucket = self.ledger._stage(stage)
        self.ledger.allowed_calls += 1
        bucket.allowed_calls += 1
        return BudgetLease(
            stage=str(stage or "unknown"),
            estimated_tokens=estimated_tokens,
            item_count=item_count,
            lease_id=f"lease:{self.ledger.allowed_calls}:{stage}",
        )

    def allow_llm_call(self, *, stage: str, estimated_tokens: int, item_count: int = 1) -> bool:
        """Compatibility pre-check; intentionally does not write the ledger."""

        return self.peek(stage=stage, estimated_tokens=estimated_tokens, item_count=item_count)

    def consume_llm(
        self,
        *,
        stage: str,
        estimated_tokens: int,
        item_count: int = 1,
        cache_hit: bool = False,
        ok: bool = True,
        network: bool = False,
        _reserved: bool = False,
    ) -> None:
        item_count = max(1, int(item_count or 1))
        if cache_hit:
            if not _reserved:
                self.ledger.record_attempt(stage=stage, estimated_tokens=estimated_tokens)
            self.ledger.record_consumed(
                stage=stage,
                estimated_tokens=estimated_tokens,
                cache_hit=True,
                ok=ok,
                network=network,
            )
            return
        if not _reserved:
            self.ledger.record_attempt(stage=stage, estimated_tokens=estimated_tokens)
            bucket = self.ledger._stage(stage)
            self.ledger.allowed_calls += 1
            bucket.allowed_calls += 1
        self.llm_calls += 1
        self.estimated_tokens += max(0, int(estimated_tokens or 0))
        if stage == "clue_refine":
            self.llm_refine_calls += 1
            self.refined_clues += item_count
        elif stage == "llm_classify":
            self.classified_by_llm += item_count
        elif stage == "llm_extract":
            self.extracted_by_llm += item_count
        self.ledger.record_consumed(
            stage=stage,
            estimated_tokens=estimated_tokens,
            cache_hit=cache_hit,
            ok=ok,
            network=network,
        )

    def consume(
        self,
        lease: BudgetLease | Mapping[str, Any],
        *,
        ok: bool = True,
        cache_hit: bool = False,
        network: bool = False,
    ) -> None:
        payload = lease.model_dump() if hasattr(lease, "model_dump") else dict(lease)
        self.consume_llm(
            stage=str(payload.get("stage") or "unknown"),
            estimated_tokens=int(payload.get("estimated_tokens") or 0),
            item_count=int(payload.get("item_count") or 1),
            cache_hit=cache_hit,
            ok=ok,
            network=network,
            _reserved=True,
        )

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
            "llm_budget": self.ledger.model_dump(),
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


__all__ = ["BudgetController", "BudgetLedger", "BudgetLease", "RuntimeBudget", "StageBudgetStats"]
