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
        records_by_trace = {
            str(item.get("trace_id") or item.get("source_trace_id") or ""): dict(item)
            for item in context.get("records") or []
            if isinstance(item, Mapping)
        }
        entity_types_by_trace: dict[str, set[str]] = defaultdict(set)
        for entity in entities:
            trace_id = str(entity.get("source_trace_id") or entity.get("trace_id") or "")
            entity_type = str(entity.get("entity_type") or "").lower()
            if trace_id and entity_type:
                entity_types_by_trace[trace_id].add(entity_type)

        self.candidate_clues = []
        self.actionable_clues = []
        self.archived_weak_clues = []
        promoted_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
        overflow_promoted: list[dict[str, Any]] = []
        for raw in items:
            candidate = dict(raw)
            candidate["clue_stage"] = "candidate"
            candidate.setdefault("weak_reason", candidate.get("threshold_reason") or "candidate_from_aggregator")
            self.candidate_clues.append(candidate)
            promoted, reason, score = self._promote(candidate, entity_types_by_trace=entity_types_by_trace, records_by_trace=records_by_trace)
            if promoted:
                actionable = dict(candidate)
                actionable["clue_stage"] = "actionable"
                actionable["promotion_reason"] = reason
                actionable["actionability_score"] = score
                identity = _clue_identity(actionable)
                existing = promoted_by_identity.get(identity)
                if existing is None or _actionable_rank(actionable) > _actionable_rank(existing):
                    if existing is not None:
                        overflow_promoted.append(existing)
                    promoted_by_identity[identity] = actionable
                else:
                    overflow_promoted.append(actionable)
            else:
                archived = dict(candidate)
                archived["clue_stage"] = "archived_weak"
                archived["archive_reason"] = reason
                archived["actionability_score"] = score
                self.archived_weak_clues.append(archived)
        self.actionable_clues, superseded = _archive_superseded_generic_clues(promoted_by_identity.values())
        self.archived_weak_clues.extend(superseded)
        for clue in overflow_promoted:
            archived = dict(clue)
            archived["clue_stage"] = "archived_weak"
            archived["archive_reason"] = "duplicate_actionable_type_review_load_capped"
            self.archived_weak_clues.append(archived)
        return [dict(item) for item in self.actionable_clues]

    def _promote(
        self,
        clue: Mapping[str, Any],
        *,
        entity_types_by_trace: Mapping[str, set[str]],
        records_by_trace: Mapping[str, Mapping[str, Any]],
    ) -> tuple[bool, str, float]:
        clue_type = str(clue.get("clue_type") or "").lower()
        traces = {str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()}
        sources = {str(item) for item in (clue.get("source_names") or []) if str(item).strip()}
        entity_values = {str(item) for item in (clue.get("entity_values") or []) if str(item).strip()}
        observed_types = {etype for trace in traces for etype in entity_types_by_trace.get(trace, set())}

        score = min(0.99, 0.2 + min(len(traces), 4) * 0.12 + min(len(sources), 3) * 0.12 + min(len(entity_values), 3) * 0.06)
        if observed_types.intersection({"contact", "account", "url", "domain", "tool_name", "price", "settlement"}):
            score = min(0.99, score + 0.18)

        if clue_type == "shared_tool_multi_source" and _is_generic_shared_tool_name(str(clue.get("key") or ""), entity_values):
            return False, "generic_shared_tool_name_requires_specific_identifier", round(score, 4)
        if (
            clue_type == "shared_settlement_multi_source"
            and _is_generic_settlement_value(str(clue.get("key") or ""), entity_values)
            and _normalized_key(clue.get("key")) != "usdt"
        ):
            return False, "generic_settlement_requires_contextual_identifier", round(score, 4)
        if clue_type == "entity_graph_tool_trade_cluster" and _is_generic_shared_tool_name(str(clue.get("key") or ""), entity_values):
            return False, "generic_tool_trade_cluster_requires_specific_identifier", round(score, 4)
        if (
            clue_type == "entity_graph_tool_trade_cluster"
            and _is_direct_contact_key(clue.get("key"))
            and not _has_explicit_tool_trade_text(clue, records_by_trace)
        ):
            return False, "direct_contact_tool_cluster_requires_explicit_tool_trade_text", round(score, 4)
        if (
            clue_type == "entity_graph_tool_trade_cluster"
            and _is_direct_contact_key(clue.get("key"))
            and not _has_specific_tool_cluster_support(entity_values)
        ):
            return False, "direct_contact_tool_cluster_requires_specific_tool_support", round(score, 4)

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


def _archive_superseded_generic_clues(actionable: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    clues = [dict(item) for item in actionable]
    specific_graph_chains = {
        (_normalized_key(clue.get("key")), tuple(sorted(_trace_set(clue))))
        for clue in clues
        if _is_specific_graph_clue(clue)
    }
    domain_chains = {
        tuple(sorted(_trace_set(clue)))
        for clue in clues
        if str(clue.get("clue_type") or "").strip().lower() == "shared_domain_multi_source"
    }
    tool_cluster_chains = {
        (_normalized_key(clue.get("key")), tuple(sorted(_trace_set(clue))))
        for clue in clues
        if str(clue.get("clue_type") or "").strip().lower() == "entity_graph_tool_trade_cluster"
    }
    kept: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    for clue in clues:
        identity = (_normalized_key(clue.get("key")), tuple(sorted(_trace_set(clue))))
        if _is_contextual_bridge_contact(clue) and tuple(sorted(_trace_set(clue))) in domain_chains:
            downgraded = dict(clue)
            downgraded["clue_stage"] = "archived_weak"
            downgraded["archive_reason"] = "superseded_by_same_chain_domain_clue"
            archived.append(downgraded)
            continue
        if _is_contextual_bridge_contact(clue) and identity in tool_cluster_chains:
            downgraded = dict(clue)
            downgraded["clue_stage"] = "archived_weak"
            downgraded["archive_reason"] = "superseded_by_same_chain_tool_cluster"
            archived.append(downgraded)
            continue
        if _is_generic_shared_clue(clue) and not _is_direct_contact_key(clue.get("key")) and identity in specific_graph_chains:
            downgraded = dict(clue)
            downgraded["clue_stage"] = "archived_weak"
            downgraded["archive_reason"] = "superseded_by_more_specific_graph_clue"
            archived.append(downgraded)
            continue
        kept.append(clue)
    return kept, archived


def _actionable_rank(clue: Mapping[str, Any]) -> tuple[float, int, int, str]:
    return (
        float(clue.get("actionability_score") or clue.get("quality_score") or clue.get("confidence") or 0.0),
        len({str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()}),
        len({str(item) for item in (clue.get("source_names") or []) if str(item).strip()}),
        str(clue.get("key") or ""),
    )


def _clue_identity(clue: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(clue.get("clue_type") or "unknown").strip().lower(),
        str(clue.get("key") or "").strip().lower(),
        str(clue.get("risk_category") or "").strip().lower(),
    )


def _is_generic_shared_clue(clue: Mapping[str, Any]) -> bool:
    clue_type = str(clue.get("clue_type") or "").strip().lower()
    return clue_type in {"shared_contact_48h", "shared_tool_multi_source"}


def _is_direct_contact_key(value: Any) -> bool:
    lowered = _normalized_key(value)
    return lowered.startswith(("telegram:", "wechat:", "qq:", "tg:", "wx:")) or lowered.startswith("@")


def _is_contextual_bridge_contact(clue: Mapping[str, Any]) -> bool:
    return (
        str(clue.get("clue_type") or "").strip().lower() == "shared_contact_48h"
        and str(clue.get("weak_reason") or clue.get("threshold_reason") or "") == "single_identifier_with_authorized_cross_source_corroboration"
    )


def _is_specific_graph_clue(clue: Mapping[str, Any]) -> bool:
    clue_type = str(clue.get("clue_type") or "").strip().lower()
    return clue_type in {"entity_graph_account_tool_overlap", "entity_graph_tool_trade_cluster"}


def _normalized_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _trace_set(clue: Mapping[str, Any]) -> set[str]:
    return {str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()}


def _is_generic_shared_tool_name(key: str, entity_values: set[str]) -> bool:
    values = {_normalized_key(key), *{_normalized_key(item) for item in entity_values}}
    generic_values = {
        "脚本",
        "群控",
        "云控",
        "群发器",
        "卡密",
        "接码",
        "批量登录",
        "工具",
        "bot",
        "userbot",
    }
    return bool(values & generic_values) and not any(_looks_like_specific_identifier(value) for value in values)


def _is_generic_settlement_value(key: str, entity_values: set[str]) -> bool:
    values = {_normalized_key(key), *{_normalized_key(item) for item in entity_values}}
    return bool(values & {"usdt", "跑分", "结算", "担保", "代付"})


def _has_explicit_tool_trade_text(clue: Mapping[str, Any], records_by_trace: Mapping[str, Mapping[str, Any]]) -> bool:
    texts = [
        str((records_by_trace.get(trace) or {}).get("content_text") or (records_by_trace.get(trace) or {}).get("clean_text") or "")
        for trace in _trace_set(clue)
    ]
    combined = " ".join(texts)
    return any(marker in combined for marker in ("售卖", "出售", "售后", "批发", "卡密", "下单", "报价", "月卡", "订单"))


def _has_specific_tool_cluster_support(entity_values: set[str]) -> bool:
    values = {_normalized_key(item) for item in entity_values}
    tool_values = {item for item in values if not _is_direct_contact_key(item)}
    if any(_looks_like_specific_identifier(item) for item in tool_values):
        return True
    return len(tool_values) >= 2


def _looks_like_specific_identifier(value: str) -> bool:
    if not value:
        return False
    if any(char.isdigit() for char in value):
        return True
    return any(separator in value for separator in ("-", "_", ".", ":"))


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
            return False, str(rule.get("reject_reason") or rule.get("fail_reason") or f"{rule_name}_rejected_risk_category"), round(score, 4)
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
