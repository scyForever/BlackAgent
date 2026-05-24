"""Deterministic controlled exploration agent for low-confidence samples."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from storage.schemas import ExplorationHypothesis

from .budget_manager import BudgetExceeded, BudgetManager
from .policy_guard import PolicyGuard
from .tool_registry import ToolRegistry


class ExplorationAgent:
    """Sandboxed analyzer for unknown, low-confidence, or slang-like samples."""

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        policy_guard: PolicyGuard | None = None,
        budget_manager: BudgetManager | None = None,
    ) -> None:
        self.tool_registry = tool_registry or ToolRegistry()
        self.policy_guard = policy_guard or PolicyGuard()
        self.budget_manager = budget_manager or BudgetManager()

    def analyze(
        self,
        *,
        raw: Any,
        cleaned: Any | None = None,
        classification: Any | None = None,
        entities: list[Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> ExplorationHypothesis:
        """Generate a deterministic review-only hypothesis."""

        self.budget_manager.reset()
        context = context or {}
        text = _first_text(cleaned, raw)
        source_trace_id = _first_value(cleaned, raw, "source_trace_id", "trace_id", "hash_id", "id") or str(uuid4())
        classification_label = str(_get(classification, "risk_category", _get(classification, "label", "unknown")))
        classification_confidence = float(_get(classification, "confidence", 0.0) or 0.0)
        entities = entities or []

        try:
            self.policy_guard.check_action_safety({"type": "tool_call", "tool": "local_db_lookup", "target": "local_sandbox"})
            similar_records = self.tool_registry.call(
                "local_db_lookup",
                text,
                corpus=context.get("history", ()),
                limit=3,
            )
            self.budget_manager.consume(rounds=1, tokens=self._estimate_tokens(text) + 64)

            self.policy_guard.check_action_safety({"type": "tool_call", "tool": "slang_similarity_search", "target": "local_sandbox"})
            slang_candidates = self.tool_registry.call(
                "slang_similarity_search",
                text,
                slang_terms=context.get("slang_terms"),
                limit=3,
            )
            self.budget_manager.consume(rounds=1, tokens=64)
            snapshot = self.budget_manager.assert_within_budget()
            budget = snapshot.consumed
            budget["elapsed_ms"] = snapshot.elapsed_ms
            return self._build_hypothesis(
                source_trace_id=source_trace_id,
                text=text,
                classification_label=classification_label,
                classification_confidence=classification_confidence,
                entities=entities,
                similar_records=similar_records,
                slang_candidates=slang_candidates,
                budget_consumed=budget,
            )
        except BudgetExceeded as exc:
            budget = exc.snapshot.consumed
            budget["elapsed_ms"] = exc.snapshot.elapsed_ms
            hypothesis = ExplorationHypothesis(
                source_trace_id=source_trace_id,
                hypothesis_type="NEW_RISK_PATTERN",
                hypothesis_summary=f"探索预算触顶({exc.reason})，保留当前样本为人工复核候选。",
                supporting_evidence_ids=[source_trace_id],
                suggested_label=classification_label if classification_label else "unknown",
                confidence=min(classification_confidence, 0.5),
                budget_consumed=budget,
            )
            self.policy_guard.assert_review_only(hypothesis)
            return hypothesis

    def _build_hypothesis(
        self,
        *,
        source_trace_id: str,
        text: str,
        classification_label: str,
        classification_confidence: float,
        entities: list[Any],
        similar_records: list[Any],
        slang_candidates: list[dict[str, Any]],
        budget_consumed: dict[str, int],
    ) -> ExplorationHypothesis:
        evidence_ids = [source_trace_id]
        for record in similar_records:
            evidence_id = _first_value(record, "trace_id", "hash_id", "id", "source_trace_id")
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(str(evidence_id))

        top_slang = slang_candidates[0] if slang_candidates else None
        has_slang = bool(top_slang and float(top_slang.get("score", 0.0)) >= 0.5)
        hypothesis_type = "NEW_SLANG_VARIANT" if has_slang else ("SUSPECTED_CLUSTER" if len(evidence_ids) > 1 else "NEW_RISK_PATTERN")
        suggested_label = classification_label if classification_label and "unknown" not in classification_label.lower() else "unknown_risk_pattern"
        confidence = max(0.2, min(0.74, (classification_confidence or 0.35) + (0.12 if similar_records else 0.0) + (0.08 if has_slang else 0.0)))

        entity_bits = []
        for entity in entities[:3]:
            entity_type = _get(entity, "entity_type", _get(entity, "type", "entity"))
            entity_value = _get(entity, "entity_value", _get(entity, "value", ""))
            if entity_value:
                entity_bits.append(f"{entity_type}:{entity_value}")
        evidence_note = f"；局部相似样本 {len(evidence_ids) - 1} 条" if len(evidence_ids) > 1 else ""
        slang_note = f"；候选黑话“{top_slang['term']}”" if top_slang else ""
        entity_note = f"；实体线索 {', '.join(entity_bits)}" if entity_bits else ""
        summary = f"样本进入受控探索：原分类={suggested_label}，疑似未知/低置信风险模式{slang_note}{evidence_note}{entity_note}。"

        normalized_term = {"raw": str(top_slang["term"]), "target": str(top_slang["term"])} if top_slang else None
        if source_trace_id not in evidence_ids:
            evidence_ids.insert(0, source_trace_id)
        hypothesis = ExplorationHypothesis(
            source_trace_id=source_trace_id,
            hypothesis_type=hypothesis_type,
            hypothesis_summary=summary,
            supporting_evidence_ids=evidence_ids,
            suggested_label=suggested_label,
            suggested_normalized_term=normalized_term,
            confidence=round(confidence, 4),
            budget_consumed=budget_consumed,
        )
        self.policy_guard.assert_review_only(hypothesis)
        self.policy_guard.check_action_safety({"type": "write", "target": "review_repo", "payload": hypothesis.model_dump()})
        return hypothesis

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _first_value(*values: Any) -> str | None:
    keys = values[-4:] if values and all(isinstance(item, str) for item in values[-4:]) else ()
    objects = values[:-4] if keys else values
    for obj in objects:
        for key in keys or ("source_trace_id", "trace_id", "hash_id", "id"):
            found = _get(obj, key)
            if found:
                return str(found)
    return None


def _first_text(*values: Any) -> str:
    for obj in values:
        for key in ("clean_text", "content_text", "text", "raw_text"):
            found = _get(obj, key)
            if found:
                return str(found)
    return str(values[0]) if values else ""
