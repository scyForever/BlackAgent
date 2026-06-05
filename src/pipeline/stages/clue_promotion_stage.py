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

        configured_result = _configured_promotion_decision(
            self.rules,
            clue,
            clue_type=clue_type,
            traces=traces,
            sources=sources,
            observed_types=observed_types,
            score=score,
        )
        if configured_result is not None:
            return configured_result

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


def _configured_promotion_decision(
    rules: Mapping[str, Any],
    clue: Mapping[str, Any],
    *,
    clue_type: str,
    traces: set[str],
    sources: set[str],
    observed_types: set[str],
    score: float,
) -> tuple[bool, str, float] | None:
    rule_map = rules.get("clue_promotion") if isinstance(rules, Mapping) else None
    if not isinstance(rule_map, Mapping):
        return None
    for rule_name, raw_rule in rule_map.items():
        if not isinstance(raw_rule, Mapping):
            continue
        rule = dict(raw_rule)
        exact_types = {item.lower() for item in _string_list(rule.get("match_clue_types"))}
        contains = [item.lower() for item in _string_list(rule.get("match_clue_type_contains"))]
        if exact_types and clue_type not in exact_types:
            continue
        if contains and not any(token in clue_type for token in contains):
            continue
        rejected = {item.lower() for item in _string_list(rule.get("reject_risk_categories"))}
        if rejected and str(clue.get("risk_category") or "").lower() in rejected:
            return False, str(rule.get("fail_reason") or f"{rule_name}_rejected_risk_category"), round(score, 4)
        passed = _requirements_pass(rule, traces=traces, sources=sources, observed_types=observed_types)
        return (
            passed,
            str(rule.get("pass_reason") if passed else rule.get("fail_reason") or rule_name),
            round(score, 4),
        )
    return None


def _requirements_pass(rule: Mapping[str, Any], *, traces: set[str], sources: set[str], observed_types: set[str]) -> bool:
    require_all = rule.get("require_all")
    require_any = rule.get("require_any")
    if isinstance(require_all, list) and require_all:
        if not all(_requirement_pass(item, traces=traces, sources=sources, observed_types=observed_types) for item in require_all if isinstance(item, Mapping)):
            return False
    if isinstance(require_any, list) and require_any:
        if not any(_requirement_pass(item, traces=traces, sources=sources, observed_types=observed_types) for item in require_any if isinstance(item, Mapping)):
            return False
    if "require_min_sources" in rule and len(sources) < int(rule.get("require_min_sources") or 0):
        return False
    if "require_min_observations" in rule and len(traces) < int(rule.get("require_min_observations") or 0):
        return False
    if "require_any_entity" in rule and not observed_types.intersection({item.lower() for item in _string_list(rule.get("require_any_entity"))}):
        return False
    return True


def _requirement_pass(requirement: Mapping[str, Any], *, traces: set[str], sources: set[str], observed_types: set[str]) -> bool:
    if "min_sources" in requirement and len(sources) < int(requirement.get("min_sources") or 0):
        return False
    if "min_observations" in requirement and len(traces) < int(requirement.get("min_observations") or 0):
        return False
    if "any_entity_type" in requirement and not observed_types.intersection({item.lower() for item in _string_list(requirement.get("any_entity_type"))}):
        return False
    return True


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
