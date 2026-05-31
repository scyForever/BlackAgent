"""Promote wide-recall candidate clues into actionable review clues."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from src.rules import RuleRegistry


class CluePromotionStage:
    """Split clue output into candidate/actionable/archived layers.

    Aggregators remain recall-oriented.  This stage is the precision gate that
    decides which candidates should count toward review load.
    """

    def __init__(self, *, strict: bool = True, rule_registry: RuleRegistry | None = None) -> None:
        self.strict = strict
        self.rule_registry = rule_registry or RuleRegistry()
        self.rules = self.rule_registry.load_clue_generation_rules()
        self.candidate_clues: list[dict[str, Any]] = []
        self.actionable_clues: list[dict[str, Any]] = []
        self.archived_weak_clues: list[dict[str, Any]] = []

    def run_batch(self, items: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        context = dict(kwargs.get("context") or {})
        entities = [dict(item) for item in context.get("entities") or [] if isinstance(item, Mapping)]
        entity_types_by_trace: dict[str, set[str]] = defaultdict(set)
        for entity in entities:
            trace_id = str(entity.get("source_trace_id") or entity.get("trace_id") or "")
            entity_type = str(entity.get("entity_type") or "").lower()
            if trace_id and entity_type:
                entity_types_by_trace[trace_id].add(entity_type)

        self.candidate_clues = []
        self.actionable_clues = []
        self.archived_weak_clues = []
        promoted_by_type: dict[str, dict[str, Any]] = {}
        overflow_promoted: list[dict[str, Any]] = []
        for raw in items:
            candidate = dict(raw)
            candidate["clue_stage"] = "candidate"
            candidate.setdefault("weak_reason", candidate.get("threshold_reason") or "candidate_from_aggregator")
            self.candidate_clues.append(candidate)
            promoted, reason, score = self._promote(candidate, entity_types_by_trace=entity_types_by_trace)
            if promoted:
                actionable = dict(candidate)
                actionable["clue_stage"] = "actionable"
                actionable["promotion_reason"] = reason
                actionable["actionability_score"] = score
                clue_type = str(actionable.get("clue_type") or "unknown")
                existing = promoted_by_type.get(clue_type)
                if existing is None or _actionable_rank(actionable) > _actionable_rank(existing):
                    if existing is not None:
                        overflow_promoted.append(existing)
                    promoted_by_type[clue_type] = actionable
                else:
                    overflow_promoted.append(actionable)
            else:
                archived = dict(candidate)
                archived["clue_stage"] = "archived_weak"
                archived["archive_reason"] = reason
                archived["actionability_score"] = score
                self.archived_weak_clues.append(archived)
        self.actionable_clues = list(promoted_by_type.values())
        for clue in overflow_promoted:
            archived = dict(clue)
            archived["clue_stage"] = "archived_weak"
            archived["archive_reason"] = "duplicate_actionable_type_review_load_capped"
            self.archived_weak_clues.append(archived)
        return [dict(item) for item in self.actionable_clues]

    def _promote(self, clue: Mapping[str, Any], *, entity_types_by_trace: Mapping[str, set[str]]) -> tuple[bool, str, float]:
        clue_type = str(clue.get("clue_type") or "").lower()
        traces = {str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()}
        sources = {str(item) for item in (clue.get("source_names") or []) if str(item).strip()}
        entity_values = {str(item) for item in (clue.get("entity_values") or []) if str(item).strip()}
        observed_types = {etype for trace in traces for etype in entity_types_by_trace.get(trace, set())}

        score = min(0.99, 0.2 + min(len(traces), 4) * 0.12 + min(len(sources), 3) * 0.12 + min(len(entity_values), 3) * 0.06)
        if observed_types.intersection({"contact", "account", "url", "domain", "tool_name", "price", "settlement"}):
            score = min(0.99, score + 0.18)

        if any(token in clue_type for token in ("contact", "account", "invite")):
            configured = _rule_promotion(self.rules, "shared_contact_48h")
            min_sources = int(configured.get("require_min_sources") or 2)
            min_observations = int(configured.get("require_min_observations") or 3)
            if len(sources) >= min_sources or len(traces) >= min_observations:
                return True, "contact_account_cross_source_or_three_observations", round(score, 4)
            return False, "contact_account_requires_two_sources_or_three_observations", round(score, 4)

        if any(token in clue_type for token in ("domain", "url")):
            configured = _rule_promotion(self.rules, "shared_domain_multi_source")
            required_entities = set(_string_list(configured.get("require_any_entity"))) or {"contact", "account", "tool_name"}
            if len(sources) >= int(configured.get("require_min_sources") or 2) and observed_types.intersection(required_entities):
                return True, "domain_url_cross_source_with_contact_or_tool", round(score, 4)
            return False, "domain_url_requires_two_sources_and_contact_or_tool", round(score, 4)

        if any(token in clue_type for token in ("tool", "slang")):
            configured = _rule_promotion(self.rules, "tool_slang")
            required_entities = set(_string_list(configured.get("require_any_entity"))) or {"contact", "account", "price", "url", "domain"}
            if observed_types.intersection(required_entities) or len(sources) >= int(configured.get("require_min_sources") or 2):
                return True, "tool_slang_has_transaction_or_cross_source_support", round(score, 4)
            return False, "tool_slang_requires_contact_price_url_or_cross_source_support", round(score, 4)

        if "template" in clue_type:
            configured = _rule_promotion(self.rules, "high_frequency_template")
            rejected = {item.lower() for item in _string_list(configured.get("reject_risk_categories"))} or {"normal_noise", "正常业务白噪声"}
            if len(traces) >= int(configured.get("require_min_observations") or configured.get("min_records") or 3) and str(clue.get("risk_category") or "").lower() not in rejected:
                return True, "template_repeated_three_times_non_defensive", round(score, 4)
            return False, "template_requires_three_non_defensive_repetitions", round(score, 4)

        if not self.strict and len(traces) >= 2:
            return True, "non_strict_two_evidence_samples", round(score, 4)
        return False, "no_promotion_rule_matched", round(score, 4)


def _actionable_rank(clue: Mapping[str, Any]) -> tuple[float, int, int, str]:
    return (
        float(clue.get("actionability_score") or clue.get("quality_score") or clue.get("confidence") or 0.0),
        len({str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()}),
        len({str(item) for item in (clue.get("source_names") or []) if str(item).strip()}),
        str(clue.get("key") or ""),
    )


def _rule_promotion(rules: Mapping[str, Any], name: str) -> dict[str, Any]:
    spec = rules.get(name) if isinstance(rules, Mapping) else {}
    if not isinstance(spec, Mapping):
        return {}
    promotion = spec.get("promotion")
    if isinstance(promotion, Mapping):
        return {**dict(spec), **dict(promotion)}
    return dict(spec)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [str(value)]
    try:
        return [str(item) for item in value if str(item).strip()]
    except TypeError:
        return [str(value)]


__all__ = ["CluePromotionStage"]
