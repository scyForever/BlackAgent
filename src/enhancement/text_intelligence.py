"""Advanced cleaning, classification, and entity enrichment."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.cleaner.text_filter import calculate_noise_score, normalize_intel_text, normalize_text, shannon_entropy, text_similarity
from src.classifier.nlp_rule_matcher import (
    ACCOUNT_TRADING,
    CLICK_FARMING,
    CROWD_SERVICE,
    FRAUD_TRAFFIC,
    NORMAL_NOISE,
    REVIEW_BUCKET_HUMAN_REVIEW_REQUIRED,
    REVIEW_BUCKET_LOW_RELEVANCE,
    TOOL_TRADING,
    UNKNOWN,
    RuleFastTrackClassifier,
    review_bucket_for_classification,
)
from src.collector.base_collector import get_record_field
from src.extractor.entity_extractor import ACCOUNT, CONTACT, TOOL_NAME, URL, BasicEntityExtractor
from src.enhancement.context_polarity import NEGATIVE_RISK_ASSERTION, polarity_from_config
from src.intelligence.entity_normalizer import EntityNormalizer
from src.intelligence.entity_postprocessor import filter_and_order_entities
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
    review_bucket: str = REVIEW_BUCKET_HUMAN_REVIEW_REQUIRED
    final_secondary_label: str | None = None
    candidate_secondary_labels: list[dict[str, Any]] = field(default_factory=list)
    conflict_status: str = "RESOLVED"
    conflict_categories: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    review_decision_reason: str = "default_review_policy"
    classifier_version: str = "fine_grained_v2_conflict_v4"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SlangVariantCandidate:
    raw: str
    normalized: str
    entity_type: str
    start_offset: int
    end_offset: int
    category_hint: str | None = None
    context_confirmed: bool = False
    context_hits: list[str] = field(default_factory=list)
    confidence: float = 0.78
    method: str = "slang_variant_normalizer_v1"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SlangVariantAnalysis:
    original_text: str
    normalized_text: str
    expanded_text: str
    candidates: list[SlangVariantCandidate] = field(default_factory=list)
    context_hits: list[str] = field(default_factory=list)

    @property
    def confirmed_candidates(self) -> list[SlangVariantCandidate]:
        return [item for item in self.candidates if item.context_confirmed]

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class SlangVariantNormalizer:
    """Normalize black/gray slang variants only when context supports risk use.

    The normalizer is intentionally deterministic and local: it does not mark a
    record risky merely because a slang token appears.  A token becomes a
    classification hint only when trading/recruiting/contact/url context is
    present around the same text.
    """

    CONTEXT_MARKERS = (
        "出售",
        "卖",
        "买",
        "接单",
        "招募",
        "上车",
        "拉群",
        "进群",
        "私聊",
        "联系",
        "客服",
        "咨询",
        "详聊",
        "暗号",
        "口令",
        "邀请码",
        "code:",
        "tg:",
        "telegram",
        "@",
        "http://",
        "https://",
        "hxxp://",
        "hxxps://",
        "短链",
        "低价",
        "价格",
        "卡密",
    )
    VARIANT_SPECS: tuple[tuple[re.Pattern[str], str, str, str | None], ...] = (
        (re.compile(r"(?i)(?:音\s*符|🎵|\bd\s*y\b)"), "抖音", "slang_term", "诈骗引流"),
        (re.compile(r"(?i)(?:纸\s*飞\s*机|小\s*飞\s*机|飞\s*机|\bt\s*g\b(?!\s*[:：@])|telegram(?!\s*[:：@]))"), "Telegram", "slang_term", None),
        (re.compile(r"(?i)(?:企\s*鹅|🐧|q\s*q)"), "QQ", "slang_term", None),
        (re.compile(r"(?i)(?:\+?\s*v\s*x|[+＋➕]?\s*v\b|加\s*[vV薇微威围]|微\s*信|薇\s*信|威\s*信|围\s*信)"), "加v", "slang_term", "诈骗引流"),
        (re.compile(r"(?i)(?:接\s*[🐴马m]\s*a?|j\s*m|接\s*码)"), "接码", "tool_name", "账号交易"),
        (re.compile(r"(?:裙|羣|q\s*群)"), "群组", "slang_term", "诈骗引流"),
        (re.compile(r"(?:料\s*子|客\s*资|数\s*据|库)"), "账号资料", "slang_term", "账号交易"),
        (re.compile(r"群\s*控"), "群控", "tool_name", "工具交易"),
        (re.compile(r"脚\s*本"), "脚本", "tool_name", "工具交易"),
        (re.compile(r"卡\s*密"), "卡密", "tool_name", "工具交易"),
    )

    def analyze(self, text: str) -> SlangVariantAnalysis:
        original = normalize_text(text)
        normalized = self.normalize_text(original)
        context_hits = _ordered_unique(self._context_hits(normalized))
        candidates: list[SlangVariantCandidate] = []
        occupied: list[range] = []
        for pattern, target, entity_type, category_hint in self.VARIANT_SPECS:
            for match in pattern.finditer(original):
                raw = match.group(0)
                span = range(match.start(), match.end())
                if any(_ranges_overlap(span, used) for used in occupied):
                    continue
                local_hits = _ordered_unique(self._context_hits(original[max(0, match.start() - 24) : match.end() + 24]))
                confirmed = bool(local_hits or context_hits)
                candidates.append(
                    SlangVariantCandidate(
                        raw=raw,
                        normalized=target,
                        entity_type=entity_type,
                        start_offset=match.start(),
                        end_offset=match.end(),
                        category_hint=category_hint,
                        context_confirmed=confirmed,
                        context_hits=local_hits or context_hits,
                        confidence=0.9 if confirmed else 0.76,
                    )
                )
                occupied.append(span)
        expanded_terms = [
            candidate.normalized
            for candidate in candidates
            if candidate.context_confirmed or candidate.entity_type in {"tool_name", "contact"}
        ]
        expanded_text = " ".join(_ordered_unique([normalized, *expanded_terms]))
        return SlangVariantAnalysis(
            original_text=original,
            normalized_text=normalized,
            expanded_text=expanded_text,
            candidates=candidates,
            context_hits=context_hits,
        )

    def normalize_text(self, text: str) -> str:
        normalized = normalize_intel_text(_normalize_obfuscation(text))
        normalized = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", normalized)
        normalized = re.sub(r"(?i)\bt\s+g\b", "TG", normalized)
        normalized = re.sub(r"(?i)\bd\s+y\b", "dy", normalized)
        normalized = re.sub(r"(?i)j\s*m", "jm", normalized)
        return normalize_text(normalized)

    def candidates_in_text(self, text: str) -> list[SlangVariantCandidate]:
        return self.analyze(text).candidates

    def _context_hits(self, text: str) -> list[str]:
        lowered = str(text or "").lower()
        return [marker for marker in self.CONTEXT_MARKERS if marker.lower() in lowered]


class FineGrainedIntentClassifier:
    """Phase II second-level classifier plus Phase III conflict resolver."""

    ORDINARY_PUBLIC_INFO_MARKERS = (
        "article",
        "read more",
        "read full guide",
        "follow us",
        "linkedin",
        "twitter",
        "public",
        "explains",
        "documentation",
        "docs",
        "guide",
        "tutorial",
        "blog",
        "news",
        "release notes",
        "changelog",
        "#update",
        "#fix",
        "知识",
        "科普",
        "文章",
        "新闻",
        "公告",
        "介绍",
        "说明",
        "解读",
        "资料",
    )
    ORDINARY_TECHNICAL_MARKERS = (
        "technology",
        "company",
        "feed mill",
        "grain",
        "agronomy",
        "partial discharge",
        "conditionmonitoring",
        "condition monitoring",
        "electricalengineering",
        "electrical engineering",
        "electrical",
        "engineering",
        "diagnostics",
        "maintenance",
        "equipment",
        "testing",
        "process engineers",
        "pressure gauge",
        "control valve",
        "boiler",
        "automationforum",
        "forumelectrical",
        "电气",
        "工程",
        "自动化",
        "设备",
        "维护",
        "诊断",
        "测试",
    )
    ORDINARY_PUBLIC_SOURCE_MARKERS = (
        "forum",
        "blog",
        "news",
        "vertical",
        "technical",
        "techforum",
        "website",
        "web",
        "automationforum",
        "forumelectrical",
    )
    PUBLIC_SOCIAL_LINK_MARKERS = (
        "follow us",
        "linkedin",
        "twitter",
        "t.me/",
        "read more",
        "read full guide",
        "category/",
        "github",
    )
    ORDINARY_SOFTWARE_UPDATE_MARKERS = (
        "#update",
        "#fix",
        "release notes",
        "changelog",
        "regression",
        "module loading",
        "custom modules",
        "update your userbot",
        "restart your dynos",
        "latest commits",
    )
    TECHNICAL_HOMONYM_RISK_MARKERS = (
        "steam",
        "automation",
    )
    ORDINARY_STEAM_DISCUSSION_MARKERS = (
        "忘记密码",
        "手机验证",
        "安全性",
        "原因",
        "为何",
        "为什么",
        "探讨",
        "讨论",
    )
    ORDINARY_GAME_CONTEXT_MARKERS = (
        "游戏",
        "单人局",
        "战局",
        "玩法",
        "模组",
        "mod",
        "steam创意工坊",
        "荒野大镖客",
        "群星",
        "创意工坊",
        "订阅说明",
    )
    ORDINARY_CONSUMER_PROMO_MARKERS = (
        "外卖",
        "饭卡",
        "流量卡",
        "运营商",
        "平台活动",
        "优惠",
        "折上折",
        "充值",
        "充100",
        "会员",
        "高信誉用户",
    )
    ORDINARY_NEWS_DISCUSSION_MARKERS = (
        "余承东",
        "聊天截图",
        "离职",
        "玩笑",
        "网友",
        "新闻",
        "报道",
    )
    ORDINARY_COMMUNITY_DISCUSSION_MARKERS = (
        "微信群",
        "交流群",
        "社区",
        "群二维码",
        "人工邀请",
        "解决方案讨论",
        "问题很麻烦",
        "产品限制",
        "普通社区",
        "技术、创意、生活",
        "b站",
        "哔哩哔哩",
        "app",
        "创建圈子",
        "加入已有的圈子",
        "怎么拉群",
        "下载安装",
    )
    SMS_PLATFORM_BUSINESS_MARKERS = (
        "接码平台",
        "临时号码",
        "接收验证码",
        "批量购买",
        "快速支付",
        "sms-activate",
        "供应商",
        "具体号码",
        "自有软件",
    )
    LAGROUP_OPERATION_MARKERS = (
        "拉群主号",
        "拉群拉手",
        "拉群任务",
        "拉群资源",
        "进群人数",
        "邀请链接",
        "主号进群",
        "经销代理",
        "实时拉群",
        "拉群协议方",
        "号源",
        "成群",
        "任务流程",
    )
    PRIVATE_DOMAIN_MONETIZATION_MARKERS = (
        "私域钩子",
        "自动导流",
        "微信个人号",
        "企业微信群",
        "私域日新增",
        "私域成交",
        "成交占比",
        "月变现",
        "矩阵",
        "变现稳定",
    )
    STRONG_TRANSACTION_INTENT_MARKERS = (
        "出售",
        "承接",
        "接单",
        "接任务",
        "招募",
        "上车",
        "低价",
        "报价",
        "价格",
        "下单",
        "老板",
        "担保",
        "包量",
        "量大",
        "秒出",
        "有需求",
        "来盘",
        "可谈",
        "长期合作",
        "for sale",
        "buy now",
        "wholesale",
    )
    DIRECT_CONTACT_INTENT_MARKERS = (
        "contact",
        "dm",
        "pm",
        "inbox",
        "message me",
        "联系",
        "私聊",
        "详聊",
        "客服",
        "咨询",
        "对接",
    )
    REVIEW_RISK_MARKERS = (
        "交易",
        "出售",
        "承接",
        "接单",
        "接任务",
        "招募",
        "上车",
        "低价",
        "报价",
        "价格",
        "下单",
        "老板",
        "担保",
        "包量",
        "量大",
        "秒出",
        "客服",
        "咨询",
        "私聊",
        "详聊",
        "进群",
        "拉群",
        "群发",
        "私信",
        "代发",
        "代投",
        "引流",
        "导流",
        "打粉",
        "活粉",
        "接码",
        "验证码",
        "群控",
        "脚本",
        "卡密",
        "跑分",
        "刷单",
        "补单",
        "返佣",
        "垫付",
    )
    CONTACT_MARKERS = ("tg:", "telegram:", "t.me/", "加v", "微信", "wechat", "wx:", "qq:", "@")

    def __init__(self, rule_registry: RuleRegistry | None = None) -> None:
        self.rule_registry = rule_registry or RuleRegistry()
        self.fast_classifier = RuleFastTrackClassifier(rule_registry=self.rule_registry)
        self.slang_variant_normalizer = SlangVariantNormalizer()
        configured_terms = self.rule_registry.primary_terms_by_label()
        self.category_keywords = {category: tuple(terms) for category, terms in configured_terms.items()}
        configured_promotions = self.rule_registry.promotion_markers_by_label()
        self.category_promotion_markers = {
            category: tuple(markers)
            for category, markers in configured_promotions.items()
        }
        configured_secondary = self.rule_registry.secondary_rules()
        self.secondary_rules = {
            category: {label: tuple(terms) for label, terms in labels.items()}
            for category, labels in configured_secondary.items()
        }
        self.secondary_signal_terms = tuple(
            dict.fromkeys(
                term
                for labels in self.secondary_rules.values()
                for terms in labels.values()
                for term in terms
                if str(term).strip()
            )
        )
        polarity = self.rule_registry.load_context_polarity()
        self.defensive_context_markers = tuple(str(item) for item in polarity.get("defensive_markers", []) if str(item).strip())
        self.generic_guide_markers = self.rule_registry.context_markers("generic_guide_markers")
        policy = self.rule_registry.classifier_policy()
        marker_groups = policy.get("promotion_marker_groups") if isinstance(policy.get("promotion_marker_groups"), Mapping) else {}
        self.crowd_promotion_markers = _as_tuple(marker_groups.get("crowd") if isinstance(marker_groups, Mapping) else ())
        self.tool_promotion_markers = _as_tuple(marker_groups.get("tool") if isinstance(marker_groups, Mapping) else ())
        self.tool_update_markers = _as_tuple(marker_groups.get("tool_update") if isinstance(marker_groups, Mapping) else ())
        self.click_promotion_markers = _as_tuple(marker_groups.get("click") if isinstance(marker_groups, Mapping) else ())
        self.click_core_markers = _as_tuple(marker_groups.get("click_core") if isinstance(marker_groups, Mapping) else ())
        self.solicitation_markers = tuple(
            dict.fromkeys(
                [
                    *_as_tuple(marker_groups.get("solicitation") if isinstance(marker_groups, Mapping) else ()),
                    *[marker for markers in self.category_promotion_markers.values() for marker in markers],
                ]
            )
        )
        self.blackgray_review_markers = tuple(
            dict.fromkeys(
                [
                    *self.solicitation_markers,
                    *self.secondary_signal_terms,
                    *self.REVIEW_RISK_MARKERS,
                ]
            )
        )
        self.review_only_categories = set(_as_tuple(policy.get("review_only_categories")))
        self.review_only_secondary_labels = set(_as_tuple(policy.get("review_only_secondary_labels")))
        auto_clear = policy.get("review_auto_clear") if isinstance(policy.get("review_auto_clear"), Mapping) else {}
        self.review_auto_clear_secondary_labels = set(
            _as_tuple(auto_clear.get("secondary_labels") if isinstance(auto_clear, Mapping) else ())
        )
        self.review_auto_clear_min_confidence = _float_value(
            auto_clear.get("min_confidence") if isinstance(auto_clear, Mapping) else None,
            0.78,
        )
        self.review_auto_clear_min_evidence = int(auto_clear.get("min_evidence") or 2) if isinstance(auto_clear, Mapping) else 2
        self.review_auto_clear_require_resolved_conflict = bool(
            auto_clear.get("require_resolved_conflict", True) if isinstance(auto_clear, Mapping) else True
        )
        self.review_auto_clear_require_non_theme_only = bool(
            auto_clear.get("require_non_theme_only", True) if isinstance(auto_clear, Mapping) else True
        )
        self.review_auto_clear_conflict_secondary_labels = set(
            _as_tuple(auto_clear.get("conflict_secondary_labels") if isinstance(auto_clear, Mapping) else ())
        )
        self.review_auto_clear_conflict_min_confidence = _float_value(
            auto_clear.get("conflict_min_confidence") if isinstance(auto_clear, Mapping) else None,
            0.92,
        )
        self.review_auto_clear_conflict_min_margin = int(
            auto_clear.get("conflict_min_margin") or 2
        ) if isinstance(auto_clear, Mapping) else 2
        secondary_gate = policy.get("secondary_label_gate") if isinstance(policy.get("secondary_label_gate"), Mapping) else {}
        self.secondary_min_markers_for_final = int(secondary_gate.get("min_markers_for_final") or 2) if isinstance(secondary_gate, Mapping) else 2
        self.secondary_allow_single_marker_with_entity_context = bool(
            secondary_gate.get("allow_single_marker_with_entity_context", True)
        ) if isinstance(secondary_gate, Mapping) else True
        self.secondary_entity_context_markers = _as_tuple(
            secondary_gate.get("entity_context_markers") if isinstance(secondary_gate, Mapping) else ()
        )
        self.category_priority = {str(key): int(value) for key, value in (policy.get("category_priority") or {}).items()} if isinstance(policy.get("category_priority"), Mapping) else {}
        self.theme_priors = self.rule_registry.theme_priors()
        self.polarity_scorer = polarity_from_config(polarity)
        self.rule_version = self.rule_registry.version_hash()

    def classify(self, record: Mapping[str, Any] | Any) -> FineClassificationResult:
        text = _text(record)
        slang_analysis = self.slang_variant_normalizer.analyze(text)
        match_text = slang_analysis.expanded_text or text
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "unknown")
        matched_keywords = self._signal_terms(record, "matched_keywords")
        matched_themes = self._signal_terms(record, "matched_themes")
        fast_payload = dict(record) if isinstance(record, Mapping) else {"content_text": text}
        fast_payload["clean_text"] = match_text
        fast = self.fast_classifier.classify(fast_payload)
        fast_data = fast.model_dump() if hasattr(fast, "model_dump") else dict(fast)
        category_scores, category_evidence, theme_only_scores = self._category_scores(match_text, matched_keywords, matched_themes)
        self._apply_slang_variant_scores(
            category_scores,
            category_evidence,
            slang_analysis=slang_analysis,
            text=match_text,
        )

        benign_override_evidence = self._ordinary_public_context_override_evidence(
            record=record,
            text=match_text,
            matched_keywords=matched_keywords,
            matched_themes=matched_themes,
            category_scores=category_scores,
        )
        if category_scores and benign_override_evidence:
            return FineClassificationResult(
                source_trace_id=trace_id,
                risk_category=NORMAL_NOISE,
                secondary_label="低相关",
                confidence=0.76,
                review_required=False,
                review_bucket=REVIEW_BUCKET_LOW_RELEVANCE,
                final_secondary_label="低相关",
                candidate_secondary_labels=[],
                conflict_status="RESOLVED",
                conflict_categories=[],
                evidence=benign_override_evidence,
                review_decision_reason="ordinary_public_context_overrode_homonym_risk",
            )

        if not category_scores:
            benign_evidence = self._ordinary_public_information_evidence(
                record=record,
                text=match_text,
                matched_keywords=matched_keywords,
                matched_themes=matched_themes,
                slang_analysis=slang_analysis,
            )
            if benign_evidence:
                return FineClassificationResult(
                    source_trace_id=trace_id,
                    risk_category=NORMAL_NOISE,
                    secondary_label="低相关",
                    confidence=0.72,
                    review_required=False,
                    review_bucket=REVIEW_BUCKET_LOW_RELEVANCE,
                    final_secondary_label="低相关",
                    candidate_secondary_labels=[],
                    conflict_status="RESOLVED",
                    conflict_categories=[],
                    evidence=benign_evidence,
                    review_decision_reason="ordinary_public_information_no_risk_signal",
                )
            return FineClassificationResult(
                source_trace_id=trace_id,
                risk_category=UNKNOWN,
                secondary_label="待研判",
                confidence=0.35,
                review_required=True,
                review_bucket=REVIEW_BUCKET_HUMAN_REVIEW_REQUIRED,
                final_secondary_label=None,
                candidate_secondary_labels=[],
                conflict_status="UNKNOWN",
                conflict_categories=[],
                evidence=[],
                review_decision_reason="no_category_score",
            )
        topic_terms = [term for values in category_evidence.values() for term in values if not str(term).startswith("theme:")]
        polarity = self.polarity_scorer.score(text, topic_terms=topic_terms)
        if self._is_defensive_context(match_text) or polarity.polarity == NEGATIVE_RISK_ASSERTION:
            return FineClassificationResult(
                source_trace_id=trace_id,
                risk_category=NORMAL_NOISE,
                secondary_label="研究讨论" if polarity.actor_intent == "research" else "防御语境",
                confidence=max(0.8, polarity.confidence),
                review_required=False,
                review_bucket=REVIEW_BUCKET_LOW_RELEVANCE,
                final_secondary_label=None,
                candidate_secondary_labels=[],
                conflict_status="NEGATIVE_RISK_ASSERTION",
                conflict_categories=[],
                evidence=polarity.evidence or ["defensive_context"],
                review_decision_reason="defensive_or_negative_context",
            )

        ordered = sorted(
            category_scores.items(),
            key=lambda item: (-item[1], -self.category_priority.get(item[0], 0), item[0]),
        )
        top_category, top_score = ordered[0]
        raw_conflicts = [category for category, score in ordered[1:] if score == top_score or (top_score - score <= 1 and score >= 2)]
        raw_conflicts = self._account_tool_cross_conflicts(
            raw_conflicts,
            top_category=top_category,
            category_scores=category_scores,
            text=match_text,
        )
        conflict_status = "RESOLVED"
        secondary_label, secondary_evidence, secondary_candidates = self._secondary_label(top_category, match_text, matched_keywords)
        supporting_evidence = self._ordered_unique((*category_evidence.get(top_category, []), *secondary_evidence))
        conflicts = self._calibrated_conflicts(
            raw_conflicts,
            top_category=top_category,
            top_score=top_score,
            ordered_scores=ordered,
            secondary_label=secondary_label,
            secondary_evidence=secondary_evidence,
            supporting_evidence=supporting_evidence,
            category_evidence=category_evidence,
            text=match_text,
        )

        confidence = max(
            float(fast_data.get("confidence", 0.0) or 0.0),
            min(0.96, 0.56 + top_score * 0.07 + len(secondary_evidence) * 0.03),
        )
        theme_only = bool(theme_only_scores.get(top_category, False))
        review_required = bool(fast_data.get("review_required", False))
        review_reason = "fast_classifier_review" if review_required else "auto_resolved"
        if top_category in self.review_only_categories:
            review_required = True
            review_reason = "review_only_category"
        if theme_only:
            review_required = True
            review_reason = "theme_only_evidence"
            confidence = min(confidence, 0.72)
        if secondary_label in {"未细分", "待研判"}:
            review_required = True
            review_reason = "secondary_label_unresolved"
        if secondary_label in self.review_only_secondary_labels:
            review_required = True
            review_reason = "review_only_secondary_label"
            confidence = min(confidence, 0.78)
        if conflicts:
            conflict_status = "CONFLICT_REVIEW"
            review_required = True
            review_reason = "category_conflict"
            confidence = min(confidence, 0.74)
        if self._can_auto_clear_review(
            secondary_label=secondary_label,
            confidence=confidence,
            evidence=supporting_evidence,
            has_conflict=bool(conflicts),
            theme_only=theme_only,
        ):
            review_required = False
            review_reason = "high_confidence_auto_clear"

        return FineClassificationResult(
            source_trace_id=trace_id,
            risk_category=top_category,
            secondary_label=secondary_label,
            final_secondary_label=None if secondary_label in {"未细分", "待研判"} else secondary_label,
            candidate_secondary_labels=secondary_candidates,
            confidence=round(confidence, 4),
            review_required=review_required,
            review_bucket=review_bucket_for_classification(
                risk_category=top_category,
                review_required=review_required,
                confidence=confidence,
                secondary_label=secondary_label,
                conflict_status=conflict_status,
            ),
            conflict_status=conflict_status,
            conflict_categories=conflicts,
            evidence=supporting_evidence,
            review_decision_reason=review_reason,
        )

    def _is_defensive_context(self, text: str) -> bool:
        defensive_hits = self._marker_hits(text, self.defensive_context_markers)
        if not defensive_hits:
            return False
        solicitation_hits = self._marker_hits(text, self.solicitation_markers)
        return len(solicitation_hits) == 0 or any(marker in text for marker in ("不提供", "不要参与", "切勿参与"))

    def _ordinary_public_information_evidence(
        self,
        *,
        record: Mapping[str, Any] | Any,
        text: str,
        matched_keywords: tuple[str, ...],
        matched_themes: tuple[str, ...],
        slang_analysis: SlangVariantAnalysis,
    ) -> list[str]:
        if not text.strip():
            return []
        if matched_keywords or matched_themes or any(candidate.category_hint for candidate in slang_analysis.confirmed_candidates):
            return []
        if self._has_blackgray_review_signal(text):
            return []

        source_context = " ".join(
            normalize_text(str(get_record_field(record, field) or ""))
            for field in ("source_name", "source_type", "source_url")
        )
        ordinary_hits = self._marker_hits(
            text,
            (
                *self.ORDINARY_PUBLIC_INFO_MARKERS,
                *self.ORDINARY_TECHNICAL_MARKERS,
                *self.generic_guide_markers,
            ),
        )
        software_update_hits = self._marker_hits(text, self.ORDINARY_SOFTWARE_UPDATE_MARKERS)
        source_hits = self._marker_hits(source_context, self.ORDINARY_PUBLIC_SOURCE_MARKERS)
        public_link_hits = self._marker_hits(text, self.PUBLIC_SOCIAL_LINK_MARKERS)
        has_public_or_technical_context = bool(ordinary_hits or software_update_hits or (source_hits and public_link_hits))
        if not has_public_or_technical_context:
            return []
        if self._has_direct_contact_intent(text):
            return []
        if self._has_contact_marker(text) and not (public_link_hits or software_update_hits):
            return []

        evidence = [
            *(f"ordinary:{marker}" for marker in ordinary_hits[:3]),
            *(f"software_update:{marker}" for marker in software_update_hits[:3]),
            *(f"source:{marker}" for marker in source_hits[:2]),
            *(f"public_link:{marker}" for marker in public_link_hits[:2]),
        ]
        return self._ordered_unique(evidence or ["ordinary_public_information"])

    def _ordinary_public_context_override_evidence(
        self,
        *,
        record: Mapping[str, Any] | Any,
        text: str,
        matched_keywords: tuple[str, ...],
        matched_themes: tuple[str, ...],
        category_scores: Mapping[str, int],
    ) -> list[str]:
        if not text.strip() or not category_scores:
            return []
        if self._has_disqualifying_solicitation(text):
            return []
        if self._has_blackgray_business_operation_signal(
            text=text,
            matched_keywords=matched_keywords,
            matched_themes=matched_themes,
            category_scores=category_scores,
        ):
            return []

        source_context = " ".join(
            normalize_text(str(get_record_field(record, field) or ""))
            for field in ("source_name", "source_type", "source_url")
        )
        evidence: list[str] = []
        matched_hint = [*(f"keyword:{item}" for item in matched_keywords[:3]), *(f"theme:{item}" for item in matched_themes[:2])]

        if "steam" in text.lower() and "验证码" in text and self._marker_hits(text, self.ORDINARY_STEAM_DISCUSSION_MARKERS):
            evidence.extend(["ordinary_context:steam_account_safety_discussion", *matched_hint])

        if "卡单" in text and self._marker_hits(text, self.ORDINARY_GAME_CONTEXT_MARKERS):
            evidence.extend(["ordinary_context:gameplay_or_mod_discussion", *matched_hint])

        game_hits = self._marker_hits(text, self.ORDINARY_GAME_CONTEXT_MARKERS)
        if game_hits and any(marker in text.lower() for marker in ("automation", "脚本", "mod", "模组")):
            evidence.extend(["ordinary_context:game_mod_automation_discussion", *matched_hint])

        if self._marker_hits(text, self.ORDINARY_CONSUMER_PROMO_MARKERS) and any(marker in text for marker in ("返佣", "佣金", "优惠", "折上折")):
            evidence.extend(["ordinary_context:consumer_promotion_article", *matched_hint])

        if "拉群" in text and self._marker_hits(text, self.ORDINARY_NEWS_DISCUSSION_MARKERS):
            evidence.extend(["ordinary_context:public_news_discussion", *matched_hint])

        if self._marker_hits(text, self.ORDINARY_COMMUNITY_DISCUSSION_MARKERS) and any(marker in text for marker in ("拉新", "拉群", "邀请", "群二维码", "微信群")):
            evidence.extend(["ordinary_context:community_operations_discussion", *matched_hint])

        if evidence:
            ordinary_hits = self._marker_hits(
                text,
                (
                    *self.ORDINARY_PUBLIC_INFO_MARKERS,
                    *self.ORDINARY_TECHNICAL_MARKERS,
                    *self.ORDINARY_GAME_CONTEXT_MARKERS,
                    *self.ORDINARY_CONSUMER_PROMO_MARKERS,
                    *self.ORDINARY_NEWS_DISCUSSION_MARKERS,
                    *self.ORDINARY_COMMUNITY_DISCUSSION_MARKERS,
                ),
            )
            source_hits = self._marker_hits(source_context, self.ORDINARY_PUBLIC_SOURCE_MARKERS)
            evidence.extend(f"ordinary:{marker}" for marker in ordinary_hits[:3])
            evidence.extend(f"source:{marker}" for marker in source_hits[:2])
        return self._ordered_unique(evidence)

    def _has_blackgray_review_signal(self, text: str) -> bool:
        hits = self._marker_hits(text, self.blackgray_review_markers)
        if not hits:
            return False
        ordinary_hits = self._marker_hits(
            text,
            (
                *self.ORDINARY_PUBLIC_INFO_MARKERS,
                *self.ORDINARY_TECHNICAL_MARKERS,
                *self.ORDINARY_SOFTWARE_UPDATE_MARKERS,
                *self.PUBLIC_SOCIAL_LINK_MARKERS,
            ),
        )
        risk_hits = [
            hit
            for hit in hits
            if hit.lower() not in {marker.lower() for marker in self.TECHNICAL_HOMONYM_RISK_MARKERS}
        ]
        return bool(risk_hits or not ordinary_hits)

    def _has_contact_marker(self, text: str) -> bool:
        lowered_text = text.lower()
        return any(marker.lower() in lowered_text for marker in self.CONTACT_MARKERS)

    def _has_direct_contact_intent(self, text: str) -> bool:
        lowered_text = text.lower()
        if not self._has_contact_marker(text):
            return False
        english_markers = ("contact", "dm", "pm", "inbox")
        if any(re.search(rf"\b{re.escape(marker)}\b", lowered_text) for marker in english_markers):
            return True
        if re.search(r"\bmessage\s+me\b", lowered_text):
            return True
        chinese_markers = ("联系", "私聊", "详聊", "客服", "咨询", "对接")
        return any(marker in text for marker in chinese_markers)

    def _has_disqualifying_solicitation(self, text: str) -> bool:
        if self._has_direct_contact_intent(text):
            return True
        if self._has_contact_marker(text):
            public_link_hits = self._marker_hits(text, self.PUBLIC_SOCIAL_LINK_MARKERS)
            software_update_hits = self._marker_hits(text, self.ORDINARY_SOFTWARE_UPDATE_MARKERS)
            if not (public_link_hits or software_update_hits):
                if self._is_ordinary_platform_reference(text):
                    return False
                return True
        if self._is_ordinary_game_mod_discussion(text):
            solicitation_hits = self._marker_hits(text, self.STRONG_TRANSACTION_INTENT_MARKERS)
            risky_hits = [hit for hit in solicitation_hits if hit not in {"招募"}]
            return bool(risky_hits)
        return bool(self._marker_hits(text, self.STRONG_TRANSACTION_INTENT_MARKERS))

    def _is_ordinary_platform_reference(self, text: str) -> bool:
        if not self._marker_hits(text, self.ORDINARY_COMMUNITY_DISCUSSION_MARKERS):
            return False
        if any(marker in text.lower() for marker in ("wx:", "wechat:", "微信:", "微信号", "加微信", "加v", "联系微信")):
            return False
        return any(marker in text for marker in ("微信群", "交流群", "群二维码"))

    def _is_ordinary_game_mod_discussion(self, text: str) -> bool:
        lowered = text.lower()
        if self._has_contact_marker(text):
            return False
        game_hits = self._marker_hits(text, self.ORDINARY_GAME_CONTEXT_MARKERS)
        if not game_hits:
            return False
        return any(marker in lowered for marker in ("steam", "mod", "automation", "创意工坊", "游戏"))

    def _has_blackgray_business_operation_signal(
        self,
        *,
        text: str,
        matched_keywords: tuple[str, ...],
        matched_themes: tuple[str, ...],
        category_scores: Mapping[str, int],
    ) -> bool:
        keyword_text = " ".join((*matched_keywords, *matched_themes, text))
        if (
            category_scores.get(ACCOUNT_TRADING, 0) > 0
            and self._marker_hits(keyword_text, ("接码", "接码平台", "验证码"))
            and len(self._marker_hits(text, self.SMS_PLATFORM_BUSINESS_MARKERS)) >= 2
        ):
            return True
        if (
            (category_scores.get(CROWD_SERVICE, 0) > 0 or category_scores.get(FRAUD_TRAFFIC, 0) > 0)
            and "拉群" in text
            and len(self._marker_hits(text, self.LAGROUP_OPERATION_MARKERS)) >= 2
        ):
            return True
        if (
            category_scores.get(FRAUD_TRAFFIC, 0) > 0
            and "私域" in text
            and "导流" in text
            and len(self._marker_hits(text, self.PRIVATE_DOMAIN_MONETIZATION_MARKERS)) >= 2
        ):
            return True
        return False

    def _suppress_affiliate_rebate_click_confusion(
        self,
        score_map: dict[str, int],
        evidence_map: dict[str, list[str]],
        *,
        text: str,
    ) -> None:
        if not score_map.get(FRAUD_TRAFFIC) or not score_map.get(CLICK_FARMING):
            return
        if self._marker_hits(text, ("刷单", "补单", "垫付", "做任务", "点赞任务", "关注任务", "卡单", "日结", "兼职")):
            return
        fraud_hits = self._marker_hits(text, ("交易所", "okx", "币安", "binance", "api", "开户链接", "开户", "拉新", "高佣", "返利", "合约", "节点"))
        click_evidence = {
            str(item).replace("click:", "").strip()
            for item in evidence_map.get(CLICK_FARMING, [])
            if str(item).strip()
        }
        if fraud_hits and click_evidence and click_evidence <= {"返佣"}:
            score_map.pop(CLICK_FARMING, None)
            evidence_map.pop(CLICK_FARMING, None)

    def _account_tool_cross_conflicts(
        self,
        raw_conflicts: list[str],
        *,
        top_category: str,
        category_scores: Mapping[str, int],
        text: str,
    ) -> list[str]:
        if top_category not in {ACCOUNT_TRADING, TOOL_TRADING}:
            return raw_conflicts
        other = ACCOUNT_TRADING if top_category == TOOL_TRADING else TOOL_TRADING
        if other in raw_conflicts:
            return raw_conflicts
        if not category_scores.get(other):
            return raw_conflicts
        lowered = text.lower()
        account_hits = self._marker_hits(text, ("注册账号", "账号", "接码", "验证码", "手机码"))
        tool_hits = self._marker_hits(text, ("接码平台", "卡密", "用户端", "电脑端", "网址", "后台"))
        has_contact_or_url = any(marker in lowered for marker in ("@", "tg:", "telegram", "http://", "https://", "客服", "联系"))
        if account_hits and tool_hits and has_contact_or_url:
            return [*raw_conflicts, other]
        return raw_conflicts

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

        for category, keywords in self.category_keywords.items():
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
            mapped = self.theme_priors.get(theme)
            if mapped is None:
                continue
            category, bonus = mapped
            score_map[category] = score_map.get(category, 0) + bonus
            evidence_map[category].append(f"theme:{theme}")

        crowd_markers = _ordered_unique(
            [
                *self._marker_hits(text, self.category_promotion_markers.get(CROWD_SERVICE, ())),
                *self._marker_hits(text, self.crowd_promotion_markers),
            ]
        )

        if crowd_markers and self._matches_any(text, matched_keyword_set, self.secondary_rules[CROWD_SERVICE]["拉群获客"]):
            score_map[CROWD_SERVICE] = score_map.get(CROWD_SERVICE, 0) + min(2, len(crowd_markers))
            evidence_map[CROWD_SERVICE].extend(f"service:{marker}" for marker in crowd_markers[:2])

        tool_generic_markers = self._marker_hits(text, self.category_promotion_markers.get(TOOL_TRADING, ()))
        tool_specific_markers = self._marker_hits(text, self.tool_promotion_markers)
        tool_markers = _ordered_unique([*tool_specific_markers, *tool_generic_markers])
        if tool_specific_markers or (tool_generic_markers and score_map.get(TOOL_TRADING, 0) > 0):
            score_map[TOOL_TRADING] = score_map.get(TOOL_TRADING, 0) + min(3, len(tool_markers))
            evidence_map[TOOL_TRADING].extend(f"tool:{marker}" for marker in tool_markers[:3])

        tool_update_markers = self._marker_hits(text, self.tool_update_markers)
        if len(tool_update_markers) >= 2:
            score_map[TOOL_TRADING] = score_map.get(TOOL_TRADING, 0) + 2
            evidence_map[TOOL_TRADING].extend(f"tool_update:{marker}" for marker in tool_update_markers[:2])

        click_markers = _ordered_unique(
            [
                *self._marker_hits(text, self.category_promotion_markers.get(CLICK_FARMING, ())),
                *self._marker_hits(text, self.click_promotion_markers),
            ]
        )
        if click_markers and ("卡单" in text or "手工单" in text or "做单" in text):
            score_map[CLICK_FARMING] = score_map.get(CLICK_FARMING, 0) + min(2, len(click_markers))
            evidence_map[CLICK_FARMING].extend(f"order:{marker}" for marker in click_markers[:2])

        click_core_markers = self._marker_hits(text, self.click_core_markers)
        if click_core_markers:
            score_map[CLICK_FARMING] = score_map.get(CLICK_FARMING, 0) + min(2, len(click_core_markers))
            evidence_map[CLICK_FARMING].extend(f"click:{marker}" for marker in click_core_markers[:2])

        self._suppress_affiliate_rebate_click_confusion(score_map, evidence_map, text=text)

        theme_only_scores = {
            category: all(item.startswith("theme:") for item in evidence_map.get(category, []))
            for category in score_map
        }
        return score_map, {key: self._ordered_unique(value) for key, value in evidence_map.items()}, theme_only_scores

    def _apply_slang_variant_scores(
        self,
        score_map: dict[str, int],
        evidence_map: dict[str, list[str]],
        *,
        slang_analysis: SlangVariantAnalysis,
        text: str,
    ) -> None:
        confirmed = slang_analysis.confirmed_candidates
        if not confirmed:
            return
        normalized_terms = {candidate.normalized for candidate in confirmed}
        has_contact_or_url = any(marker in text.lower() for marker in ("tg:", "telegram", "http://", "https://", "hxxp://", "hxxps://", "@", "加v", "微信"))
        has_trade_or_recruit = bool(set(slang_analysis.context_hits).intersection({"出售", "卖", "买", "接单", "招募", "上车", "拉群", "进群", "联系", "咨询", "私聊", "低价", "价格", "卡密", "短链", "暗号", "口令", "邀请码", "code:"}))
        for candidate in confirmed:
            if candidate.category_hint:
                score_map[candidate.category_hint] = score_map.get(candidate.category_hint, 0) + 1
                evidence_map.setdefault(candidate.category_hint, []).append(f"slang:{candidate.normalized}")
        if has_contact_or_url and has_trade_or_recruit and normalized_terms.intersection({"抖音", "加v", "群组", "账号资料", "Telegram"}):
            score_map[FRAUD_TRAFFIC] = score_map.get(FRAUD_TRAFFIC, 0) + 3
            evidence_map.setdefault(FRAUD_TRAFFIC, []).append("slang_context:contact_or_url_plus_recruiting")
        if has_contact_or_url and has_trade_or_recruit and "Telegram" in normalized_terms and self._platform_account_trade_context(text):
            score_map[ACCOUNT_TRADING] = score_map.get(ACCOUNT_TRADING, 0) + 4
            evidence_map.setdefault(ACCOUNT_TRADING, []).append("slang_context:platform_account_trade")
        if normalized_terms.intersection({"群控", "脚本", "卡密"}) and has_trade_or_recruit:
            score_map[TOOL_TRADING] = score_map.get(TOOL_TRADING, 0) + 2
            evidence_map.setdefault(TOOL_TRADING, []).append("slang_context:tool_trade")

    def _platform_account_trade_context(self, text: str) -> bool:
        lowered = text.lower()
        trade_hits = ("低价", "价格", "出售", "卖", "买", "号", "账号", "卡密", "可谈")
        contact_hits = ("@", "tg:", "telegram", "联系", "私聊", "客服", "咨询")
        return any(marker in lowered for marker in trade_hits) and any(marker in lowered for marker in contact_hits)

    def _secondary_label(self, category: str, text: str, matched_keywords: tuple[str, ...]) -> tuple[str, list[str], list[dict[str, Any]]]:
        candidates: list[tuple[str, list[str], bool]] = []
        matched_keyword_set = {value.lower() for value in matched_keywords}
        for label, keywords in self.secondary_rules.get(category, {}).items():
            hits = [
                keyword
                for keyword in keywords
                if keyword.lower() in text.lower() or normalize_text(keyword).lower() in matched_keyword_set
            ]
            if hits:
                has_entity_context = any(marker.lower() in text.lower() for marker in self.secondary_entity_context_markers)
                candidates.append((label, self._ordered_unique(hits), has_entity_context))
        if not candidates:
            return "未细分", [], []
        candidate_payloads = [
            {
                "label": label,
                "confidence": round(min(0.92, 0.46 + 0.12 * len(hits) + (0.08 if has_entity_context else 0.0)), 4),
                "evidence": hits,
                "reason": (
                    "secondary_gate_ready"
                    if self._secondary_gate_ready(hits, has_entity_context)
                    else "single_secondary_marker_only"
                ),
            }
            for label, hits, has_entity_context in candidates
        ]
        label, hits, has_entity_context = max(
            candidates,
            key=lambda item: (len(item[1]), item[2], item[0]),
        )
        if self._secondary_gate_ready(hits, has_entity_context):
            return label, hits, candidate_payloads
        return "待研判", [], candidate_payloads

    def _secondary_gate_ready(self, hits: list[str], has_entity_context: bool) -> bool:
        if len(hits) >= self.secondary_min_markers_for_final:
            return True
        return bool(
            self.secondary_allow_single_marker_with_entity_context
            and hits
            and has_entity_context
        )

    def _can_auto_clear_review(
        self,
        *,
        secondary_label: str,
        confidence: float,
        evidence: list[str],
        has_conflict: bool,
        theme_only: bool,
    ) -> bool:
        if not self.review_auto_clear_secondary_labels:
            return False
        if secondary_label not in self.review_auto_clear_secondary_labels:
            return False
        if secondary_label in self.review_only_secondary_labels or secondary_label in {"未细分", "待研判"}:
            return False
        if self.review_auto_clear_require_resolved_conflict and has_conflict:
            return False
        if self.review_auto_clear_require_non_theme_only and theme_only:
            return False
        if confidence < self.review_auto_clear_min_confidence:
            return False
        non_theme_evidence = [item for item in evidence if not str(item).startswith("theme:")]
        return len(non_theme_evidence) >= self.review_auto_clear_min_evidence

    def _calibrated_conflicts(
        self,
        raw_conflicts: list[str],
        *,
        top_category: str,
        top_score: int,
        ordered_scores: list[tuple[str, int]],
        secondary_label: str,
        secondary_evidence: list[str],
        supporting_evidence: list[str],
        category_evidence: Mapping[str, list[str]],
        text: str,
    ) -> list[str]:
        if not raw_conflicts:
            return []
        if self._can_safely_resolve_conflict(
            top_category=top_category,
            top_score=top_score,
            ordered_scores=ordered_scores,
            secondary_label=secondary_label,
            secondary_evidence=secondary_evidence,
            supporting_evidence=supporting_evidence,
            category_evidence=category_evidence,
            text=text,
        ):
            return []
        return raw_conflicts

    def _can_safely_resolve_conflict(
        self,
        *,
        top_category: str,
        top_score: int,
        ordered_scores: list[tuple[str, int]],
        secondary_label: str,
        secondary_evidence: list[str],
        supporting_evidence: list[str],
        category_evidence: Mapping[str, list[str]],
        text: str,
    ) -> bool:
        if not self.review_auto_clear_conflict_secondary_labels:
            return False
        if secondary_label not in self.review_auto_clear_conflict_secondary_labels:
            return False
        if secondary_label in self.review_only_secondary_labels or secondary_label in {"未细分", "待研判"}:
            return False
        if len(secondary_evidence) < self.review_auto_clear_min_evidence:
            return False
        non_theme_evidence = [item for item in supporting_evidence if not str(item).startswith("theme:")]
        if len(non_theme_evidence) < self.review_auto_clear_min_evidence:
            return False
        if top_score < max(score for _category, score in ordered_scores[1:] or [(UNKNOWN, 0)]):
            return False
        best_secondary_confidence = max(
            [
                float(item.get("confidence") or 0.0)
                for item in self._secondary_label(top_category, text, ())[2]
                if item.get("label") == secondary_label
            ]
            or [0.0]
        )
        if best_secondary_confidence < self.review_auto_clear_conflict_min_confidence:
            return False
        top_non_theme_count = len([item for item in category_evidence.get(top_category, []) if not str(item).startswith("theme:")])
        competing_non_theme_count = max(
            (
                len([item for item in category_evidence.get(category, []) if not str(item).startswith("theme:")])
                for category, _score in ordered_scores[1:]
            ),
            default=0,
        )
        return top_non_theme_count - competing_non_theme_count >= self.review_auto_clear_conflict_min_margin

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
    canonical_hash: str | None = None
    masked_value: str | None = None
    normalizer_version: str = "entity_normalizer_v1"
    sensitivity_level: str = "normal"

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
        occupied: list[range] = []
        for raw, target in sorted(self._terms.items(), key=lambda item: len(str(item[0])), reverse=True):
            start = lowered.find(raw.lower())
            while start >= 0:
                span = range(start, start + len(raw))
                if not any(_ranges_overlap(span, used) for used in occupied):
                    results.append((raw, target, start, start + len(raw)))
                    occupied.append(span)
                start = lowered.find(raw.lower(), start + len(raw))
        return results


class AdvancedEntityExtractor:
    """Phase II normalization + Phase III hidden entity discovery."""

    def __init__(self, slang_dictionary: SlangDictionary | None = None, rule_registry: RuleRegistry | None = None) -> None:
        self.rule_registry = rule_registry or RuleRegistry()
        self.basic = BasicEntityExtractor(rule_registry=self.rule_registry)
        self.slang_dictionary = slang_dictionary or SlangDictionary(rule_registry=self.rule_registry)
        self.slang_variant_normalizer = SlangVariantNormalizer()
        self.entity_normalizer = EntityNormalizer()
        self.configured_patterns = _compile_entity_patterns(self.rule_registry.load_entity_patterns())

    def extract(self, record: Mapping[str, Any] | Any) -> list[AdvancedEntity]:
        text = _text(record)
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "unknown")
        entities: list[AdvancedEntity] = []
        seen: set[tuple[str, str]] = set()

        def add(entity_type: str, value: str, start: int, end: int, *, method: str = "advanced_rule_v2", confidence: float = 1.0) -> None:
            slang_normalized = self.slang_dictionary.normalize(_normalize_obfuscation(value))
            normalized_entity = self.entity_normalizer.normalize(
                entity_type=entity_type,
                raw_value=slang_normalized,
                confidence=confidence,
            )
            normalized = normalized_entity.normalized_value
            final_type = normalized_entity.entity_type
            key = (final_type, normalized)
            if key in seen or not normalized:
                return
            seen.add(key)
            entities.append(
                AdvancedEntity(
                    entity_type=final_type,
                    entity_value=value.strip(),
                    normalized_value=normalized,
                    start_offset=start,
                    end_offset=end,
                    source_trace_id=trace_id,
                    confidence=confidence,
                    context_relevance=context_relevance(text, start, end),
                    extraction_method=method,
                    canonical_hash=normalized_entity.canonical_hash,
                    masked_value=normalized_entity.masked_value,
                    normalizer_version=normalized_entity.normalizer_version,
                    sensitivity_level=normalized_entity.sensitivity_level,
                )
            )

        for basic_entity in self.basic.extract(record):
            data = basic_entity.model_dump() if hasattr(basic_entity, "model_dump") else dict(basic_entity)
            add(data["entity_type"], data["entity_value"], int(data["start_offset"]), int(data["end_offset"]), method="basic_plus_normalized")

        for raw, _target, start, end in self.slang_dictionary.candidates_in_text(text):
            add("slang_term", raw, start, end, method="slang_dictionary", confidence=0.88)
        for candidate in self.slang_variant_normalizer.candidates_in_text(text):
            if not candidate.context_confirmed and candidate.category_hint:
                continue
            add(
                candidate.entity_type,
                candidate.raw,
                candidate.start_offset,
                candidate.end_offset,
                method=candidate.method,
                confidence=candidate.confidence,
            )
        for regex, entity_type, method in self.configured_patterns:
            for match in regex.finditer(text):
                group_index = _first_group_index(match)
                value = match.group(group_index) if group_index is not None else match.group(0)
                start = match.start(group_index) if group_index is not None else match.start()
                add(entity_type, value, start, start + len(value), method=method, confidence=0.84)
        return filter_and_order_entities(entities, record)


def _first_group_index(match: re.Match[str]) -> int | None:
    for index, value in enumerate(match.groups(), start=1):
        if value:
            return index
    return None


def _compile_entity_patterns(payload: Mapping[str, Any]) -> list[tuple[re.Pattern[str], str, str]]:
    compiled: list[tuple[re.Pattern[str], str, str]] = []
    for name, spec in payload.items():
        if not isinstance(spec, Mapping):
            continue
        entity_type = str(spec.get("entity_type") or name)
        raw_patterns = spec.get("patterns") if isinstance(spec.get("patterns"), list) else [spec.get("pattern")]
        for pattern in raw_patterns:
            text = str(pattern or "").strip()
            if not text:
                continue
            try:
                compiled.append((re.compile(text, re.IGNORECASE), entity_type, str(spec.get("method") or f"configured_entity_pattern:{name}")))
            except re.error:
                continue
    return compiled


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, Iterable):
        values = value
    else:
        values = ()
    return tuple(dict.fromkeys(str(item) for item in values if str(item).strip()))


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(normalized)
    return ordered


def _ranges_overlap(left: range, right: range) -> bool:
    return left.start < right.stop and right.start < left.stop


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
    "SlangVariantAnalysis",
    "SlangVariantCandidate",
    "SlangVariantNormalizer",
    "context_relevance",
    "shannon_entropy",
]
