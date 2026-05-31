"""Model routing decisions for cost/latency-aware LLM usage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping


RouteAction = Literal["skip", "deterministic_only", "llm_classify_extract", "llm_refine_only"]


@dataclass(frozen=True)
class ModelRouteDecision:
    action: RouteAction
    reason: str
    priority: int
    max_tokens: int
    deadline_ms: int
    requires_review: bool

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class ModelRouter:
    """Deterministically decide whether a record or clue deserves LLM budget."""

    def __init__(
        self,
        profile: str = "balanced",
        *,
        record_enrich_enabled: bool = True,
        value_gate_reason: str | None = None,
    ) -> None:
        self.profile = _normalize_profile(profile)
        self.record_enrich_enabled = bool(record_enrich_enabled)
        self.value_gate_reason = value_gate_reason or ""

    def with_profile(self, profile: str | None) -> "ModelRouter":
        return type(self)(
            _normalize_profile(profile or self.profile),
            record_enrich_enabled=self.record_enrich_enabled,
            value_gate_reason=self.value_gate_reason,
        )

    def with_llm_value_metrics(
        self,
        recent_metrics: Mapping[str, Any] | None,
        *,
        profile: str | None = None,
    ) -> "ModelRouter":
        """Return a router tightened by recent offline LLM value evidence.

        If the value gate says record enrichment is not paying for itself, the
        router keeps deterministic processing for normal ambiguous records and
        reserves LLM classification/extraction for true conflict cases.
        """

        from src.evaluation.llm_ablation import LLMValueGate

        metrics = dict(recent_metrics or {})
        normalized_profile = _normalize_profile(profile or self.profile)
        enabled = LLMValueGate().should_enable_record_enrich(normalized_profile, metrics)
        reason = str(
            metrics.get("gate_reason")
            or ("llm_value_gate_enabled_record_enrich" if enabled else "llm_value_gate_disabled_record_enrich")
        )
        return type(self)(
            normalized_profile,
            record_enrich_enabled=enabled,
            value_gate_reason=reason,
        )

    def decide_record(
        self,
        *,
        rule_confidence: float,
        risk_score: float,
        entity_count: int,
        has_contact: bool,
        has_url: bool,
        has_tool: bool,
        has_conflict: bool,
        is_duplicate: bool,
        quality_score: float,
    ) -> ModelRouteDecision:
        """Route one processed sample before expensive model enrichment."""

        if quality_score < 0.25 and not (has_contact or has_url or has_tool):
            return ModelRouteDecision("skip", "low_quality_low_signal", 0, 0, 0, False)
        if is_duplicate and rule_confidence >= 0.80:
            return ModelRouteDecision("deterministic_only", "duplicate_high_rule_confidence", 1, 0, 0, False)
        if rule_confidence >= 0.85 and entity_count >= 2 and not has_conflict:
            return ModelRouteDecision("deterministic_only", "high_confidence_rule_and_entities", 2, 0, 0, False)
        if not self.record_enrich_enabled and not has_conflict:
            return ModelRouteDecision(
                "deterministic_only",
                self.value_gate_reason or "llm_value_gate_disabled_record_enrich",
                1,
                0,
                0,
                rule_confidence < 0.70 or risk_score >= 0.75,
            )
        if has_conflict or ((has_contact or has_url or has_tool) and rule_confidence >= 0.45):
            return ModelRouteDecision(
                "llm_classify_extract",
                "conflict_hard_case_despite_value_gate"
                if has_conflict and not self.record_enrich_enabled
                else "ambiguous_high_value_signal",
                5,
                600 if self.profile == "fast" else 900,
                2500 if self.profile == "fast" else 6000,
                True,
            )
        return ModelRouteDecision(
            "deterministic_only",
            "low_ambiguity_default",
            1,
            0,
            0,
            rule_confidence < 0.70 or risk_score >= 0.75,
        )

    def decide_clue_refinement(self, clue: Mapping[str, Any]) -> ModelRouteDecision:
        """Route one candidate clue before LLM summary/refinement."""

        quality_score = _float(clue.get("quality_score"))
        confidence = _float(clue.get("confidence"))
        evidence_count = len({str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()})
        cross_source_count = len({str(item) for item in (clue.get("source_names") or []) if str(item).strip()})
        entity_count = len({str(item) for item in (clue.get("entity_values") or []) if str(item).strip()})
        has_refinement = isinstance(clue.get("refinement"), Mapping)
        quality = clue.get("quality") if isinstance(clue.get("quality"), Mapping) else {}
        review_required = bool(quality.get("review_required")) or clue.get("quality_level") != "high"

        if quality_score < 0.35 and entity_count == 0:
            return ModelRouteDecision("skip", "low_quality_without_entities", 0, 0, 0, False)
        if has_refinement and quality_score >= 0.82 and evidence_count >= 2:
            return ModelRouteDecision("deterministic_only", "already_refined_high_quality", 1, 0, 0, False)
        if evidence_count >= 2 or cross_source_count >= 2 or review_required:
            return ModelRouteDecision(
                "llm_refine_only",
                "reviewable_high_value_clue",
                5 if review_required else 4,
                300 if self.profile == "fast" else 450,
                2500 if self.profile == "fast" else 5000,
                review_required or confidence < 0.78,
            )
        return ModelRouteDecision(
            "deterministic_only",
            "single_evidence_default",
            1,
            0,
            0,
            confidence < 0.70,
        )


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_profile(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {"fast", "latency", "low_latency"}:
        return "fast"
    if text in {"high_recall", "recall", "quality"}:
        return "high_recall"
    return "balanced"


__all__ = ["ModelRouteDecision", "ModelRouter", "RouteAction"]
