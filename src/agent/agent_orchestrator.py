"""Orchestrate deterministic backbone and controlled exploration sandbox."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable, Mapping
from uuid import uuid4

from storage import AuditEvent, AuditRepo, ReviewRepo
from storage.review_repo import normalize_decision

from .budget_manager import BudgetManager
from .exploration_agent import ExplorationAgent, ExplorationHypothesis
from .policy_guard import PolicyGuard
from .tool_registry import ToolRegistry


UNKNOWN_LABELS = ("unknown", "待研判", "未知", "conflict", "冲突", "review")
ANOMALOUS_SLANG_MARKERS = ("音符", "抖", "dy", "黑话", "暗号", "谐音", "变体")


@dataclass
class PipelineItemResult:
    source_trace_id: str
    route: str
    classification: dict[str, Any]
    entity_count: int = 0
    hypothesis: ExplorationHypothesis | None = None
    reason: str | None = None

    def model_dump(self) -> dict[str, Any]:
        data = asdict(self)
        if self.hypothesis is not None:
            data["hypothesis"] = self.hypothesis.model_dump(mode="json")
        return data


@dataclass
class PipelineRunResult:
    standard_count: int = 0
    sandbox_count: int = 0
    entity_count: int = 0
    review_count: int = 0
    items: list[PipelineItemResult] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return {
            "status": "completed",
            "mode": "controlled_exploration",
            "input_count": len(self.items),
            "standard_count": self.standard_count,
            "sandbox_count": self.sandbox_count,
            "entity_count": self.entity_count,
            "review_count": self.review_count,
            "items": [item.model_dump() for item in self.items],
        }


class AgentOrchestrator:
    """Main controller for PRD Step D.

    External backbone components can be injected by other workers.  When they
    are absent, deterministic local fallbacks keep Worker D's tests executable
    without writing outside ``src/agent``.
    """

    def __init__(
        self,
        *,
        cleaner: Any | None = None,
        classifier: Any | None = None,
        extractor: Any | None = None,
        entity_repo: Any | None = None,
        review_repo: Any | None = None,
        audit_repo: Any | None = None,
        exploration_agent: ExplorationAgent | None = None,
        confidence_threshold: float = 0.75,
        history: Iterable[Any] | None = None,
        settings: Any | None = None,
        config: Any | None = None,
    ) -> None:
        self.cleaner = cleaner
        self.classifier = classifier
        self.extractor = extractor
        self.entity_repo = entity_repo if entity_repo is not None else []
        self.review_repo = review_repo if review_repo is not None else ReviewRepo()
        self.audit_repo = audit_repo if audit_repo is not None else AuditRepo()
        self.confidence_threshold = confidence_threshold
        self.history = list(history or ())
        self.settings = settings
        self.config = config
        self.exploration_agent = exploration_agent or ExplorationAgent(
            tool_registry=ToolRegistry(),
            policy_guard=PolicyGuard(),
            budget_manager=BudgetManager(),
        )

    def run_pipeline(self, records: Iterable[Any] | Any) -> PipelineRunResult:
        if isinstance(records, Mapping) and isinstance(records.get("items"), list):
            iterable_records = list(records["items"])
        elif isinstance(records, Mapping) or not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
            iterable_records = [records]
        else:
            iterable_records = list(records)

        result = PipelineRunResult()
        for raw in iterable_records:
            item = self.process_one(raw)
            result.items.append(item)
            if item.route == "standard":
                result.standard_count += 1
                result.entity_count += item.entity_count
            else:
                result.sandbox_count += 1
                result.review_count += 1
        return result

    def process_one(self, raw: Any) -> PipelineItemResult:
        cleaned = self._clean(raw)
        classification: dict[str, Any]
        entities: list[dict[str, Any]]
        sandbox_reason: str | None = None

        try:
            classification = self._classify(cleaned)
        except Exception as exc:  # pragma: no cover - exercised by integration callers
            classification = {"risk_category": "unknown", "confidence": 0.0, "review_required": True, "error": str(exc)}
            sandbox_reason = "classification_error"

        try:
            entities = self._extract(cleaned)
        except Exception as exc:  # pragma: no cover - exercised by integration callers
            entities = []
            classification = {**classification, "review_required": True, "extract_error": str(exc)}
            sandbox_reason = sandbox_reason or "extraction_error"

        source_trace_id = str(_first_value(cleaned, raw, "source_trace_id", "trace_id", "hash_id", "id") or uuid4())
        route_to_sandbox, reason = self._should_route_to_sandbox(classification, entities, cleaned)
        sandbox_reason = sandbox_reason or reason

        if route_to_sandbox:
            hypothesis = self.exploration_agent.analyze(
                raw=raw,
                cleaned=cleaned,
                classification=classification,
                entities=entities,
                context={"history": self.history},
            )
            self._write_repo(self.review_repo, hypothesis)
            return PipelineItemResult(
                source_trace_id=source_trace_id,
                route="sandbox",
                classification=classification,
                entity_count=0,
                hypothesis=hypothesis,
                reason=sandbox_reason,
            )

        stored_entities = []
        for entity in entities:
            entity_with_source = dict(entity)
            entity_with_source.setdefault("source_trace_id", source_trace_id)
            self._write_repo(self.entity_repo, entity_with_source)
            stored_entities.append(entity_with_source)
        return PipelineItemResult(
            source_trace_id=source_trace_id,
            route="standard",
            classification=classification,
            entity_count=len(stored_entities),
        )

    pipeline_run = run_pipeline

    def list_review_tasks(self, status: str | None = "PENDING") -> list[dict[str, Any]]:
        if hasattr(self.review_repo, "list_task_views"):
            result = self.review_repo.list_task_views(status=status)
            return [_jsonable(item) for item in result]

        tasks = self.review_repo if isinstance(self.review_repo, list) else []
        rows = [_jsonable(item) for item in tasks]
        for row in rows:
            row.setdefault("review_state", {"status": "PENDING", "decision": None})
            row.setdefault("priority_features", _priority_features_from_row(row))
            row.setdefault("priority_score", row["priority_features"]["priority_score"])
        if status is not None:
            rows = [row for row in rows if row.get("review_state", {}).get("status") == status]
        return sorted(rows, key=lambda row: (-float(row.get("priority_score", 0.0)), str(row.get("created_at", ""))))

    def record_review_decision(
        self,
        hypothesis_id: str,
        *,
        decision: str,
        reviewer: str = "system",
        notes: str | None = None,
        edited_risk_type: str | None = None,
        secondary_label: str | None = None,
        corrected_entities: list[dict[str, Any]] | None = None,
        add_to_wordlist: bool = False,
    ) -> dict[str, Any]:
        """Apply a human workbench decision and append an audit event.

        The sandbox hypothesis is never mutated into a formal entity here; this
        method only changes review state and records feedback/audit metadata.
        """

        normalized_decision = normalize_decision(decision)
        if not hasattr(self.review_repo, "get") or not hasattr(self.review_repo, "decide"):
            raise TypeError("review decisions require a stateful review repository")

        hypothesis = self.review_repo.get(hypothesis_id)
        if hypothesis is None:
            raise KeyError(f"unknown hypothesis_id: {hypothesis_id}")

        state = self.review_repo.decide(
            hypothesis_id,
            decision=normalized_decision,
            reviewer=reviewer,
            notes=notes,
            edited_risk_type=edited_risk_type,
            secondary_label=secondary_label,
            corrected_entities=corrected_entities,
            add_to_wordlist=add_to_wordlist,
        )
        event = AuditEvent(
            event_type="review_decision_recorded",
            actor=reviewer,
            target_id=hypothesis_id,
            payload={
                "decision": normalized_decision.value,
                "source_trace_id": hypothesis.source_trace_id,
                "status": state.status,
                "notes": notes,
                "edits": {
                    "edited_risk_type": edited_risk_type,
                    "secondary_label": secondary_label,
                    "corrected_entities": corrected_entities or [],
                    "add_to_wordlist": add_to_wordlist,
                },
                "feedback_targets": _feedback_targets_for_decision(normalized_decision.value, add_to_wordlist),
                "sandbox_hypothesis_kept_review_only": hypothesis.requires_human_review is True,
            },
        )
        self._write_audit(event)
        return {
            "hypothesis": _jsonable(hypothesis),
            "review_state": state.model_dump() if hasattr(state, "model_dump") else _jsonable(state),
            "audit_event": event.model_dump(mode="json"),
        }

    def list_audit_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if hasattr(self.audit_repo, "list"):
            return [_jsonable(event) for event in self.audit_repo.list(event_type)]
        if isinstance(self.audit_repo, list):
            return [_jsonable(event) for event in self.audit_repo if event_type is None or _get(event, "event_type") == event_type]
        return []

    def _clean(self, raw: Any) -> Any:
        if self.cleaner is not None:
            for method_name in ("process_one", "clean_one", "clean", "process"):
                method = getattr(self.cleaner, method_name, None)
                if callable(method):
                    return method(raw)
        text = _first_text(raw)
        source_trace_id = _first_value(raw, "trace_id", "hash_id", "id") or str(uuid4())
        return {"source_trace_id": source_trace_id, "clean_text": text.strip()}

    def _classify(self, cleaned: Any) -> dict[str, Any]:
        if self.classifier is not None:
            classify = getattr(self.classifier, "classify", None) or getattr(self.classifier, "predict", None)
            if callable(classify):
                return _to_dict(classify(cleaned))
        text = _first_text(cleaned)
        lowered = text.lower()
        if any(marker.lower() in lowered for marker in ANOMALOUS_SLANG_MARKERS):
            return {"risk_category": "unknown", "confidence": 0.42, "review_required": True, "version": "fallback_v1"}
        keyword_map = {
            "接码": "account_trade",
            "账号": "account_trade",
            "刷单": "fraud_task",
            "跑分": "payment_fraud",
            "引流": "traffic_fraud",
            "群控": "tool_trade",
            "脚本": "tool_trade",
        }
        for keyword, label in keyword_map.items():
            if keyword in text:
                return {"risk_category": label, "confidence": 0.91, "review_required": False, "version": "fallback_v1"}
        return {"risk_category": "unknown", "confidence": 0.35, "review_required": True, "version": "fallback_v1"}

    def _extract(self, cleaned: Any) -> list[dict[str, Any]]:
        if self.extractor is not None:
            extract = getattr(self.extractor, "extract", None) or getattr(self.extractor, "run", None)
            if callable(extract):
                extracted = extract(cleaned)
                if isinstance(extracted, Mapping) and "entities" in extracted:
                    extracted = extracted["entities"]
                return [_to_dict(entity) for entity in (extracted or [])]
        text = _first_text(cleaned)
        entities: list[dict[str, Any]] = []
        for match in re.finditer(r"https?://[^\s]+", text):
            entities.append({"entity_type": "url", "entity_value": match.group(0), "start_offset": match.start(), "end_offset": match.end()})
        for match in re.finditer(r"(?:tg|telegram|wx|wechat|qq)[:：@]?[a-zA-Z0-9_\-]{3,}", text, flags=re.IGNORECASE):
            entities.append({"entity_type": "contact", "entity_value": match.group(0), "start_offset": match.start(), "end_offset": match.end()})
        for keyword in ("接码", "群控", "脚本", "跑分", "刷单"):
            start = text.find(keyword)
            if start >= 0:
                entities.append({"entity_type": "tool_or_keyword", "entity_value": keyword, "start_offset": start, "end_offset": start + len(keyword)})
        for marker in ANOMALOUS_SLANG_MARKERS:
            start = text.lower().find(marker.lower())
            if start >= 0:
                entities.append({"entity_type": "unknown_slang", "entity_value": marker, "start_offset": start, "end_offset": start + len(marker)})
        return entities

    def _should_route_to_sandbox(self, classification: Mapping[str, Any], entities: list[Mapping[str, Any]], cleaned: Any) -> tuple[bool, str | None]:
        confidence = float(classification.get("confidence", 0.0) or 0.0)
        label = str(classification.get("risk_category") or classification.get("label") or "").lower()
        if confidence < self.confidence_threshold:
            return True, "low_confidence"
        if any(marker in label for marker in UNKNOWN_LABELS):
            return True, "unknown_label"
        if classification.get("review_required") is True:
            return True, "classification_review_required"
        if any(str(entity.get("entity_type", "")).lower() in {"unknown_slang", "slang_variant", "new_slang"} for entity in entities):
            return True, "anomalous_slang"
        text = _first_text(cleaned).lower()
        if any(marker.lower() in text for marker in ANOMALOUS_SLANG_MARKERS):
            return True, "anomalous_slang"
        return False, None

    def _write_repo(self, repo: Any | None, item: Any) -> None:
        if repo is None:
            return
        if isinstance(repo, list):
            repo.append(item)
            return
        for method_name in ("add", "append", "store", "save", "enqueue", "create"):
            method = getattr(repo, method_name, None)
            if callable(method):
                method(item)
                return
        raise TypeError(f"Repository {repo!r} does not expose add/append/store/save/enqueue/create")

    def _write_audit(self, event: AuditEvent) -> None:
        if self.audit_repo is None:
            return
        if isinstance(self.audit_repo, list):
            self.audit_repo.append(event)
            return
        for method_name in ("append", "add", "store", "save", "create"):
            method = getattr(self.audit_repo, method_name, None)
            if callable(method):
                method(event)
                return
        raise TypeError(f"Audit repository {self.audit_repo!r} does not expose append/add/store/save/create")


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


def _jsonable(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    return {"value": value}


def _priority_features_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    confidence = max(float(row.get("confidence", 0.01) or 0.01), 0.01)
    spread_count = max(1, len(row.get("supporting_evidence_ids") or ()))
    risk_text = f"{row.get('suggested_label', '')} {row.get('hypothesis_summary', '')} {row.get('hypothesis_type', '')}".lower()
    if any(marker in risk_text for marker in ("payment", "account", "tool", "fraud", "跑分", "接码", "账号", "群控", "脚本", "诈骗")):
        risk_level, risk_weight = "HIGH", 3
    elif any(marker in risk_text for marker in ("unknown", "slang", "黑话", "未知", "变体", "暗号")):
        risk_level, risk_weight = "MEDIUM", 2
    else:
        risk_level, risk_weight = "LOW", 1
    inverse_confidence = round(1.0 / confidence, 4)
    priority_score = round(risk_weight * inverse_confidence * spread_count, 4)
    return {
        "risk_level": risk_level,
        "risk_weight": risk_weight,
        "confidence": float(row.get("confidence", 0.0) or 0.0),
        "inverse_confidence": inverse_confidence,
        "spread_count": spread_count,
        "priority_score": priority_score,
    }


def _feedback_targets_for_decision(decision: str, add_to_wordlist: bool) -> list[str]:
    targets: list[str] = []
    if decision == "MISREPORT":
        targets.extend(["whitelist_candidate_pool", "offline_negative_sample_set"])
    if decision == "APPROVED" and add_to_wordlist:
        targets.extend(["dynamic_wordlist_candidate_pool", "few_shot_example_pool"])
    if decision == "ESCALATE":
        targets.append("expert_review_queue")
    return targets


def _get(value: Any, key: str, default: Any = None) -> Any:
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
