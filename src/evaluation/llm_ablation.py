"""Reusable LLM ablation helpers for value/cost reporting."""

from __future__ import annotations

from typing import Any


def run_llm_ablation(records: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Run the standard fast/off vs high_recall/off/mock comparison."""

    from scripts.evaluate_pipeline import evaluate_ablation

    return evaluate_ablation(records, **kwargs)


class LLMValueGate:
    """Offline value gate for deciding whether record enrichment is worthwhile."""

    def __init__(self, *, min_f1_gain: float = 0.01, max_tokens_per_valid_delta: float = 10_000.0) -> None:
        self.min_f1_gain = min_f1_gain
        self.max_tokens_per_valid_delta = max_tokens_per_valid_delta

    def should_enable_record_enrich(self, profile: str, recent_metrics: dict[str, Any]) -> bool:
        if str(profile or "").strip().lower() == "fast":
            return False
        llm_gain = max(
            float(recent_metrics.get("classification_f1_delta") or 0.0),
            float(recent_metrics.get("entity_f1_delta") or 0.0),
            float(recent_metrics.get("clue_recall_delta") or 0.0),
        )
        tokens_per_valid_delta = recent_metrics.get("tokens_per_extra_valid_clue")
        if llm_gain < self.min_f1_gain and (
            tokens_per_valid_delta is None or float(tokens_per_valid_delta) > self.max_tokens_per_valid_delta
        ):
            return False
        return True


__all__ = ["LLMValueGate", "run_llm_ablation"]
