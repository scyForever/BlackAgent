"""Budget management for controlled exploration rounds."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class BudgetSnapshot:
    rounds: int
    tokens: int
    elapsed_ms: int
    max_rounds: int
    max_tokens: int
    max_elapsed_ms: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @property
    def consumed(self) -> dict[str, int]:
        return {"rounds": self.rounds, "tokens": self.tokens, "elapsed_ms": self.elapsed_ms}


class BudgetExceeded(RuntimeError):
    """Raised when the sandbox exceeds its configured budget."""

    def __init__(self, reason: str, snapshot: BudgetSnapshot) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot


class BudgetManager:
    """Track rounds, estimated tokens, and elapsed milliseconds."""

    def __init__(self, *, max_rounds: int = 3, max_tokens: int = 2_000, max_elapsed_ms: int = 25_000) -> None:
        if max_rounds <= 0 or max_tokens <= 0 or max_elapsed_ms <= 0:
            raise ValueError("Budget limits must be positive integers.")
        self.max_rounds = max_rounds
        self.max_tokens = max_tokens
        self.max_elapsed_ms = max_elapsed_ms
        self.reset()

    def reset(self) -> None:
        self._rounds = 0
        self._tokens = 0
        self._extra_elapsed_ms = 0
        self._started_at = time.monotonic()

    def consume(self, *, rounds: int = 0, tokens: int = 0, elapsed_ms: int = 0) -> BudgetSnapshot:
        """Consume budget and raise once any hard limit is exceeded."""

        if rounds < 0 or tokens < 0 or elapsed_ms < 0:
            raise ValueError("Budget consumption values cannot be negative.")
        self._rounds += rounds
        self._tokens += tokens
        self._extra_elapsed_ms += elapsed_ms
        return self.assert_within_budget()

    def assert_within_budget(self) -> BudgetSnapshot:
        snapshot = self.snapshot()
        if snapshot.rounds > self.max_rounds:
            raise BudgetExceeded("max_rounds exceeded", snapshot)
        if snapshot.tokens > self.max_tokens:
            raise BudgetExceeded("max_tokens exceeded", snapshot)
        if snapshot.elapsed_ms > self.max_elapsed_ms:
            raise BudgetExceeded("max_elapsed_ms exceeded", snapshot)
        return snapshot

    def snapshot(self) -> BudgetSnapshot:
        elapsed_ms = int((time.monotonic() - self._started_at) * 1000) + self._extra_elapsed_ms
        return BudgetSnapshot(
            rounds=self._rounds,
            tokens=self._tokens,
            elapsed_ms=elapsed_ms,
            max_rounds=self.max_rounds,
            max_tokens=self.max_tokens,
            max_elapsed_ms=self.max_elapsed_ms,
        )

    def remaining(self) -> dict[str, int]:
        snapshot = self.snapshot()
        return {
            "rounds": max(self.max_rounds - snapshot.rounds, 0),
            "tokens": max(self.max_tokens - snapshot.tokens, 0),
            "elapsed_ms": max(self.max_elapsed_ms - snapshot.elapsed_ms, 0),
        }

