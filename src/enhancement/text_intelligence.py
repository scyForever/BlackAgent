"""Advanced cleaning, classification, and entity enrichment."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.cleaner.text_filter import calculate_noise_score, normalize_text, shannon_entropy, text_similarity
from src.classifier.nlp_rule_matcher import (
    ACCOUNT_TRADING,
    CLICK_FARMING,
    CROWD_SERVICE,
    FRAUD_TRAFFIC,
    NORMAL_NOISE,
    TOOL_TRADING,
    UNKNOWN,
    RuleFastTrackClassifier,
)
from src.collector.base_collector import get_record_field
from src.extractor.entity_extractor import ACCOUNT, CONTACT, TOOL_NAME, URL, BasicEntityExtractor
from src.rules import RuleRegistry


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
            "私域导流": ("私聊", "进群", "开户链接", "引流", "导流", "私域", "加v", "加微", "落地页"),
            "拉群语义": ("拉群", "群聊", "群里", "交流群", "邀请", "扣1"),
            "打粉引流": ("打粉", "全品类粉", "粉价", "超链", "分流链接", "回复情况"),
        },
        ACCOUNT_TRADING: {
            "接码注册": ("接码", "验证码", "短信验证码", "批量注册", "虚拟号码", "云短信", "实卡"),
            "实名账号买卖": ("实名号", "卖号", "收号", "老号", "白号", "成品号", "出号", "号商", "实名认证", "verified account", "二要素"),
            "账号养号": ("养号", "权重号", "资料号", "飞机号", "电报号"),
        },
        TOOL_TRADING: {
            "群控脚本": (
                "群控",
                "云控",
                "脚本",
                "协议号",
                "自动化工具",
                "软件",
                "机器人",
                "系统",
                "功能",
                "更新",
                "教程",
                "版本",
                "拉群端",
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
            "改机外挂": ("改机", "外挂", "设备指纹"),
            "卡密交易": ("卡密", "授权码", "激活码"),
        },
        CROWD_SERVICE: {
            "拉群获客": (
                "拉人",
                "拉群",
                "进群",
                "入群",
                "指定群",
                "拉满",
                "保开群",
                "偷人",
                "邀请",
                "成群",
                "手机号拉人",
                "批量邀请",
                "加群",
                "机房",
                "秒出",
                "普群",
            ),
            "打粉卖量": (
                "打粉",
                "活粉",
                "活人粉",
                "僵尸粉",
                "克隆粉",
                "粉价",
                "全品类粉",
                "爆粉",
                "秒罐",
                "卖量",
                "刷阅读量",
                "指定群活人",
                "进群人数",
                "筛活",
                "接粉",
            ),
            "代投服务": (
                "群发",
                "私信",
                "代发",
                "广告",
                "投放",
                "代投",
                "seo",
                "排名",
                "首页展示",
                "直通车",
                "推广",
                "引流",
                "导流",
                "关键词监听",
                "监控关键词",
                "采集群成员",
                "群成员采集",
                "回执",
                "成功率",
                "订单",
                "按钮",
                "图文",
                "文案",
                "链接",
                "接单",
                "业务",
                "客户",
                "对接",
                "担保",
                "包量",
            ),
            "代运营": ("代运营", "矩阵", "运营", "托管", "分成", "转化", "涨粉", "获客"),
            "代投放": ("代投", "投放", "seo", "排名", "首页展示", "直通车", "广告", "群广告", "推广"),
        },
        CLICK_FARMING: {
            "刷单返佣": ("刷单", "补单", "返佣"),
            "点赞关注任务": ("点赞任务", "关注任务", "做任务"),
            "垫付兼职": ("垫付", "日结", "兼职"),
            "订单卡单": ("卡单", "支付失败", "支付通道", "下单", "订单", "发货", "补发", "退款", "售后", "订单号"),
            "卡单玩法": ("卡单", "游戏", "单人局", "战局", "卖金", "文件", "paypal", "steam", "模组", "封号"),
            "手工做单": ("手工单", "做单", "平台单", "线下订单", "打单"),
        },
    }

    CATEGORY_KEYWORDS = RuleFastTrackClassifier.CATEGORY_KEYWORDS
    THEME_PRIORS = RuleFastTrackClassifier.THEME_PRIORS
    REVIEW_ONLY_CATEGORIES = {CROWD_SERVICE}
    REVIEW_ONLY_SECONDARY_LABELS = {"拉群语义", "打粉引流", "订单卡单", "卡单玩法", "手工做单"}
    CROWD_PROMOTION_MARKERS = ("业务联系", "长期合作", "老板", "对接", "担保", "测试联系", "低价", "价格", "售后", "方案", "客服", "咨询", "量大", "包量", "优惠")
    TOOL_PROMOTION_MARKERS = ("拉群端", "开控", "配置", "后台", "session", "自动注册", "官方软件", "启动", "更新内容", "粉丝列表")
    TOOL_UPDATE_MARKERS = ("更新", "版本", "功能", "教程", "软件", "下载", "新增", "修复", "操作", "文档", "演示视频", "停用")
    CLICK_PROMOTION_MARKERS = ("卡单", "支付失败", "支付通道", "下单", "订单", "发货", "补发", "退款", "售后", "手工单", "做单")
    CLICK_CORE_MARKERS = ("刷单", "补单", "垫付", "返佣", "做单", "手工单", "卡单")
    DEFENSIVE_CONTEXT_MARKERS = (
        "反诈",
        "反诈提醒",
        "安全研究",
        "研究复盘",
        "治理复盘",
        "黑产治理",
        "警方发布",
        "警方通报",
        "公安通报",
        "新闻曝光",
        "曝光",
        "安全通告",
        "不提供",
        "不要参与",
        "切勿参与",
        "案例复盘",
    )
    SOLICITATION_MARKERS = (
        "出售",
        "出号",
        "卖号",
        "收号",
        "上车",
        "招募",
        "接单",
        "联系",
        "客服",
        "低价",
        "价格",
        "报价",
        "接洽",
        "合作",
        "代发",
        "代投",
        "包量",
        "秒出",
    )
    CATEGORY_PRIORITY = {
        CROWD_SERVICE: 5,
        TOOL_TRADING: 4,
        ACCOUNT_TRADING: 3,
        FRAUD_TRAFFIC: 2,
        CLICK_FARMING: 1,
        UNKNOWN: 0,
    }

    def __init__(self, rule_registry: RuleRegistry | None = None) -> None:
        self.rule_registry = rule_registry or RuleRegistry()
        self.fast_classifier = RuleFastTrackClassifier()
        polarity = self.rule_registry.load_context_polarity()
        configured_markers = tuple(str(item) for item in polarity.get("defensive_markers", []) if str(item).strip())
        if configured_markers:
            self.defensive_context_markers = tuple(dict.fromkeys([*self.DEFENSIVE_CONTEXT_MARKERS, *configured_markers]))
        else:
            self.defensive_context_markers = self.DEFENSIVE_CONTEXT_MARKERS
        self.rule_version = self.rule_registry.version_hash()

    def classify(self, record: Mapping[str, Any] | Any) -> FineClassificationResult:
        text = _text(record)
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "unknown")
        matched_keywords = self._signal_terms(record, "matched_keywords")
        matched_themes = self._signal_terms(record, "matched_themes")
        fast = self.fast_classifier.classify(record)
        fast_data = fast.model_dump() if hasattr(fast, "model_dump") else dict(fast)
        category_scores, category_evidence, theme_only_scores = self._category_scores(text, matched_keywords, matched_themes)

        if not category_scores:
            return FineClassificationResult(trace_id, UNKNOWN, "待研判", 0.35, True, "UNKNOWN", [], [])
        if self._is_defensive_context(text):
            return FineClassificationResult(
                trace_id,
                UNKNOWN,
                "防御语境",
                0.12,
                False,
                "DEFENSIVE_CONTEXT",
                [],
                ["defensive_context"],
            )

        ordered = sorted(
            category_scores.items(),
            key=lambda item: (-item[1], -self.CATEGORY_PRIORITY.get(item[0], 0), item[0]),
        )
        top_category, top_score = ordered[0]
        conflicts = [category for category, score in ordered[1:] if score == top_score or (top_score - score <= 1 and score >= 2)]
        conflict_status = "RESOLVED"
        secondary_label, secondary_evidence = self._secondary_label(top_category, text, matched_keywords)
        supporting_evidence = self._ordered_unique((*category_evidence.get(top_category, []), *secondary_evidence))

        confidence = max(
            float(fast_data.get("confidence", 0.0) or 0.0),
            min(0.96, 0.56 + top_score * 0.07 + len(secondary_evidence) * 0.03),
        )
        review_required = bool(fast_data.get("review_required", False))
        if top_category in self.REVIEW_ONLY_CATEGORIES:
            review_required = True
        if theme_only_scores.get(top_category, False):
            review_required = True
            confidence = min(confidence, 0.72)
        if secondary_label in {"未细分", "待研判"}:
            review_required = True
        if secondary_label in self.REVIEW_ONLY_SECONDARY_LABELS:
            review_required = True
            confidence = min(confidence, 0.78)
        if conflicts:
            conflict_status = "CONFLICT_REVIEW"
            review_required = True
            confidence = min(confidence, 0.74)

        return FineClassificationResult(
            source_trace_id=trace_id,
            risk_category=top_category,
            secondary_label=secondary_label,
            confidence=round(confidence, 4),
            review_required=review_required,
            conflict_status=conflict_status,
            conflict_categories=conflicts,
            evidence=supporting_evidence,
        )

    def _is_defensive_context(self, text: str) -> bool:
        defensive_hits = self._marker_hits(text, self.defensive_context_markers)
        if not defensive_hits:
            return False
        solicitation_hits = self._marker_hits(text, self.SOLICITATION_MARKERS)
        return len(solicitation_hits) == 0 or any(marker in text for marker in ("不提供", "不要参与", "切勿参与"))

    def _signal_terms(self, record: Mapping[str, Any] | Any, field_name: str) -> tuple[str, ...]:
        values = get_record_field(record, field_name) or ()
        if isinstance(values, str):
            values = [values]
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values if isinstance(values, Iterable) and not isinstance(values, (str, bytes)) else ():
            value = normalize_text(str(raw))
            if not value:
                continue
            lowered = value.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(value)
        return tuple(normalized)

    def _category_scores(
        self,
        text: str,
        matched_keywords: tuple[str, ...],
        matched_themes: tuple[str, ...],
    ) -> tuple[dict[str, int], dict[str, list[str]], dict[str, bool]]:
        score_map: dict[str, int] = {}
        evidence_map: dict[str, list[str]] = defaultdict(list)
        matched_keyword_set = {value.lower() for value in matched_keywords}

        for category, keywords in self.CATEGORY_KEYWORDS.items():
            hits = [
                keyword
                for keyword in keywords
                if keyword.lower() in text.lower() or normalize_text(keyword).lower() in matched_keyword_set
            ]
            if not hits:
                continue
            unique_hits = self._ordered_unique(hits)
            score_map[category] = score_map.get(category, 0) + len(unique_hits)
            evidence_map[category].extend(unique_hits)

        for theme in matched_themes:
            mapped = self.THEME_PRIORS.get(theme)
            if mapped is None:
                continue
            category, bonus = mapped
            score_map[category] = score_map.get(category, 0) + bonus
            evidence_map[category].append(f"theme:{theme}")

        crowd_markers = self._marker_hits(text, self.CROWD_PROMOTION_MARKERS)
        if crowd_markers and self._matches_any(text, matched_keyword_set, self.SECONDARY_RULES[CROWD_SERVICE]["拉群获客"]):
            score_map[CROWD_SERVICE] = score_map.get(CROWD_SERVICE, 0) + min(2, len(crowd_markers))
            evidence_map[CROWD_SERVICE].extend(f"service:{marker}" for marker in crowd_markers[:2])

        tool_markers = self._marker_hits(text, self.TOOL_PROMOTION_MARKERS)
        if tool_markers:
            score_map[TOOL_TRADING] = score_map.get(TOOL_TRADING, 0) + min(3, len(tool_markers))
            evidence_map[TOOL_TRADING].extend(f"tool:{marker}" for marker in tool_markers[:3])

        tool_update_markers = self._marker_hits(text, self.TOOL_UPDATE_MARKERS)
        if len(tool_update_markers) >= 2:
            score_map[TOOL_TRADING] = score_map.get(TOOL_TRADING, 0) + 2
            evidence_map[TOOL_TRADING].extend(f"tool_update:{marker}" for marker in tool_update_markers[:2])

        click_markers = self._marker_hits(text, self.CLICK_PROMOTION_MARKERS)
        if click_markers and ("卡单" in text or "手工单" in text or "做单" in text):
            score_map[CLICK_FARMING] = score_map.get(CLICK_FARMING, 0) + min(2, len(click_markers))
            evidence_map[CLICK_FARMING].extend(f"order:{marker}" for marker in click_markers[:2])

        click_core_markers = self._marker_hits(text, self.CLICK_CORE_MARKERS)
        if click_core_markers:
            score_map[CLICK_FARMING] = score_map.get(CLICK_FARMING, 0) + min(2, len(click_core_markers))
            evidence_map[CLICK_FARMING].extend(f"click:{marker}" for marker in click_core_markers[:2])

        theme_only_scores = {
            category: all(item.startswith("theme:") for item in evidence_map.get(category, []))
            for category in score_map
        }
        return score_map, {key: self._ordered_unique(value) for key, value in evidence_map.items()}, theme_only_scores

    def _secondary_label(self, category: str, text: str, matched_keywords: tuple[str, ...]) -> tuple[str, list[str]]:
        candidates = []
        matched_keyword_set = {value.lower() for value in matched_keywords}
        for label, keywords in self.SECONDARY_RULES.get(category, {}).items():
            hits = [
                keyword
                for keyword in keywords
                if keyword.lower() in text.lower() or normalize_text(keyword).lower() in matched_keyword_set
            ]
            if hits:
                candidates.append((label, hits))
        if not candidates:
            return "未细分", []
        label, hits = max(candidates, key=lambda item: len(item[1]))
        return label, self._ordered_unique(hits)

    def _marker_hits(self, text: str, markers: Iterable[str]) -> list[str]:
        lowered_text = text.lower()
        return [marker for marker in markers if marker.lower() in lowered_text]

    def _matches_any(self, text: str, matched_keyword_set: set[str], keywords: Iterable[str]) -> bool:
        lowered_text = text.lower()
        return any(keyword.lower() in lowered_text or normalize_text(keyword).lower() in matched_keyword_set for keyword in keywords)

    def _ordered_unique(self, values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            normalized = normalize_text(str(value))
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(normalized)
        return ordered


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
        "🎵": "抖音",
        "抖": "抖音",
        "dy": "抖音",
        "飞机": "Telegram",
        "纸飞机": "Telegram",
        "小飞机": "Telegram",
        "✈": "Telegram",
        "✈️": "Telegram",
        "🛩": "Telegram",
        "🛩️": "Telegram",
        "企鹅": "QQ",
        "🐧": "QQ",
        "料子": "账号资料",
        "车队": "任务团伙",
        "上车": "加入任务",
        "水房": "洗钱结算",
        "加薇": "加v",
        "加威": "加v",
        "加围": "加v",
        "➕v": "加v",
        "➕V": "加v",
    }

    def __init__(self, initial_terms: Mapping[str, str] | None = None, rule_registry: RuleRegistry | None = None) -> None:
        self.rule_registry = rule_registry or RuleRegistry()
        self._terms = dict(self.DEFAULT)
        self._terms.update(self.rule_registry.load_slang_dictionary())
        if initial_terms:
            self._terms.update({str(k): str(v) for k, v in initial_terms.items()})
        self.rule_version = self.rule_registry.version_hash()

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

def context_relevance(text: str, start: int, end: int) -> float:
    window = text[max(0, start - 18) : min(len(text), end + 18)]
    markers = ("出售", "招募", "接码", "群控", "跑分", "引流", "刷单", "代付", "暗号", "联系", "上车")
    hits = sum(1 for marker in markers if marker in window)
    return round(min(1.0, 0.35 + hits * 0.15), 4)


def _normalize_obfuscation(value: str) -> str:
    normalized = normalize_text(value)
    normalized = normalized.replace("hxxp://", "http://").replace("hxxps://", "https://")
    normalized = normalized.replace("[.]", ".").replace("【.】", ".").replace("(.)", ".")
    normalized = normalized.replace("➕", "加").replace("＋", "加").replace("✈️", "飞机").replace("✈", "飞机")
    normalized = normalized.replace("🛩️", "飞机").replace("🛩", "飞机").replace("🛰️", "飞机").replace("🛰", "飞机")
    normalized = normalized.replace("🎵", "音符").replace("🐧", "QQ").replace("纸飞机", "飞机").replace("小飞机", "飞机")
    normalized = re.sub(r"(?i)\bv\s*x\b", "vx", normalized)
    normalized = re.sub(r"(?i)(加|联系|咨询|客服|对接)\s*[vV薇微威围]\b", r"\1v", normalized)
    normalized = normalized.replace("进裙", "进群").replace("拉裙", "拉群")
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
