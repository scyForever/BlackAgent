"""Reusable LLM ablation helpers for value/cost reporting."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RecordEnrichPolicy = str


def run_llm_ablation(records: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Run the standard fast/off vs high_recall/off/mock comparison."""

    from scripts.evaluate_pipeline import evaluate_ablation

    return evaluate_ablation(records, **kwargs)


class LLMValueGate:
    """Offline value gate for deciding whether record enrichment is worthwhile."""

    def __init__(self, *, min_f1_gain: float = 0.01, max_tokens_per_valid_delta: float = 10_000.0) -> None:
        self.min_f1_gain = min_f1_gain
        self.max_tokens_per_valid_delta = max_tokens_per_valid_delta

    def record_enrich_policy(self, profile: str, recent_metrics: dict[str, Any] | None) -> RecordEnrichPolicy:
        normalized_profile = str(profile or "").strip().lower()
        if normalized_profile == "fast":
            return "disabled"
        if not recent_metrics:
            return "hard_cases_only"
        explicit_policy = str(recent_metrics.get("record_enrich_policy") or "").strip().lower()
        if explicit_policy in {"enabled", "hard_cases_only", "conflict_only", "disabled"}:
            return explicit_policy
        explicit = recent_metrics.get("should_enable_record_enrich")
        if explicit is False:
            return "conflict_only"
        llm_gain = max(
            float((recent_metrics or {}).get("classification_f1_delta") or 0.0),
            float((recent_metrics or {}).get("entity_f1_delta") or 0.0),
            float((recent_metrics or {}).get("clue_recall_delta") or 0.0),
        )
        tokens_per_valid_delta = (recent_metrics or {}).get("tokens_per_extra_valid_clue")
        if llm_gain < self.min_f1_gain and (
            tokens_per_valid_delta is None or float(tokens_per_valid_delta) > self.max_tokens_per_valid_delta
        ):
            return "conflict_only"
        return "enabled"

    def should_enable_record_enrich(self, profile: str, recent_metrics: dict[str, Any]) -> bool:
        return self.record_enrich_policy(profile, recent_metrics) == "enabled"


def llm_value_report_from_ablation(
    ablation_report: dict[str, Any],
    *,
    profile: str = "high_recall",
) -> dict[str, Any]:
    """Normalize an ablation report into the runtime-facing value-gate shape."""

    value = dict(ablation_report.get("llm_value") or ablation_report)
    gate = dict(ablation_report.get("llm_value_gate") or {})
    should_enable = gate.get("should_enable_record_enrich")
    if should_enable is None:
        should_enable = LLMValueGate().should_enable_record_enrich(profile, value)
    record_policy = LLMValueGate().record_enrich_policy(profile, {**value, "should_enable_record_enrich": should_enable})
    report = {
        "profile": str(profile or "high_recall"),
        "classification_f1_delta": float(value.get("classification_f1_delta") or 0.0),
        "entity_f1_delta": float(value.get("entity_f1_delta") or 0.0),
        "hard_negative_fpr_delta": float(value.get("hard_negative_fpr_delta") or 0.0),
        "clue_precision_delta": float(value.get("clue_precision_delta") or 0.0),
        "clue_recall_delta": float(value.get("clue_recall_delta") or 0.0),
        "llm_calls_delta": float(value.get("llm_calls_delta") or 0.0),
        "p95_latency_ms_delta": float(value.get("p95_latency_ms_delta") or 0.0),
        "tokens_per_f1_gain": value.get("tokens_per_f1_gain"),
        "tokens_per_extra_valid_clue": value.get("tokens_per_extra_valid_clue"),
        "latency_ms_per_f1_gain": value.get("latency_ms_per_f1_gain"),
        "latency_ms_per_extra_valid_clue": value.get("latency_ms_per_extra_valid_clue"),
        "gate_reason": str(value.get("gate_reason") or gate.get("reason") or "llm_value_gate_report"),
        "should_enable_record_enrich": bool(should_enable),
        "record_enrich_policy": record_policy,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    provider_specific = {
        key: value[key]
        for key in ("balanced_mock", "balanced_real_or_fallback", "balanced_real", "real_or_fallback", "real")
        if key in value
    }
    if provider_specific:
        report["provider_specific"] = provider_specific
    return report


def write_latest_llm_value_report(
    ablation_report: dict[str, Any],
    *,
    output_path: str | Path = "data/eval/latest_llm_value.json",
    profile: str = "high_recall",
) -> dict[str, Any]:
    """Persist the latest value gate report for runtime ModelRouter loading."""

    report = llm_value_report_from_ablation(ablation_report, profile=profile)
    target = Path(output_path)
    if not target.is_absolute():
        target = Path.cwd() / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def load_latest_llm_value_report(path: str | Path = "data/eval/latest_llm_value.json") -> dict[str, Any] | None:
    """Load runtime LLM value metrics if available and valid."""

    target = Path(path)
    if not target.is_absolute():
        candidates = [Path.cwd() / target, Path(__file__).resolve().parents[2] / target]
        target = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    if not target.exists():
        return None
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "LLMValueGate",
    "llm_value_report_from_ablation",
    "load_latest_llm_value_report",
    "run_llm_ablation",
    "write_latest_llm_value_report",
]
