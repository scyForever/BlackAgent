"""Model routing decisions for cost/latency-aware LLM usage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

from src.rules import RuleRegistry


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
        record_enrich_policy: str | None = None,
        value_gate_reason: str | None = None,
        rule_registry: RuleRegistry | None = None,
        routing_rules: Mapping[str, Any] | None = None,
    ) -> None:
        self.profile = _normalize_profile(profile)
        self.record_enrich_enabled = bool(record_enrich_enabled)
        self.record_enrich_policy = _normalize_record_enrich_policy(
            record_enrich_policy or ("enabled" if self.record_enrich_enabled else "hard_cases_only")
        )
        self.value_gate_reason = value_gate_reason or ""
        self.rule_registry = rule_registry or RuleRegistry()
        configured = self.rule_registry.load_model_stage_policy()
        self.routing_rules = dict(routing_rules or configured)
        self.record_rules = _section(self.routing_rules, "record_routing")
        self.clue_rules = _section(self.routing_rules, "clue_routing")

    def with_profile(self, profile: str | None) -> "ModelRouter":
        return type(self)(
            _normalize_profile(profile or self.profile),
            record_enrich_enabled=self.record_enrich_enabled,
            record_enrich_policy=self.record_enrich_policy,
            value_gate_reason=self.value_gate_reason,
            rule_registry=self.rule_registry,
            routing_rules=self.routing_rules,
        )

    def with_record_enrich_policy(
        self,
        *,
        enabled: bool,
        reason: str,
        profile: str | None = None,
        policy: str | None = None,
    ) -> "ModelRouter":
        normalized_policy = _normalize_record_enrich_policy(policy or ("enabled" if enabled else "hard_cases_only"))
        return type(self)(
            _normalize_profile(profile or self.profile),
            record_enrich_enabled=normalized_policy == "enabled",
            record_enrich_policy=normalized_policy,
            value_gate_reason=reason,
            rule_registry=self.rule_registry,
            routing_rules=self.routing_rules,
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
        record_policy = LLMValueGate().record_enrich_policy(normalized_profile, metrics)
        reason = str(
            metrics.get("gate_reason")
            or (
                "llm_value_gate_enabled_record_enrich"
                if record_policy == "enabled"
                else f"llm_value_gate_{record_policy}_record_enrich"
            )
        )
        return type(self)(
            normalized_profile,
            record_enrich_enabled=record_policy == "enabled",
            record_enrich_policy=record_policy,
            value_gate_reason=reason,
            rule_registry=self.rule_registry,
            routing_rules=self.routing_rules,
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

        if quality_score < _float_rule(self.record_rules, "low_quality_min_score", 0.25) and not (has_contact or has_url or has_tool):
            return ModelRouteDecision("skip", "low_quality_low_signal", 0, 0, 0, False)
        if is_duplicate and rule_confidence >= _float_rule(self.record_rules, "duplicate_auto_accept_confidence", 0.80):
            return ModelRouteDecision("deterministic_only", "duplicate_high_rule_confidence", 1, 0, 0, False)
        if (
            rule_confidence >= _float_rule(self.record_rules, "deterministic_auto_accept_confidence", 0.85)
            and entity_count >= _int_rule(self.record_rules, "deterministic_auto_accept_min_entities", 2)
            and not has_conflict
        ):
            return ModelRouteDecision("deterministic_only", "high_confidence_rule_and_entities", 2, 0, 0, False)
        policy = self.record_enrich_policy
        hard_case_allowed = self._hard_case_allowed_by_policy(
            policy,
            rule_confidence=rule_confidence,
            risk_score=risk_score,
            has_contact=has_contact,
            has_url=has_url,
            has_tool=has_tool,
            has_conflict=has_conflict,
        )
        if policy == "disabled":
            return ModelRouteDecision(
                "deterministic_only",
                self.value_gate_reason or "record_enrich_disabled_by_policy",
                1,
                0,
                0,
                rule_confidence < _float_rule(self.record_rules, "value_gate_review_confidence", 0.70)
                or risk_score >= _float_rule(self.record_rules, "value_gate_review_risk_score", 0.75),
            )
        if policy in {"hard_cases_only", "conflict_only"} and not hard_case_allowed:
            return ModelRouteDecision(
                "deterministic_only",
                self.value_gate_reason or "llm_value_gate_disabled_record_enrich",
                1,
                0,
                0,
                rule_confidence < _float_rule(self.record_rules, "value_gate_review_confidence", 0.70)
                or risk_score >= _float_rule(self.record_rules, "value_gate_review_risk_score", 0.75),
            )
        if has_conflict or (
            (has_contact or has_url or has_tool)
            and rule_confidence >= _float_rule(self.record_rules, "llm_min_rule_confidence_with_signal", 0.45)
        ):
            return ModelRouteDecision(
                "llm_classify_extract",
                f"{policy}_hard_case_record_enrich"
                if policy in {"hard_cases_only", "conflict_only"}
                else "ambiguous_high_value_signal",
                5,
                _profile_int_rule(self.record_rules, self.profile, "record_max_tokens", 600 if self.profile == "fast" else 900),
                _profile_int_rule(self.record_rules, self.profile, "record_deadline_ms", 2500 if self.profile == "fast" else 6000),
                True,
            )
        return ModelRouteDecision(
            "deterministic_only",
            "low_ambiguity_default",
            1,
            0,
            0,
            rule_confidence < _float_rule(self.record_rules, "value_gate_review_confidence", 0.70)
            or risk_score >= _float_rule(self.record_rules, "value_gate_review_risk_score", 0.75),
        )

    def _hard_case_allowed_by_policy(
        self,
        policy: str,
        *,
        rule_confidence: float,
        risk_score: float,
        has_contact: bool,
        has_url: bool,
        has_tool: bool,
        has_conflict: bool,
    ) -> bool:
        if policy == "enabled":
            return True
        if policy == "disabled":
            return False
        if policy == "conflict_only":
            return bool(has_conflict)
        if policy == "hard_cases_only":
            if has_conflict:
                return True
            high_value_signal = has_contact or has_url or has_tool
            return bool(
                high_value_signal
                and rule_confidence <= _float_rule(self.record_rules, "hard_case_max_rule_confidence", 0.68)
                and risk_score >= _float_rule(self.record_rules, "hard_case_min_risk_score", 0.65)
            )
        return False

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

        if quality_score < _float_rule(self.clue_rules, "low_quality_without_entities_score", 0.35) and entity_count == 0:
            return ModelRouteDecision("skip", "low_quality_without_entities", 0, 0, 0, False)
        if has_refinement and quality_score >= _float_rule(self.clue_rules, "already_refined_quality_score", 0.82) and evidence_count >= 2:
            return ModelRouteDecision("deterministic_only", "already_refined_high_quality", 1, 0, 0, False)
        if evidence_count >= 2 or cross_source_count >= 2 or review_required:
            return ModelRouteDecision(
                "llm_refine_only",
                "reviewable_high_value_clue",
                5 if review_required else 4,
                _profile_int_rule(self.clue_rules, self.profile, "refine_max_tokens", 300 if self.profile == "fast" else 450),
                _profile_int_rule(self.clue_rules, self.profile, "refine_deadline_ms", 2500 if self.profile == "fast" else 5000),
                review_required or confidence < _float_rule(self.clue_rules, "review_confidence_threshold", 0.78),
            )
        return ModelRouteDecision(
            "deterministic_only",
            "single_evidence_default",
            1,
            0,
            0,
            confidence < _float_rule(self.clue_rules, "default_review_confidence_threshold", 0.70),
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


def _normalize_record_enrich_policy(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {"disabled", "off", "false", "none"}:
        return "disabled"
    if text in {"conflict_only", "conflicts_only"}:
        return "conflict_only"
    if text in {"hard_cases_only", "hard_case_only", "selective"}:
        return "hard_cases_only"
    return "enabled"


def _section(rules: Mapping[str, Any], name: str) -> dict[str, Any]:
    section = rules.get(name) if isinstance(rules, Mapping) else {}
    return dict(section) if isinstance(section, Mapping) else {}


def _float_rule(rules: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(rules.get(key, default))
    except (TypeError, ValueError):
        return default


def _int_rule(rules: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(rules.get(key, default))
    except (TypeError, ValueError):
        return default


def _profile_int_rule(rules: Mapping[str, Any], profile: str, suffix: str, default: int) -> int:
    profile_key = "fast" if profile == "fast" else "default"
    return _int_rule(rules, f"{profile_key}_{suffix}", default)


__all__ = ["ModelRouteDecision", "ModelRouter", "RouteAction"]
