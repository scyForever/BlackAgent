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

    CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
        FRAUD_TRAFFIC: (
            "引流",
            "导流",
            "私域",
            "返利",
            "高佣",
            "开户链接",
            "拉新",
            "私聊进群",
            "加v",
            "加微",
            "加vx",
            "落地页",
            "跑分",
            "代付",
            "刷流水",
            "项目车队",
        ),
        ACCOUNT_TRADING: (
            "账号买卖",
            "卖号",
            "收号",
            "出号",
            "实名号",
            "实名认证",
            "白号",
            "老号",
            "养号",
            "接码",
            "验证码",
            "短信验证码",
            "实卡",
            "虚拟号码",
            "云短信",
            "短信平台",
            "验证码平台",
            "料子",
            "号商",
            "成品号",
            "飞机号",
            "电报号",
            "批量注册",
            "verified account",
            "二要素",
        ),
        TOOL_TRADING: (
            "群控",
            "云控",
            "脚本",
            "拉群端",
            "协议号",
            "外挂",
            "改机",
            "软件",
            "机器人",
            "教程",
            "更新",
            "版本",
            "功能",
            "监控",
            "系统",
            "自动化工具",
            "卡密",
            "接码平台",
            "打粉工具",
            "爬虫",
            "开控",
            "配置",
            "后台",
            "session",
            "自动注册",
            "官方软件",
            "启动",
            "分流链接",
            "粉丝列表",
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
        CROWD_SERVICE: (
            "拉人",
            "拉群",
            "群发",
            "私信",
            "代发",
            "接单",
            "接任务",
            "工作室",
            "精准客户",
            "采集群成员",
            "群成员采集",
            "活粉",
            "僵尸粉",
            "克隆粉",
            "代运营",
            "矩阵",
            "代投",
            "投放",
            "seo",
            "排名",
            "首页展示",
            "直通车",
            "推广",
            "获客",
            "转化",
            "打粉",
            "粉价",
            "全品类粉",
            "保开群",
            "拉满",
            "官媒",
            "卖服务",
            "订单",
            "回执",
            "成功率",
        ),
    }
    THEME_PRIORS: dict[str, tuple[str, int]] = {
        "众包任务": (CROWD_SERVICE, 2),
        "工具交易": (TOOL_TRADING, 2),
        "账号交易": (ACCOUNT_TRADING, 2),
        "接码": (ACCOUNT_TRADING, 2),
        "诈骗引流": (FRAUD_TRAFFIC, 2),
        "刷单作弊": (CLICK_FARMING, 2),
    }
    DEFENSIVE_CONTEXT = ("曝光", "辟谣", "警方通报", "安全通告", "新闻报道", "研究分析", "反诈提醒")

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

        score_map: dict[str, int] = {}
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            hits = sum(1 for keyword in keywords if keyword in text or keyword in matched_keywords)
            if hits:
                score_map[category] = hits
        for theme in matched_themes:
            mapped = self.THEME_PRIORS.get(theme)
            if mapped is None:
                continue
            category, bonus = mapped
            score_map[category] = score_map.get(category, 0) + bonus

        if not score_map:
            return build_classification_result(
                risk_category=UNKNOWN,
                confidence=0.35,
                review_required=True,
                classification_version=self.version,
                source_trace_id=source_trace_id,
            )

        best_category, hit_count = max(score_map.items(), key=lambda pair: (pair[1], pair[0] == CROWD_SERVICE, pair[0]))
        confidence = min(0.98, 0.62 + hit_count * 0.08)
        return build_classification_result(
            risk_category=best_category,
            confidence=confidence,
            review_required=confidence < 0.76 or best_category in {UNKNOWN, CROWD_SERVICE},
            classification_version=self.version,
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
