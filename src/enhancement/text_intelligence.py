"""Advanced cleaning, classification, and entity enrichment."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.cleaner.text_filter import calculate_noise_score, normalize_text, text_similarity
from src.classifier.nlp_rule_matcher import (
    ACCOUNT_TRADING,
    CLICK_FARMING,
    FRAUD_TRAFFIC,
    NORMAL_NOISE,
    TOOL_TRADING,
    UNKNOWN,
    RuleFastTrackClassifier,
)
from src.collector.base_collector import get_record_field
from src.extractor.entity_extractor import ACCOUNT, CONTACT, TOOL_NAME, URL, BasicEntityExtractor


@dataclass(frozen=True)
class EntropyDecision:
    source_trace_id: str
    action: str
    entropy: float
    noise_score: float
    reason: str

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class AdaptiveEntropyFilter:
    """Phase III dynamic entropy noise filter.

    It drops extremely low-information or symbol-heavy records while retaining
    short but meaningful Chinese risk snippets.
    """

    def __init__(self, *, min_entropy: float = 1.0, max_noise_score: float = 0.82) -> None:
        self.min_entropy = min_entropy
        self.max_noise_score = max_noise_score

    def evaluate(self, record: Mapping[str, Any] | Any) -> EntropyDecision:
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "unknown")
        text = normalize_text(str(get_record_field(record, "clean_text") or get_record_field(record, "content_text") or record))
        entropy = shannon_entropy(text)
        noise = calculate_noise_score(text)
        if not text:
            return EntropyDecision(trace_id, "DROP", entropy, noise, "empty_text")
        if entropy < self.min_entropy and len(text) >= 8:
            return EntropyDecision(trace_id, "DROP", entropy, noise, "low_information_entropy")
        if noise > self.max_noise_score:
            return EntropyDecision(trace_id, "DROP", entropy, noise, "high_noise_score")
        return EntropyDecision(trace_id, "KEEP", entropy, noise, "signal_preserved")


@dataclass(frozen=True)
class SimilarityCluster:
    cluster_id: str
    trace_ids: list[str]
    representative_text: str
    average_similarity: float

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class SimilarityClusterer:
    """Phase II near-duplicate / template clusterer."""

    def __init__(self, *, threshold: float = 0.82) -> None:
        self.threshold = threshold

    def cluster(self, records: Iterable[Mapping[str, Any] | Any]) -> list[SimilarityCluster]:
        clusters: list[list[Mapping[str, Any] | Any]] = []
        representatives: list[str] = []
        for record in records:
            text = _text(record)
            if not text:
                continue
            placed = False
            for index, representative in enumerate(representatives):
                if text_similarity(text, representative) >= self.threshold:
                    clusters[index].append(record)
                    placed = True
                    break
            if not placed:
                clusters.append([record])
                representatives.append(text)

        results: list[SimilarityCluster] = []
        for index, cluster in enumerate(clusters, start=1):
            rep = representatives[index - 1]
            trace_ids = [str(get_record_field(item, "source_trace_id") or get_record_field(item, "trace_id") or index) for item in cluster]
            scores = [text_similarity(_text(item), rep) for item in cluster]
            results.append(
                SimilarityCluster(
                    cluster_id=f"template_cluster_{index}",
                    trace_ids=trace_ids,
                    representative_text=rep,
                    average_similarity=round(sum(scores) / len(scores), 4) if scores else 0.0,
                )
            )
        return results


@dataclass(frozen=True)
class FineClassificationResult:
    source_trace_id: str
    risk_category: str
    secondary_label: str
    confidence: float
    review_required: bool
    conflict_status: str = "RESOLVED"
    conflict_categories: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    classifier_version: str = "fine_grained_v2_conflict_v3"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class FineGrainedIntentClassifier:
    """Phase II second-level classifier plus Phase III conflict resolver."""

    SECONDARY_RULES: dict[str, dict[str, tuple[str, ...]]] = {
        FRAUD_TRAFFIC: {
            "返利引流": ("返利", "高佣", "拉新"),
            "跑分代付": ("跑分", "代付", "刷流水"),
            "私域导流": ("私聊", "进群", "开户链接", "引流"),
        },
        ACCOUNT_TRADING: {
            "接码注册": ("接码", "验证码", "批量注册"),
            "实名账号买卖": ("实名号", "卖号", "收号", "老号", "白号"),
            "账号养号": ("养号", "权重号", "资料号"),
        },
        TOOL_TRADING: {
            "群控脚本": ("群控", "脚本", "协议号", "自动化工具"),
            "改机外挂": ("改机", "外挂", "设备指纹"),
            "卡密交易": ("卡密", "授权码", "激活码"),
        },
        CLICK_FARMING: {
            "刷单返佣": ("刷单", "补单", "返佣"),
            "点赞关注任务": ("点赞任务", "关注任务", "做任务"),
            "垫付兼职": ("垫付", "日结", "兼职"),
        },
    }

    CATEGORY_KEYWORDS = RuleFastTrackClassifier.CATEGORY_KEYWORDS

    def __init__(self) -> None:
        self.fast_classifier = RuleFastTrackClassifier()

    def classify(self, record: Mapping[str, Any] | Any) -> FineClassificationResult:
        text = _text(record)
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "unknown")
        fast = self.fast_classifier.classify(record)
        fast_data = fast.model_dump() if hasattr(fast, "model_dump") else dict(fast)
        category_scores: dict[str, int] = {}
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            hits = [keyword for keyword in keywords if keyword in text]
            if hits:
                category_scores[category] = len(hits)

        if not category_scores:
            return FineClassificationResult(trace_id, UNKNOWN, "待研判", 0.35, True, "UNKNOWN", [], [])

        ordered = sorted(category_scores.items(), key=lambda item: (-item[1], item[0]))
        top_category, top_score = ordered[0]
        conflicts = [category for category, score in ordered[1:] if score == top_score or (top_score - score <= 1 and score >= 2)]
        conflict_status = "RESOLVED"
        review_required = bool(fast_data.get("review_required", False))
        confidence = float(fast_data.get("confidence", 0.65) or 0.65)
        if conflicts:
            conflict_status = "CONFLICT_REVIEW"
            review_required = True
            confidence = min(confidence, 0.74)

        secondary_label, evidence = self._secondary_label(top_category, text)
        return FineClassificationResult(
            source_trace_id=trace_id,
            risk_category=top_category,
            secondary_label=secondary_label,
            confidence=round(confidence, 4),
            review_required=review_required,
            conflict_status=conflict_status,
            conflict_categories=conflicts,
            evidence=evidence,
        )

    def _secondary_label(self, category: str, text: str) -> tuple[str, list[str]]:
        candidates = []
        for label, keywords in self.SECONDARY_RULES.get(category, {}).items():
            hits = [keyword for keyword in keywords if keyword in text]
            if hits:
                candidates.append((label, hits))
        if not candidates:
            return "未细分", []
        label, hits = max(candidates, key=lambda item: len(item[1]))
        return label, hits


@dataclass(frozen=True)
class AdvancedEntity:
    entity_type: str
    entity_value: str
    normalized_value: str
    start_offset: int
    end_offset: int
    source_trace_id: str
    confidence: float = 1.0
    context_relevance: float = 0.5
    extraction_method: str = "advanced_rule_v2"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class SlangDictionary:
    """Dynamic slang normalization dictionary."""

    DEFAULT = {
        "音符": "抖音",
        "抖": "抖音",
        "dy": "抖音",
        "飞机": "Telegram",
        "料子": "账号资料",
        "车队": "任务团伙",
        "上车": "加入任务",
        "水房": "洗钱结算",
    }

    def __init__(self, initial_terms: Mapping[str, str] | None = None) -> None:
        self._terms = dict(self.DEFAULT)
        if initial_terms:
            self._terms.update({str(k): str(v) for k, v in initial_terms.items()})

    def normalize(self, value: str) -> str:
        lowered = value.lower()
        for raw, target in self._terms.items():
            if raw.lower() == lowered:
                return target
        return value

    def candidates_in_text(self, text: str) -> list[tuple[str, str, int, int]]:
        results: list[tuple[str, str, int, int]] = []
        lowered = text.lower()
        for raw, target in self._terms.items():
            start = lowered.find(raw.lower())
            while start >= 0:
                results.append((raw, target, start, start + len(raw)))
                start = lowered.find(raw.lower(), start + len(raw))
        return results


class AdvancedEntityExtractor:
    """Phase II normalization + Phase III hidden entity discovery."""

    OBFUSCATED_URL_RE = re.compile(r"(?i)(?:hxxps?|https?)[:：]//[^\s]+|[a-z0-9-]+\s*\[\.\]\s*(?:com|cn|net|top|xyz)(?:/[^\s]*)?")
    INVITE_CODE_RE = re.compile(r"(?:邀请码|暗号|口令|code)[:：\s]*([A-Za-z0-9_-]{4,24})", re.IGNORECASE)
    SETTLEMENT_RE = re.compile(r"(跑分|代付|虚拟币|USDT|银行卡|支付宝|微信收款)", re.IGNORECASE)

    def __init__(self, slang_dictionary: SlangDictionary | None = None) -> None:
        self.basic = BasicEntityExtractor()
        self.slang_dictionary = slang_dictionary or SlangDictionary()

    def extract(self, record: Mapping[str, Any] | Any) -> list[AdvancedEntity]:
        text = _text(record)
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "unknown")
        entities: list[AdvancedEntity] = []
        seen: set[tuple[str, str, int, int]] = set()

        def add(entity_type: str, value: str, start: int, end: int, *, method: str = "advanced_rule_v2", confidence: float = 1.0) -> None:
            normalized = self.slang_dictionary.normalize(_normalize_obfuscation(value))
            key = (entity_type, normalized, start, end)
            if key in seen or not normalized:
                return
            seen.add(key)
            entities.append(
                AdvancedEntity(
                    entity_type=entity_type,
                    entity_value=value.strip(),
                    normalized_value=normalized,
                    start_offset=start,
                    end_offset=end,
                    source_trace_id=trace_id,
                    confidence=confidence,
                    context_relevance=context_relevance(text, start, end),
                    extraction_method=method,
                )
            )

        for basic_entity in self.basic.extract(record):
            data = basic_entity.model_dump() if hasattr(basic_entity, "model_dump") else dict(basic_entity)
            add(data["entity_type"], data["entity_value"], int(data["start_offset"]), int(data["end_offset"]), method="basic_plus_normalized")

        for raw, _target, start, end in self.slang_dictionary.candidates_in_text(text):
            add("slang_term", raw, start, end, method="slang_dictionary", confidence=0.88)
        for regex, entity_type, method in (
            (self.OBFUSCATED_URL_RE, URL, "hidden_obfuscated_url"),
            (self.INVITE_CODE_RE, ACCOUNT, "hidden_invite_code"),
            (self.SETTLEMENT_RE, "settlement", "hidden_settlement_term"),
        ):
            for match in regex.finditer(text):
                value = match.group(1) if match.groups() and match.group(1) else match.group(0)
                start = match.start(1) if match.groups() and match.group(1) else match.start()
                add(entity_type, value, start, start + len(value), method=method, confidence=0.82)
        return sorted(entities, key=lambda item: (item.source_trace_id, item.start_offset, item.entity_type))


def shannon_entropy(text: str) -> float:
    normalized = normalize_text(text)
    if not normalized:
        return 0.0
    counts = Counter(char for char in normalized if not char.isspace())
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return round(-sum((count / total) * math.log2(count / total) for count in counts.values()), 4)


def context_relevance(text: str, start: int, end: int) -> float:
    window = text[max(0, start - 18) : min(len(text), end + 18)]
    markers = ("出售", "招募", "接码", "群控", "跑分", "引流", "刷单", "代付", "暗号", "联系", "上车")
    hits = sum(1 for marker in markers if marker in window)
    return round(min(1.0, 0.35 + hits * 0.15), 4)


def _normalize_obfuscation(value: str) -> str:
    normalized = normalize_text(value)
    normalized = normalized.replace("hxxp://", "http://").replace("hxxps://", "https://")
    normalized = normalized.replace("[.]", ".").replace("【.】", ".").replace("(.)", ".")
    normalized = re.sub(r"\s+", "", normalized) if "[.]" in value or "【.】" in value else normalized
    return normalized.strip(" ,，。;；")


def _text(record: Mapping[str, Any] | Any) -> str:
    return normalize_text(str(get_record_field(record, "clean_text") or get_record_field(record, "content_text") or get_record_field(record, "text") or record))


__all__ = [
    "AdaptiveEntropyFilter",
    "AdvancedEntity",
    "AdvancedEntityExtractor",
    "EntropyDecision",
    "FineClassificationResult",
    "FineGrainedIntentClassifier",
    "SimilarityCluster",
    "SimilarityClusterer",
    "SlangDictionary",
    "context_relevance",
    "shannon_entropy",
]
