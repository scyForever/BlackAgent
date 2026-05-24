"""In-memory human review queue for sandbox exploration hypotheses."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from threading import RLock
from typing import Any
from uuid import UUID

from .schemas import ExplorationHypothesis, ReviewDecision, utc_now


@dataclass(frozen=True)
class ReviewState:
    """Lightweight queue state kept outside the hypothesis contract."""

    hypothesis_id: str
    status: str
    reviewer: str | None = None
    decision: ReviewDecision | None = None
    notes: str | None = None
    edited_risk_type: str | None = None
    secondary_label: str | None = None
    corrected_entities: tuple[dict[str, Any], ...] = ()
    add_to_wordlist: bool = False
    updated_at: datetime | None = None

    def model_dump(self) -> dict[str, Any]:
        data = asdict(self)
        if self.decision is not None:
            data["decision"] = self.decision.value
        if self.updated_at is not None:
            data["updated_at"] = self.updated_at.isoformat()
        return data


class InMemoryReviewRepo:
    """Queue exploration hypotheses without promoting them to official stores."""

    PENDING = "PENDING"
    REVIEWED = "REVIEWED"
    UNCERTAIN = "UNCERTAIN"
    ESCALATED = "ESCALATED"

    def __init__(self) -> None:
        self._hypotheses: dict[str, ExplorationHypothesis] = {}
        self._states: dict[str, ReviewState] = {}
        self._lock = RLock()

    def add_hypothesis(self, hypothesis: ExplorationHypothesis) -> ExplorationHypothesis:
        if hypothesis.requires_human_review is not True:
            raise ValueError("review queue only accepts hypotheses requiring human review")

        record = hypothesis.model_copy(deep=True)
        hypothesis_id = str(record.hypothesis_id)
        with self._lock:
            self._hypotheses[hypothesis_id] = record
            self._states[hypothesis_id] = ReviewState(
                hypothesis_id=hypothesis_id,
                status=self.PENDING,
                updated_at=utc_now(),
            )
        return record.model_copy(deep=True)

    # Generic repository alias used by the orchestrator's duck-typed writer.
    def add(self, hypothesis: ExplorationHypothesis) -> ExplorationHypothesis:
        return self.add_hypothesis(hypothesis)

    def get(self, hypothesis_id: str | UUID) -> ExplorationHypothesis | None:
        with self._lock:
            record = self._hypotheses.get(str(hypothesis_id))
            return record.model_copy(deep=True) if record else None

    def get_state(self, hypothesis_id: str | UUID) -> ReviewState | None:
        with self._lock:
            return self._states.get(str(hypothesis_id))

    def list_tasks(self, status: str | None = None) -> list[ExplorationHypothesis]:
        with self._lock:
            ids = [
                hypothesis_id
                for hypothesis_id, state in self._states.items()
                if status is None or state.status == status
            ]
            return [self._hypotheses[hypothesis_id].model_copy(deep=True) for hypothesis_id in ids]

    def list_task_views(self, status: str | None = PENDING) -> list[dict[str, Any]]:
        """Return flattened workbench rows sorted by PRD priority.

        Priority follows PRD section 11: risk level x inverse confidence x
        spread count. A larger score is more urgent, so results are returned in
        descending priority while preserving oldest-first ordering for ties.
        """

        with self._lock:
            rows: list[dict[str, Any]] = []
            for hypothesis_id, hypothesis in self._hypotheses.items():
                state = self._states[hypothesis_id]
                if status is not None and state.status != status:
                    continue
                priority = self._priority_features(hypothesis)
                row = hypothesis.model_dump(mode="json")
                row["review_state"] = state.model_dump()
                row["priority_score"] = priority["priority_score"]
                row["priority_features"] = priority
                rows.append(row)
            return sorted(
                rows,
                key=lambda row: (-float(row["priority_score"]), str(row.get("created_at", ""))),
            )

    def mark_reviewed(
        self,
        hypothesis_id: str | UUID,
        *,
        decision: str,
        reviewer: str = "system",
        notes: str | None = None,
        edited_risk_type: str | None = None,
        secondary_label: str | None = None,
        corrected_entities: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        add_to_wordlist: bool = False,
    ) -> ReviewState:
        key = str(hypothesis_id)
        normalized_decision = normalize_decision(decision)
        status = self._status_for_decision(normalized_decision)
        with self._lock:
            if key not in self._hypotheses:
                raise KeyError(f"unknown hypothesis_id: {hypothesis_id}")
            state = ReviewState(
                hypothesis_id=key,
                status=status,
                reviewer=reviewer,
                decision=normalized_decision,
                notes=notes,
                edited_risk_type=edited_risk_type,
                secondary_label=secondary_label,
                corrected_entities=tuple(deepcopy(list(corrected_entities or ()))),
                add_to_wordlist=add_to_wordlist,
                updated_at=utc_now(),
            )
            self._states[key] = state
            return state

    def decide(
        self,
        hypothesis_id: str | UUID,
        *,
        decision: str,
        reviewer: str = "system",
        notes: str | None = None,
        edited_risk_type: str | None = None,
        secondary_label: str | None = None,
        corrected_entities: list[dict[str, Any]] | None = None,
        add_to_wordlist: bool = False,
    ) -> ReviewState:
        """Record a workbench decision using explicit PRD decision names."""

        return self.mark_reviewed(
            hypothesis_id,
            decision=decision,
            reviewer=reviewer,
            notes=notes,
            edited_risk_type=edited_risk_type,
            secondary_label=secondary_label,
            corrected_entities=corrected_entities,
            add_to_wordlist=add_to_wordlist,
        )

    def clear(self) -> None:
        with self._lock:
            self._hypotheses.clear()
            self._states.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._hypotheses)

    def _status_for_decision(self, decision: ReviewDecision) -> str:
        if decision == ReviewDecision.UNCERTAIN:
            return self.UNCERTAIN
        if decision == ReviewDecision.ESCALATE:
            return self.ESCALATED
        return self.REVIEWED

    def _priority_features(self, hypothesis: ExplorationHypothesis) -> dict[str, Any]:
        risk_level, risk_weight = _risk_level_for(hypothesis)
        confidence = max(float(hypothesis.confidence), 0.01)
        inverse_confidence = round(1.0 / confidence, 4)
        spread_count = max(1, len(hypothesis.supporting_evidence_ids))
        priority_score = round(risk_weight * inverse_confidence * spread_count, 4)
        return {
            "risk_level": risk_level,
            "risk_weight": risk_weight,
            "confidence": float(hypothesis.confidence),
            "inverse_confidence": inverse_confidence,
            "spread_count": spread_count,
            "priority_score": priority_score,
        }


def normalize_decision(decision: str | ReviewDecision) -> ReviewDecision:
    """Normalize API/user decision spellings to the shared enum."""

    if isinstance(decision, ReviewDecision):
        return decision

    normalized = str(decision).strip().upper().replace("-", "_")
    aliases = {
        "APPROVE": ReviewDecision.APPROVED,
        "APPROVED": ReviewDecision.APPROVED,
        "CONFIRM": ReviewDecision.APPROVED,
        "CONFIRMED_RISK": ReviewDecision.APPROVED,
        "确认风险": ReviewDecision.APPROVED,
        "MISREPORT": ReviewDecision.MISREPORT,
        "FALSE_POSITIVE": ReviewDecision.MISREPORT,
        "误报": ReviewDecision.MISREPORT,
        "UNCERTAIN": ReviewDecision.UNCERTAIN,
        "UNSURE": ReviewDecision.UNCERTAIN,
        "暂不确定": ReviewDecision.UNCERTAIN,
        "ESCALATE": ReviewDecision.ESCALATE,
        "ESCALATED": ReviewDecision.ESCALATE,
        "需升级专家": ReviewDecision.ESCALATE,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        allowed = ", ".join(item.value for item in ReviewDecision)
        raise ValueError(f"unsupported review decision: {decision!r}; expected one of {allowed}") from exc


def _risk_level_for(hypothesis: ExplorationHypothesis) -> tuple[str, int]:
    label = (hypothesis.suggested_label or "").lower()
    summary = hypothesis.hypothesis_summary.lower()
    text = f"{label} {summary} {hypothesis.hypothesis_type.value.lower()}"

    high_markers = ("payment", "account", "tool", "fraud", "跑分", "接码", "账号", "群控", "脚本", "诈骗")
    medium_markers = ("slang", "unknown", "new_", "黑话", "未知", "变体", "暗号")
    if any(marker in text for marker in high_markers):
        return "HIGH", 3
    if hypothesis.hypothesis_type.value == "SUSPECTED_CLUSTER":
        return "HIGH", 3
    if any(marker in text for marker in medium_markers):
        return "MEDIUM", 2
    return "LOW", 1


ReviewRepo = InMemoryReviewRepo

__all__ = ["InMemoryReviewRepo", "ReviewRepo", "ReviewState", "normalize_decision"]
