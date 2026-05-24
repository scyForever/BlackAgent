"""Rule fast-track intent classifier for MVP known risk labels."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Any, Iterable

from src.collector.base_collector import get_record_field
from src.cleaner.text_filter import normalize_text


FRAUD_TRAFFIC = "诈骗引流"
ACCOUNT_TRADING = "账号交易"
TOOL_TRADING = "工具交易"
CLICK_FARMING = "刷单作弊"
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

    version = "rule_fast_track_v1"

    CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
        FRAUD_TRAFFIC: (
            "引流",
            "返利",
            "高佣",
            "开户链接",
            "开户链接",
            "开户链接",
            "拉新",
            "私聊进群",
            "跑分",
            "代付",
            "刷流水",
            "刷流水",
            "项目车队",
        ),
        ACCOUNT_TRADING: (
            "账号买卖",
            "卖号",
            "收号",
            "实名号",
            "白号",
            "老号",
            "养号",
            "接码",
            "料子",
            "批量注册",
        ),
        TOOL_TRADING: (
            "群控",
            "脚本",
            "协议号",
            "外挂",
            "改机",
            "自动化工具",
            "卡密",
            "接码平台",
            "打粉工具",
            "爬虫",
        ),
        CLICK_FARMING: (
            "刷单",
            "补单",
            "放单",
            "点赞任务",
            "关注任务",
            "做任务",
            "垫付",
            "返佣",
            "日结",
            "兼职",
        ),
    }
    DEFENSIVE_CONTEXT = ("曝光", "辟谣", "警方通报", "安全通告", "新闻报道", "研究分析", "反诈提醒")

    def classify(self, item: Any) -> Any:
        text = normalize_text(str(get_record_field(item, "clean_text") or get_record_field(item, "content_text") or item))
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
                classification_version=self.version,
                source_trace_id=source_trace_id,
            )

        if any(keyword in text for keyword in self.DEFENSIVE_CONTEXT):
            return build_classification_result(
                risk_category=NORMAL_NOISE,
                confidence=0.82,
                review_required=False,
                classification_version=self.version,
                source_trace_id=source_trace_id,
            )

        scores: list[tuple[str, int]] = []
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            hits = sum(1 for keyword in keywords if keyword in text)
            if hits:
                scores.append((category, hits))

        if not scores:
            return build_classification_result(
                risk_category=UNKNOWN,
                confidence=0.35,
                review_required=True,
                classification_version=self.version,
                source_trace_id=source_trace_id,
            )

        best_category, hit_count = max(scores, key=lambda pair: pair[1])
        confidence = min(0.98, 0.68 + hit_count * 0.09)
        return build_classification_result(
            risk_category=best_category,
            confidence=confidence,
            review_required=confidence < 0.75 or best_category == UNKNOWN,
            classification_version=self.version,
            source_trace_id=source_trace_id,
        )

    def classify_batch(self, items: Iterable[Any]) -> list[Any]:
        return [self.classify(item) for item in items]


__all__ = [
    "ACCOUNT_TRADING",
    "CLICK_FARMING",
    "FRAUD_TRAFFIC",
    "NORMAL_NOISE",
    "TOOL_TRADING",
    "UNKNOWN",
    "FallbackClassificationResult",
    "RuleFastTrackClassifier",
    "build_classification_result",
]
