"""Risk assertion polarity scoring for defensive/no-risk contexts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.cleaner.text_filter import normalize_text


DEFENSIVE_NOTICE = "defensive_notice"
PLATFORM_POLICY = "platform_policy"
RESEARCH = "research"
NEGATIVE_RISK_ASSERTION = "negative_risk_assertion"
RISK_SOLICITATION = "risk_solicitation"
RISK_ASSERTION = "risk_assertion"
NEUTRAL = "neutral"


DEFAULT_DEFENSIVE_MARKERS: tuple[str, ...] = (
    "严禁",
    "禁止",
    "切勿参与",
    "不要参与",
    "不提供",
    "举报",
    "用户应举报",
    "平台公告",
    "治理公告",
    "规则公告",
    "安全公告",
    "反诈",
    "反诈提醒",
    "警方通报",
    "公安通报",
    "安全研究",
    "研究分析",
    "自动化测试",
    "测试脚本",
    "覆盖率",
    "治理复盘",
    "案例复盘",
    "新闻曝光",
    "曝光",
    "辟谣",
)

DEFAULT_NEGATION_MARKERS: tuple[str, ...] = (
    "无垫付",
    "无返佣",
    "不垫付",
    "不返佣",
    "非刷单",
    "非群控",
    "不买卖",
    "不交易",
    "不接码",
    "不提供",
)

DEFAULT_RESEARCH_MARKERS: tuple[str, ...] = (
    "安全研究",
    "研究分析",
    "代码讨论",
    "自动化测试",
    "测试脚本",
    "覆盖率",
    "论文",
    "复盘",
)

DEFAULT_SOLICITATION_MARKERS: tuple[str, ...] = (
    "出售",
    "低价",
    "价格",
    "报价",
    "上车",
    "招募",
    "接单",
    "下单",
    "欢迎咨询",
    "联系客服",
    "业务联系",
    "联系",
    "对接",
    "合作",
    "包量",
    "秒出",
    "返佣",
    "垫付",
    "卖号",
    "收号",
    "出号",
)


@dataclass(frozen=True)
class RiskPolarityDecision:
    topic_risk: bool
    actor_intent: str
    polarity: str
    confidence: float
    evidence: list[str] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class RiskPolarityScorer:
    """Separate risk-topic hits from whether the text asserts active abuse."""

    def __init__(
        self,
        *,
        defensive_markers: Iterable[str] = (),
        negation_markers: Iterable[str] = (),
        research_markers: Iterable[str] = (),
        solicitation_markers: Iterable[str] = (),
    ) -> None:
        self.defensive_markers = _unique([*DEFAULT_DEFENSIVE_MARKERS, *defensive_markers])
        self.negation_markers = _unique([*DEFAULT_NEGATION_MARKERS, *negation_markers])
        self.research_markers = _unique([*DEFAULT_RESEARCH_MARKERS, *research_markers])
        self.solicitation_markers = _unique([*DEFAULT_SOLICITATION_MARKERS, *solicitation_markers])

    def score(self, text: str, *, topic_terms: Iterable[str] = ()) -> RiskPolarityDecision:
        normalized = normalize_text(text)
        topic_hits = _hits(normalized, topic_terms)
        defensive_hits = _hits(normalized, self.defensive_markers)
        negation_hits = _hits(normalized, self.negation_markers)
        research_hits = _hits(normalized, self.research_markers)
        solicitation_hits = _hits(normalized, self.solicitation_markers)
        evidence = _unique([*defensive_hits, *negation_hits, *research_hits, *solicitation_hits, *topic_hits])

        if defensive_hits and not self._has_unnegated_solicitation(normalized, solicitation_hits):
            actor = PLATFORM_POLICY if any(marker in normalized for marker in ("平台公告", "治理公告", "规则公告", "用户应举报")) else DEFENSIVE_NOTICE
            return RiskPolarityDecision(
                topic_risk=bool(topic_hits),
                actor_intent=actor,
                polarity=NEGATIVE_RISK_ASSERTION,
                confidence=_confidence(defensive_hits, negation_hits, research_hits),
                evidence=evidence,
            )
        if negation_hits and not self._has_unnegated_solicitation(normalized, solicitation_hits):
            return RiskPolarityDecision(
                topic_risk=bool(topic_hits),
                actor_intent=NEGATIVE_RISK_ASSERTION,
                polarity=NEGATIVE_RISK_ASSERTION,
                confidence=_confidence(negation_hits, defensive_hits, research_hits),
                evidence=evidence,
            )
        if research_hits and not self._has_unnegated_solicitation(normalized, solicitation_hits):
            return RiskPolarityDecision(
                topic_risk=bool(topic_hits),
                actor_intent=RESEARCH,
                polarity=NEGATIVE_RISK_ASSERTION,
                confidence=_confidence(research_hits, defensive_hits, negation_hits),
                evidence=evidence,
            )
        if solicitation_hits:
            return RiskPolarityDecision(
                topic_risk=bool(topic_hits),
                actor_intent=RISK_SOLICITATION,
                polarity=RISK_ASSERTION,
                confidence=min(0.94, 0.68 + len(solicitation_hits) * 0.05),
                evidence=evidence,
            )
        return RiskPolarityDecision(
            topic_risk=bool(topic_hits),
            actor_intent=NEUTRAL,
            polarity=NEUTRAL,
            confidence=0.5 if topic_hits else 0.35,
            evidence=evidence,
        )

    def _has_unnegated_solicitation(self, text: str, solicitation_hits: Iterable[str]) -> bool:
        hits = list(solicitation_hits)
        if not hits:
            return False
        if any(marker in text for marker in self.negation_markers):
            risky = [marker for marker in hits if marker not in {"返佣", "垫付", "招募"}]
            return bool(risky)
        return True


def polarity_from_config(config: Mapping[str, Any] | None = None) -> RiskPolarityScorer:
    payload = dict(config or {})
    root = payload.get("context_polarity") if isinstance(payload.get("context_polarity"), Mapping) else payload
    return RiskPolarityScorer(
        defensive_markers=_list(root.get("defensive_markers") if isinstance(root, Mapping) else ()),
        negation_markers=_list(root.get("negation_markers") if isinstance(root, Mapping) else ()),
        research_markers=_list(root.get("research_markers") if isinstance(root, Mapping) else ()),
        solicitation_markers=_list(root.get("solicitation_markers") if isinstance(root, Mapping) else ()),
    )


def _hits(text: str, markers: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [marker for marker in markers if marker and marker.lower() in lowered]


def _confidence(*groups: Iterable[str]) -> float:
    count = sum(len(list(group)) for group in groups)
    return round(min(0.96, 0.82 + count * 0.035), 4)


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if value is None:
        return []
    try:
        return [str(item) for item in value if str(item).strip()]
    except TypeError:
        return [str(value)]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_text(str(value))
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return tuple(output)


__all__ = [
    "NEGATIVE_RISK_ASSERTION",
    "RiskPolarityDecision",
    "RiskPolarityScorer",
    "polarity_from_config",
]
