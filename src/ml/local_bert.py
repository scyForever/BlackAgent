"""Optional local BERT-style classification / NER pre-stage.

No heavyweight ML dependency is imported here.  Production code can inject a
callable model runner, while tests and offline delivery use deterministic
signals to verify the routing contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from src.enhancement.text_intelligence import AdvancedEntityExtractor, FineGrainedIntentClassifier


ModelRunner = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class LocalBertConfig:
    enabled: bool = False
    model_name: str = "not_configured"
    min_confidence: float = 0.72
    route_llm_below_confidence: float = 0.58

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LocalBertResult:
    status: str
    risk_category: str
    secondary_label: str
    confidence: float
    entities: list[dict[str, Any]] = field(default_factory=list)
    should_route_to_llm: bool = False
    reason: str = "local_bert_adapter"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class LocalBertAdapter:
    """Adapter-shaped BERT/NER stage before expensive LLM enrichment."""

    def __init__(
        self,
        *,
        config: LocalBertConfig | None = None,
        runner: ModelRunner | None = None,
        classifier: FineGrainedIntentClassifier | None = None,
        extractor: AdvancedEntityExtractor | None = None,
    ) -> None:
        self.config = config or LocalBertConfig()
        self.runner = runner
        self.classifier = classifier or FineGrainedIntentClassifier()
        self.extractor = extractor or AdvancedEntityExtractor()

    def analyze(self, text: str) -> LocalBertResult:
        if self.config.enabled and self.runner is not None:
            return self._from_runner(dict(self.runner(text)))
        deterministic = self.classifier.classify({"trace_id": "local-bert-prestage", "content_text": text})
        entities = [
            _prestage_entity_payload(item.model_dump())
            for item in self.extractor.extract({"trace_id": "local-bert-prestage", "content_text": text})
        ]
        confidence = float(deterministic.confidence)
        return LocalBertResult(
            status="deterministic_fallback" if not self.config.enabled else "model_runner_missing",
            risk_category=deterministic.risk_category,
            secondary_label=deterministic.secondary_label,
            confidence=round(confidence, 4),
            entities=entities,
            should_route_to_llm=confidence < self.config.route_llm_below_confidence or deterministic.conflict_status == "CONFLICT_REVIEW",
            reason="no_local_model_configured" if not self.config.enabled else "enabled_without_runner",
        )

    def _from_runner(self, payload: Mapping[str, Any]) -> LocalBertResult:
        confidence = float(payload.get("confidence") or 0.0)
        return LocalBertResult(
            status="model_runner",
            risk_category=str(payload.get("risk_category") or "unknown"),
            secondary_label=str(payload.get("secondary_label") or "待研判"),
            confidence=round(confidence, 4),
            entities=[dict(item) for item in payload.get("entities", []) if isinstance(item, Mapping)],
            should_route_to_llm=confidence < self.config.route_llm_below_confidence or bool(payload.get("has_conflict")),
            reason=str(payload.get("reason") or "local_model_runner"),
        )


def _prestage_entity_payload(entity: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(entity)
    normalized = str(payload.get("normalized_value") or "")
    if str(payload.get("entity_type") or "") == "contact" and normalized.lower().startswith("telegram:"):
        bare_value = normalized.split(":", 1)[1]
        payload["normalized_value"] = bare_value
        payload["canonical_hash"] = hashlib.sha256(f"contact:{bare_value.lower()}".encode("utf-8")).hexdigest()
        payload["masked_value"] = f"***{bare_value[-2:]}" if len(bare_value) > 2 else "***"
    return payload


__all__ = ["LocalBertAdapter", "LocalBertConfig", "LocalBertResult"]
