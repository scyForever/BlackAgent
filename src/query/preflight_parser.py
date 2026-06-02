"""Deterministic preflight parser before optional LLM query parsing."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.cleaner.text_filter import normalize_intel_text, normalize_text
from src.rules import RuleRegistry


@dataclass(frozen=True)
class PreflightIntent:
    risk_types: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    slang_terms: list[str] = field(default_factory=list)
    entity_types: list[str] = field(default_factory=list)
    freshness: str = "recent"
    preferred_source_types: list[str] = field(default_factory=list)
    need_cross_source: bool = False
    confidence: float = 0.0
    needs_llm_parse: bool = False
    reason: str = "rule_preflight"
    time_range_hours: int = 24

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class PreflightQueryParser:
    """Parse cheap, high-signal query fields without calling an LLM."""

    SOURCE_HINTS = {
        "telegram": ("telegram", "tg", "电报", "飞机", "纸飞机"),
        "im": ("im", "群", "群聊", "私聊", "聊天"),
        "forum": ("forum", "论坛", "贴吧", "社区"),
        "social": ("社媒", "社交", "音符", "抖音", "x ", "twitter"),
        "vertical": ("垂直", "技术社区", "站点", "feed", "公开源"),
    }
    ENTITY_HINTS = {
        "contact": ("tg", "telegram", "@", "微信", "vx", "qq", "联系", "客服"),
        "url": ("http", "hxxp", "链接", "落地页", "域名"),
        "tool_name": ("群控", "云控", "脚本", "卡密", "外挂", "接码平台"),
        "account": ("账号", "卖号", "收号", "白号", "实名号", "邀请码", "口令"),
        "settlement": ("跑分", "代付", "usdt", "银行卡", "支付宝"),
    }
    COMPLEX_HINTS = (
        "比较",
        "对比",
        "归因",
        "多轮",
        "策略",
        "计划",
        "只走",
        "只看",
        "禁用",
        "不要",
        "强制",
        "复杂",
        "解释为什么",
    )

    def __init__(self, rule_registry: RuleRegistry | None = None) -> None:
        self.rule_registry = rule_registry or RuleRegistry()
        self.risk_terms = self.rule_registry.primary_terms_by_label()
        self.secondary_rules = self.rule_registry.secondary_rules()
        self.slang_dictionary = self.rule_registry.load_slang_dictionary()

    def parse(self, query: str, *, runtime_context: Mapping[str, Any] | None = None) -> PreflightIntent:
        raw = normalize_text(query)
        normalized = normalize_intel_text(raw)
        lowered = normalized.lower()
        runtime_context = dict(runtime_context or {})

        risk_types, risk_hits = self._risk_hits(normalized)
        runtime_slang = self._runtime_slang_hits(raw, runtime_context=runtime_context)
        configured_slang = [
            f"{raw_term}->{target}"
            for raw_term, target in self.slang_dictionary.items()
            if raw_term and (raw_term in raw or raw_term.lower() in lowered)
        ]
        slang_terms = _dedupe([*configured_slang, *runtime_slang])
        keywords = _dedupe([*risk_hits, *[item.split("->", 1)[-1] for item in slang_terms]])
        entity_types = self._entity_types(lowered)
        preferred_sources = self._preferred_sources(lowered)
        time_range_hours = _extract_time_range_hours(raw)
        freshness = "recent" if time_range_hours <= 48 else f"last_{time_range_hours}h"
        need_cross_source = any(term in raw for term in ("跨源", "多源", "关联", "图谱"))
        needs_llm_parse = self._needs_llm_parse(raw, runtime_context=runtime_context, has_risk=bool(risk_types or slang_terms))
        confidence = self._confidence(risk_types, keywords, entity_types, preferred_sources, needs_llm_parse)
        return PreflightIntent(
            risk_types=risk_types or ["黑灰产情报"],
            keywords=keywords,
            slang_terms=slang_terms,
            entity_types=entity_types,
            freshness=freshness,
            preferred_source_types=preferred_sources or ["telegram", "forum", "im"],
            need_cross_source=need_cross_source,
            confidence=confidence,
            needs_llm_parse=needs_llm_parse,
            reason="complex_query_needs_llm_parse" if needs_llm_parse else "rule_preflight_confident",
            time_range_hours=time_range_hours,
        )

    def _risk_hits(self, text: str) -> tuple[list[str], list[str]]:
        risks: list[str] = []
        hits: list[str] = []
        lowered = text.lower()
        for category, terms in self.risk_terms.items():
            matched = [term for term in terms if term and term.lower() in lowered]
            if matched:
                risks.append(category)
                hits.extend(matched)
        for category, rules in self.secondary_rules.items():
            for label, terms in rules.items():
                matched = [term for term in terms if term and term.lower() in lowered]
                if matched:
                    risks.append(category)
                    hits.extend([label, *matched])
        return _dedupe(risks), _dedupe(hits)

    def _runtime_slang_hits(self, query: str, *, runtime_context: Mapping[str, Any]) -> list[str]:
        hits: list[str] = []
        lowered = query.lower()
        for item in runtime_context.get("slang_terms", []) if isinstance(runtime_context.get("slang_terms"), list) else []:
            if not isinstance(item, Mapping):
                continue
            raw = str(item.get("term") or item.get("raw") or "").strip()
            target = str(item.get("normalized_term") or item.get("target") or "").strip()
            if raw and (raw in query or raw.lower() in lowered):
                hits.append(f"{raw}->{target}" if target else raw)
            elif target and target.lower() in lowered:
                hits.append(f"{raw}->{target}" if raw else target)
        return hits

    def _entity_types(self, lowered_query: str) -> list[str]:
        return _dedupe(
            entity_type
            for entity_type, hints in self.ENTITY_HINTS.items()
            if any(hint.lower() in lowered_query for hint in hints)
        )

    def _preferred_sources(self, lowered_query: str) -> list[str]:
        return _dedupe(
            source_type
            for source_type, hints in self.SOURCE_HINTS.items()
            if any(hint.lower() in lowered_query for hint in hints)
        )

    def _needs_llm_parse(self, query: str, *, runtime_context: Mapping[str, Any], has_risk: bool) -> bool:
        if bool(runtime_context.get("force_llm_intent_parse")):
            return True
        lowered = query.lower()
        if len(query) > 80:
            return True
        if sum(query.count(token) for token in ("，", "；", ";", "\n")) >= 2:
            return True
        if any(hint in query or hint in lowered for hint in self.COMPLEX_HINTS):
            return True
        return not has_risk

    @staticmethod
    def _confidence(
        risk_types: list[str],
        keywords: list[str],
        entity_types: list[str],
        preferred_sources: list[str],
        needs_llm_parse: bool,
    ) -> float:
        score = 0.25 + 0.15 * min(len(risk_types), 2) + 0.08 * min(len(keywords), 4) + 0.08 * min(len(entity_types), 2)
        if preferred_sources:
            score += 0.08
        if needs_llm_parse:
            score -= 0.18
        return round(max(0.0, min(0.95, score)), 4)


def _extract_time_range_hours(query: str) -> int:
    normalized = str(query or "").lower()
    match = re.search(r"(?:近|最近)?\s*(\d+)\s*(小时|小時|h|天|日|day|days)", normalized, flags=re.IGNORECASE)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        return amount * 24 if unit in {"天", "日", "day", "days"} else amount
    if "48小时" in query or "48h" in normalized:
        return 48
    if "72小时" in query or "72h" in normalized:
        return 72
    if "当天" in query or "今日" in query or "今天" in query:
        return 24
    return 24


def _dedupe(items: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = normalize_text(str(item or ""))
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(value)
    return output


__all__ = ["PreflightIntent", "PreflightQueryParser"]
