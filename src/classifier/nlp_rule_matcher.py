"""Rule fast-track intent classifier for MVP known risk labels."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Any, Iterable

from src.collector.base_collector import get_record_field
from src.cleaner.text_filter import normalize_text
from src.rules import RuleRegistry


FRAUD_TRAFFIC = "诈骗引流"
ACCOUNT_TRADING = "账号交易"
TOOL_TRADING = "工具交易"
CLICK_FARMING = "刷单作弊"
CROWD_SERVICE = "众包服务"
NORMAL_NOISE = "正常业务白噪声"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class FallbackClassificationResult:
    risk_category: str
    confidence: float
    review_required: bool
    classification_version: str
    source_trace_id: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


CLASSIFICATION_SCHEMA_MODEL = None


def _load_classification_schema_model() -> type[Any]:
    global CLASSIFICATION_SCHEMA_MODEL
    if CLASSIFICATION_SCHEMA_MODEL is not None:
        return CLASSIFICATION_SCHEMA_MODEL
    try:
        CLASSIFICATION_SCHEMA_MODEL = getattr(import_module("storage.schemas"), "ClassificationResult")
    except Exception:
        CLASSIFICATION_SCHEMA_MODEL = FallbackClassificationResult
    return CLASSIFICATION_SCHEMA_MODEL


def _schema_fields(model: type[Any]) -> set[str]:
    return set(getattr(model, "model_fields", {}) or getattr(model, "__annotations__", {}) or [])


def build_classification_result(
    *,
    risk_category: str,
    confidence: float,
    review_required: bool,
    classification_version: str,
    source_trace_id: str | None = None,
) -> Any:
    payload = {
        "source_trace_id": source_trace_id,
        "risk_category": risk_category,
        "confidence": round(confidence, 4),
        "review_required": review_required,
        "classification_version": classification_version,
        "decision_version": classification_version,
    }
    model = _load_classification_schema_model()
    fields = _schema_fields(model)
    if fields:
        version_field = next(
            (
                name
                for name in ("decision_version", "classification_version", "classifier_version", "rule_version", "version")
                if name in fields
            ),
            "classification_version",
        )
        payload_for_schema = {
            "source_trace_id": source_trace_id or "unknown",
            "risk_category": payload["risk_category"],
            "confidence": payload["confidence"],
            "review_required": payload["review_required"],
            version_field: classification_version,
        }
        payload_for_schema = {key: value for key, value in payload_for_schema.items() if key in fields}
    else:
        payload_for_schema = payload
    try:
        return model(**payload_for_schema)  # type: ignore[misc,operator]
    except Exception:
        return FallbackClassificationResult(
            risk_category=risk_category,
            confidence=round(confidence, 4),
            review_required=review_required,
            classification_version=classification_version,
            source_trace_id=source_trace_id,
        )


class RuleFastTrackClassifier:
    """Deterministic high-precision rule classifier for known MVP labels."""

    version = "rule_fast_track_v2"

    def __init__(self, rule_registry: RuleRegistry | None = None) -> None:
        self.rule_registry = rule_registry or RuleRegistry()
        self.category_keywords = self.rule_registry.primary_terms_by_label()
        self.theme_priors = self.rule_registry.theme_priors()
        self.defensive_context = self.rule_registry.defensive_markers()
        self.rule_version = self.rule_registry.version_hash()

    def _signal_terms(self, item: Any, field_name: str) -> tuple[str, ...]:
        values = get_record_field(item, field_name) or ()
        if isinstance(values, str):
            values = [values]
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values if isinstance(values, Iterable) else ():
            value = normalize_text(str(raw))
            if not value:
                continue
            lowered = value.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(value)
        return tuple(normalized)

    def classify(self, item: Any) -> Any:
        text = normalize_text(str(get_record_field(item, "clean_text") or get_record_field(item, "content_text") or item))
        matched_keywords = self._signal_terms(item, "matched_keywords")
        matched_themes = self._signal_terms(item, "matched_themes")
        source_trace_id = str(
            get_record_field(item, "source_trace_id")
            or get_record_field(item, "trace_id")
            or get_record_field(item, "hash_id")
            or "unknown"
        )
        if not text:
            return build_classification_result(
                risk_category=UNKNOWN,
                confidence=0.0,
                review_required=True,
                classification_version=self.rule_version,
                source_trace_id=source_trace_id,
            )

        if any(keyword in text for keyword in self.defensive_context):
            return build_classification_result(
                risk_category=NORMAL_NOISE,
                confidence=0.82,
                review_required=False,
                classification_version=self.rule_version,
                source_trace_id=source_trace_id,
            )

        score_map: dict[str, int] = {}
        for category, keywords in self.category_keywords.items():
            hits = sum(1 for keyword in keywords if keyword in text or keyword in matched_keywords)
            if hits:
                score_map[category] = hits
        for theme in matched_themes:
            mapped = self.theme_priors.get(theme)
            if mapped is None:
                continue
            category, bonus = mapped
            score_map[category] = score_map.get(category, 0) + bonus

        if not score_map:
            return build_classification_result(
                risk_category=UNKNOWN,
                confidence=0.35,
                review_required=True,
                classification_version=self.rule_version,
                source_trace_id=source_trace_id,
            )

        best_category, hit_count = max(score_map.items(), key=lambda pair: (pair[1], pair[0] == CROWD_SERVICE, pair[0]))
        confidence = min(0.98, 0.62 + hit_count * 0.08)
        return build_classification_result(
            risk_category=best_category,
            confidence=confidence,
            review_required=confidence < 0.76 or best_category in {UNKNOWN, CROWD_SERVICE},
            classification_version=self.rule_version,
            source_trace_id=source_trace_id,
        )

    def classify_batch(self, items: Iterable[Any]) -> list[Any]:
        return [self.classify(item) for item in items]


__all__ = [
    "ACCOUNT_TRADING",
    "CLICK_FARMING",
    "CROWD_SERVICE",
    "FRAUD_TRAFFIC",
    "NORMAL_NOISE",
    "TOOL_TRADING",
    "UNKNOWN",
    "FallbackClassificationResult",
    "RuleFastTrackClassifier",
    "build_classification_result",
]
