"""Auditable rule/LLM classification resolution policy."""

from __future__ import annotations

from typing import Any, Mapping

from src.domain import ClassificationResolution


def resolve_classification(
    rule_result: Mapping[str, Any] | None,
    llm_result: Mapping[str, Any] | None,
    *,
    trace_id: str = "unknown",
    policy: Mapping[str, Any] | None = None,
) -> ClassificationResolution:
    """Resolve deterministic and LLM classifications without hidden overwrite.

    The policy is deliberately conservative for high-risk intelligence:
    LLM output must include evidence before it can change a rule result; high
    confidence rule results with evidence stay authoritative; high-confidence
    conflicts become review-required instead of silent LLM replacement.
    """

    rule = dict(rule_result or {})
    llm = dict(llm_result or {})
    policy = dict(policy or {})
    min_llm_evidence = int(policy.get("min_llm_evidence") or 1)
    rule_confidence = _float(rule.get("confidence"), 0.0)
    llm_confidence = _float(llm.get("confidence"), 0.0)
    rule_category = str(rule.get("risk_category") or "unknown")
    llm_category = str(llm.get("risk_category") or "").strip()
    llm_evidence = [str(item) for item in (llm.get("evidence") or []) if str(item).strip()] if isinstance(llm.get("evidence"), list) else []
    rule_evidence = [str(item) for item in (rule.get("evidence") or []) if str(item).strip()] if isinstance(rule.get("evidence"), list) else []
    has_llm = bool(llm_category)
    has_llm_evidence = len(llm_evidence) >= max(1, min_llm_evidence)

    strategy = "prefer_rule"
    reason = "no_usable_llm_classification"
    final = dict(rule)
    review_required = bool(rule.get("review_required"))

    if not has_llm:
        pass
    elif not has_llm_evidence:
        strategy = "prefer_rule"
        reason = "llm_missing_evidence"
        review_required = review_required or bool(llm.get("review_required"))
    elif rule_confidence >= 0.85 and rule_evidence:
        strategy = "prefer_rule"
        reason = "high_confidence_rule_with_evidence"
        review_required = review_required or _categories_conflict(rule_category, llm_category)
    elif _categories_conflict(rule_category, llm_category) and rule_confidence >= 0.75 and llm_confidence >= 0.75:
        strategy = "conflict_review"
        reason = "high_confidence_rule_llm_conflict"
        final = {**rule, "conflict_status": "CONFLICT_REVIEW", "review_required": True}
        review_required = True
    elif rule_confidence < 0.70 and llm_confidence >= max(rule_confidence + 0.05, 0.70):
        strategy = "prefer_llm"
        reason = "low_confidence_rule_llm_with_evidence"
        final = {**rule, **llm}
        review_required = bool(final.get("review_required"))
    elif rule_category in {"unknown", "待研判", ""} and has_llm_evidence:
        strategy = "prefer_llm"
        reason = "rule_unknown_llm_with_evidence"
        final = {**rule, **llm}
        review_required = bool(final.get("review_required"))
    else:
        strategy = "prefer_rule"
        reason = "rule_not_outperformed_by_llm"
        review_required = review_required or bool(llm.get("review_required"))

    final.setdefault("risk_category", rule_category or "unknown")
    final.setdefault("secondary_label", rule.get("secondary_label") or "待研判")
    final.setdefault("confidence", rule_confidence)
    final["review_required"] = bool(review_required or final.get("review_required"))

    return ClassificationResolution(
        trace_id=str(trace_id or rule.get("source_trace_id") or rule.get("trace_id") or "unknown"),
        rule=rule,
        llm=llm,
        final=final,
        strategy=strategy,
        reason=reason,
        review_required=bool(final.get("review_required")),
    )


def _categories_conflict(left: str, right: str) -> bool:
    left = str(left or "").strip()
    right = str(right or "").strip()
    return bool(left and right and left not in {"unknown", "待研判"} and right not in {"unknown", "待研判"} and left != right)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = ["resolve_classification"]
