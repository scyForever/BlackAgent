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
    TOOL_TRADING,
    UNKNOWN,
    RuleFastTrackClassifier,
)
from src.collector.base_collector import get_record_field
from src.extractor.entity_extractor import ACCOUNT, CONTACT, TOOL_NAME, URL, BasicEntityExtractor
from src.enhancement.context_polarity import NEGATIVE_RISK_ASSERTION, polarity_from_config
from src.intelligence.entity_normalizer import EntityNormalizer
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
    final_secondary_label: str | None = None
    candidate_secondary_labels: list[dict[str, Any]] = field(default_factory=list)
    conflict_status: str = "RESOLVED"
    conflict_categories: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    classifier_version: str = "fine_grained_v2_conflict_v3"

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
        polarity = self.rule_registry.load_context_polarity()
        self.defensive_context_markers = tuple(str(item) for item in polarity.get("defensive_markers", []) if str(item).strip())
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

        if not category_scores:
            return FineClassificationResult(trace_id, UNKNOWN, "待研判", 0.35, True, "UNKNOWN", [], [])
        topic_terms = [term for values in category_evidence.values() for term in values if not str(term).startswith("theme:")]
        polarity = self.polarity_scorer.score(text, topic_terms=topic_terms)
        if self._is_defensive_context(match_text) or polarity.polarity == NEGATIVE_RISK_ASSERTION:
            return FineClassificationResult(
                trace_id,
                NORMAL_NOISE,
                "研究讨论" if polarity.actor_intent == "research" else "防御语境",
                max(0.8, polarity.confidence),
                False,
                "NEGATIVE_RISK_ASSERTION",
                [],
                polarity.evidence or ["defensive_context"],
            )

        ordered = sorted(
            category_scores.items(),
            key=lambda item: (-item[1], -self.category_priority.get(item[0], 0), item[0]),
        )
        top_category, top_score = ordered[0]
        conflicts = [category for category, score in ordered[1:] if score == top_score or (top_score - score <= 1 and score >= 2)]
        conflict_status = "RESOLVED"
        secondary_label, secondary_evidence, secondary_candidates = self._secondary_label(top_category, match_text, matched_keywords)
        supporting_evidence = self._ordered_unique((*category_evidence.get(top_category, []), *secondary_evidence))

        confidence = max(
            float(fast_data.get("confidence", 0.0) or 0.0),
            min(0.96, 0.56 + top_score * 0.07 + len(secondary_evidence) * 0.03),
        )
        theme_only = bool(theme_only_scores.get(top_category, False))
        review_required = bool(fast_data.get("review_required", False))
        if top_category in self.review_only_categories:
            review_required = True
        if theme_only:
            review_required = True
            confidence = min(confidence, 0.72)
        if secondary_label in {"未细分", "待研判"}:
            review_required = True
        if secondary_label in self.review_only_secondary_labels:
            review_required = True
            confidence = min(confidence, 0.78)
        if conflicts:
            conflict_status = "CONFLICT_REVIEW"
            review_required = True
            confidence = min(confidence, 0.74)
        if self._can_auto_clear_review(
            secondary_label=secondary_label,
            confidence=confidence,
            evidence=supporting_evidence,
            has_conflict=bool(conflicts),
            theme_only=theme_only,
        ):
            review_required = False

        return FineClassificationResult(
            source_trace_id=trace_id,
            risk_category=top_category,
            secondary_label=secondary_label,
            final_secondary_label=None if secondary_label in {"未细分", "待研判"} else secondary_label,
            candidate_secondary_labels=secondary_candidates,
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
        solicitation_hits = self._marker_hits(text, self.solicitation_markers)
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
        has_trade_or_recruit = bool(set(slang_analysis.context_hits).intersection({"出售", "卖", "接单", "招募", "上车", "拉群", "进群", "联系", "咨询", "短链", "暗号", "口令", "邀请码", "code:"}))
        for candidate in confirmed:
            if candidate.category_hint:
                score_map[candidate.category_hint] = score_map.get(candidate.category_hint, 0) + 1
                evidence_map.setdefault(candidate.category_hint, []).append(f"slang:{candidate.normalized}")
        if has_contact_or_url and has_trade_or_recruit and normalized_terms.intersection({"抖音", "加v", "群组", "账号资料", "Telegram"}):
            score_map[FRAUD_TRAFFIC] = score_map.get(FRAUD_TRAFFIC, 0) + 3
            evidence_map.setdefault(FRAUD_TRAFFIC, []).append("slang_context:contact_or_url_plus_recruiting")
        if normalized_terms.intersection({"群控", "脚本", "卡密"}) and has_trade_or_recruit:
            score_map[TOOL_TRADING] = score_map.get(TOOL_TRADING, 0) + 2
            evidence_map.setdefault(TOOL_TRADING, []).append("slang_context:tool_trade")

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
        return sorted(entities, key=lambda item: (item.source_trace_id, item.start_offset, item.entity_type))


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
