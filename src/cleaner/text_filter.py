"""Deterministic text filtering, normalization, and near-duplicate grouping."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from hashlib import sha1
from importlib import import_module
from typing import Any


MAX_CLEAN_TEXT_CHARS = 4000
NEAR_DUPLICATE_THRESHOLD = 0.92
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
WHITESPACE_RE = re.compile(r"\s+")
BLACKGRAY_CONTEXT_VARIANT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(加|联系|咨询|客服|对接)\s*[+＋➕]?\s*[vV薇微威围]\b"), r"\1v"),
    (re.compile(r"(?i)\bv\s*[xX]\b"), "vx"),
    (re.compile(r"(?i)\bw\s*[xX]\b"), "vx"),
    (re.compile(r"(?i)\bd\s*y\b"), "dy"),
    (re.compile(r"进裙"), "进群"),
    (re.compile(r"拉裙"), "拉群"),
)
BLACKGRAY_LITERAL_VARIANT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("➕", "加"),
    ("＋", "加"),
    ("✈️", "飞机"),
    ("✈", "飞机"),
    ("🛩️", "飞机"),
    ("🛩", "飞机"),
    ("🛰️", "飞机"),
    ("🛰", "飞机"),
    ("🎵", "音符"),
    ("🐧", "QQ"),
    ("🍠", "小红书"),
    ("纸飞机", "飞机"),
    ("小飞机", "飞机"),
)
CONTACT_MARKER_RE = re.compile(r"(?:tg|telegram|wx|wechat|qq)[:：@]?[a-zA-Z0-9_\-]{3,}", re.IGNORECASE)
URL_MARKER_RE = re.compile(r"(?:https?://|hxxps?://|t\.me/|[a-z0-9-]+\s*\[\.\]\s*(?:com|cn|net|top|xyz))", re.IGNORECASE)
DEFENSIVE_CONTEXT_MARKERS: tuple[str, ...] = (
    "警方通报",
    "公安通报",
    "反诈",
    "安全通告",
    "曝光",
    "辟谣",
    "新闻报道",
    "研究分析",
    "普法",
)
GENERIC_GUIDE_MARKERS: tuple[str, ...] = (
    "使用指南",
    "指南",
    "教程",
    "推荐",
    "收藏",
    "实用",
    "盘点",
    "合集",
    "测评",
)
HIGH_RISK_MARKER_SETS: dict[str, tuple[str, ...]] = {
    "诈骗引流": ("引流", "导流", "私域", "拉新", "落地页", "加v", "加微", "私聊进群", "拉群"),
    "接码注册": ("接码", "验证码", "批量注册", "虚拟号码", "云短信", "实卡"),
    "账号交易": ("实名号", "白号", "老号", "卖号", "收号", "账号买卖", "verified account", "实名认证"),
    "工具交易": ("群控", "脚本", "协议号", "卡密", "拉群端", "开控", "软件", "自动注册", "改机", "外挂"),
    "众包服务": ("接单", "拉人", "群发", "代发", "采集群成员", "工作室", "报价", "客服", "代投"),
    "刷单作弊": ("刷单", "补单", "做单", "点赞任务", "关注任务", "垫付", "返佣", "返利", "日结"),
    "跑分代付": ("跑分", "代付", "usdt", "虚拟币", "银行卡", "支付宝", "微信收款"),
}
HIGH_RISK_HINT_SETS: dict[str, tuple[str, ...]] = {
    "诈骗引流": ("诈骗引流", "私域导流"),
    "接码注册": ("接码", "验证码"),
    "账号交易": ("账号交易", "帐号交易"),
    "工具交易": ("工具交易",),
    "众包服务": ("众包任务", "众包服务"),
    "刷单作弊": ("刷单作弊", "刷单"),
    "跑分代付": ("跑分", "代付"),
}


@dataclass(frozen=True)
class FallbackCleanedText:
    source_trace_id: str
    clean_text: str
    noise_score: float
    dedup_group_id: str
    quality_score: float = 0.0
    risk_score: float = 0.0
    risk_level: str = "NONE"
    risk_categories: list[str] = field(default_factory=list)
    risk_markers: list[str] = field(default_factory=list)
    text_entropy: float = 0.0

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DroppedRecord:
    source_trace_id: str
    reason: str
    noise_score: float = 0.0
    dedup_group_id: str | None = None
    similarity: float | None = None


@dataclass(frozen=True)
class RiskSignalProfile:
    risk_level: str = "NONE"
    risk_score: float = 0.0
    risk_categories: tuple[str, ...] = ()
    risk_markers: tuple[str, ...] = ()
    text_entropy: float = 0.0

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


CLEANED_SCHEMA_MODEL = None


def _load_cleaned_schema_model() -> type[Any]:
    global CLEANED_SCHEMA_MODEL
    if CLEANED_SCHEMA_MODEL is not None:
        return CLEANED_SCHEMA_MODEL
    try:
        CLEANED_SCHEMA_MODEL = getattr(import_module("storage.schemas"), "CleanedText")
    except Exception:
        CLEANED_SCHEMA_MODEL = FallbackCleanedText
    return CLEANED_SCHEMA_MODEL


def _schema_fields(model: type[Any]) -> set[str]:
    return set(getattr(model, "model_fields", {}) or getattr(model, "__annotations__", {}) or [])


def build_cleaned_text(
    *,
    source_trace_id: str,
    clean_text: str,
    noise_score: float,
    dedup_group_id: str,
    quality_score: float = 0.0,
    risk_score: float = 0.0,
    risk_level: str = "NONE",
    risk_categories: list[str] | tuple[str, ...] | None = None,
    risk_markers: list[str] | tuple[str, ...] | None = None,
    text_entropy: float = 0.0,
    cleaning_version: str = "cleaner_v2_riskaware",
) -> Any:
    payload = {
        "source_trace_id": source_trace_id,
        "clean_text": clean_text,
        "noise_score": noise_score,
        "dedup_group_id": dedup_group_id,
        "quality_score": quality_score,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_categories": list(risk_categories or ()),
        "risk_markers": list(risk_markers or ()),
        "text_entropy": text_entropy,
        "cleaning_version": cleaning_version,
    }
    model = _load_cleaned_schema_model()
    fields = _schema_fields(model)
    candidate = {key: value for key, value in payload.items() if not fields or key in fields}
    try:
        return model(**candidate)  # type: ignore[misc,operator]
    except Exception:
        return FallbackCleanedText(**payload)


def normalize_text(text: str | None) -> str:
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = ZERO_WIDTH_RE.sub("", normalized)
    normalized = WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()


def normalize_intel_text(text: str | None) -> str:
    """Normalize black/gray slang variants, homophones, and emojis for matching."""

    normalized = normalize_text(text)
    if not normalized:
        return ""
    for raw, target in BLACKGRAY_LITERAL_VARIANT_REPLACEMENTS:
        normalized = normalized.replace(raw, target)
    for pattern, replacement in BLACKGRAY_CONTEXT_VARIANT_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    normalized = WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()


def _is_signal_char(char: str) -> bool:
    return char.isalnum() or "\u4e00" <= char <= "\u9fff"


def _ordered_unique(values: list[str] | tuple[str, ...] | set[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in values or ():
        value = normalize_text(str(raw))
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(value)
    return tuple(ordered)


def calculate_noise_score(text: str) -> float:
    """Return a 0-1 noise estimate; higher means more likely pure garbage."""

    normalized = normalize_text(text)
    if not normalized:
        return 1.0

    visible = [char for char in normalized if not char.isspace()]
    if not visible:
        return 1.0

    signal_count = sum(1 for char in visible if _is_signal_char(char))
    replacement_ratio = sum(1 for char in visible if char == "�") / len(visible)
    signal_ratio = signal_count / len(visible)
    repeat_ratio = Counter(visible).most_common(1)[0][1] / len(visible)

    symbol_noise = 1.0 - signal_ratio
    repeated_noise = repeat_ratio if repeat_ratio >= 0.80 and len(visible) >= 8 else 0.0
    short_symbol_noise = 1.0 if len(visible) <= 4 and signal_count == 0 else 0.0
    noise_score = max(replacement_ratio, symbol_noise, repeated_noise, short_symbol_noise)
    return round(min(1.0, noise_score), 4)


def is_blank_or_garbled(text: str | None, *, threshold: float = 0.72) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return True
    if not any(_is_signal_char(char) for char in normalized):
        return True
    return calculate_noise_score(normalized) >= threshold


def shannon_entropy(text: str) -> float:
    normalized = normalize_text(text)
    if not normalized:
        return 0.0
    counts = Counter(char for char in normalized if not char.isspace())
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return round(-sum((count / total) * math.log2(count / total) for count in counts.values()), 4)


def canonicalize_for_dedup(text: str) -> str:
    """Canonical representation used for exact and near duplicate grouping."""

    normalized = normalize_intel_text(text).lower()
    return "".join(char for char in normalized if _is_signal_char(char))


def stable_dedup_group_id(canonical_text: str) -> str:
    digest = sha1(canonical_text.encode("utf-8")).hexdigest()[:16]
    return f"dedup:{digest}"


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    if len(text) <= n:
        return {text} if text else set()
    return {text[index : index + n] for index in range(len(text) - n + 1)}


def text_similarity(left: str, right: str) -> float:
    left_canon = canonicalize_for_dedup(left)
    right_canon = canonicalize_for_dedup(right)
    if not left_canon or not right_canon:
        return 0.0
    if left_canon == right_canon:
        return 1.0
    length_ratio = min(len(left_canon), len(right_canon)) / max(len(left_canon), len(right_canon))
    if length_ratio < 0.60:
        return 0.0

    left_grams = _char_ngrams(left_canon)
    right_grams = _char_ngrams(right_canon)
    jaccard = len(left_grams & right_grams) / len(left_grams | right_grams) if left_grams and right_grams else 0.0
    overlap = len(left_grams & right_grams) / min(len(left_grams), len(right_grams)) if left_grams and right_grams else 0.0
    sequence_score = 0.0
    if max(len(left_canon), len(right_canon)) <= 160:
        sequence_score = SequenceMatcher(None, left_canon, right_canon, autojunk=False).ratio()
    return round(max(sequence_score, jaccard, overlap), 4)


def detect_risk_signal_profile(
    text: str | None,
    *,
    extra_terms: list[str] | tuple[str, ...] | None = None,
) -> RiskSignalProfile:
    normalized = normalize_text(text)
    lowered_text = normalize_intel_text(normalized).lower()
    signal_terms = _ordered_unique(extra_terms)
    lowered_terms = {term.lower() for term in signal_terms}

    matched_categories: list[str] = []
    matched_markers: list[str] = []
    for category, markers in HIGH_RISK_MARKER_SETS.items():
        hits = [marker for marker in markers if marker.lower() in lowered_text]
        hint_hits = [hint for hint in HIGH_RISK_HINT_SETS.get(category, ()) if hint.lower() in lowered_terms]
        combined = _ordered_unique([*hits, *hint_hits])
        if combined:
            matched_categories.append(category)
            matched_markers.extend(combined)

    has_contact = bool(CONTACT_MARKER_RE.search(normalized))
    has_url = bool(URL_MARKER_RE.search(normalized))
    if has_contact:
        matched_markers.append("contact_handle")
    if has_url:
        matched_markers.append("destination_url")

    categories = _ordered_unique(matched_categories)
    markers = _ordered_unique(matched_markers)
    entropy = shannon_entropy(normalized)

    score = 0.0
    if categories:
        score += min(0.78, len(categories) * 0.22)
    if markers:
        score += min(0.24, len(markers) * 0.04)
    if has_contact:
        score += 0.12
    if has_url:
        score += 0.10
    if categories and has_contact and has_url:
        score += 0.12
    if "接码注册" in categories and has_contact:
        score += 0.08
    if "跑分代付" in categories and has_contact:
        score += 0.08
    if "诈骗引流" in categories and has_url:
        score += 0.06
    score = round(min(1.0, score), 4)

    if score >= 0.82:
        level = "CRITICAL"
    elif score >= 0.62:
        level = "HIGH"
    elif score >= 0.35:
        level = "MEDIUM"
    elif score > 0.0:
        level = "LOW"
    else:
        level = "NONE"

    return RiskSignalProfile(
        risk_level=level,
        risk_score=score,
        risk_categories=categories,
        risk_markers=markers,
        text_entropy=entropy,
    )


def detect_noise_reason(
    text: str | None,
    *,
    noise_score: float | None = None,
    risk_score: float = 0.0,
    min_entropy: float = 1.0,
    max_noise_score: float = 0.82,
) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return "empty_text"
    if not any(_is_signal_char(char) for char in normalized):
        return "blank_or_garbled"

    score = calculate_noise_score(normalized) if noise_score is None else noise_score
    entropy = shannon_entropy(normalized)
    lowered = normalized.lower()

    if score >= 0.72 and len(canonicalize_for_dedup(normalized)) < 12 and risk_score < 0.25:
        return "blank_or_garbled"
    if entropy < min_entropy and len(normalized) >= 8 and risk_score < 0.35:
        return "low_information_entropy"
    if score > max_noise_score and risk_score < 0.35:
        return "high_noise_score"
    if any(marker.lower() in lowered for marker in DEFENSIVE_CONTEXT_MARKERS):
        return "defensive_context_noise"
    if any(marker.lower() in lowered for marker in GENERIC_GUIDE_MARKERS) and risk_score < 0.55:
        return "generic_guide_noise"
    if len(canonicalize_for_dedup(normalized)) < 4 and risk_score < 0.25:
        return "low_signal_short_text"
    return None


def calculate_quality_score(
    text: str | None,
    *,
    noise_score: float,
    risk_score: float,
    entropy: float | None = None,
) -> float:
    normalized = normalize_text(text)
    canonical_length = len(canonicalize_for_dedup(normalized))
    entropy_score = min(1.0, (entropy if entropy is not None else shannon_entropy(normalized)) / 3.5)
    length_score = min(1.0, canonical_length / 80.0)
    quality = 0.20 + 0.28 * length_score + 0.22 * entropy_score + 0.30 * risk_score - 0.45 * noise_score
    return round(max(0.0, min(1.0, quality)), 4)


class DedupIndex:
    """Stateful exact + near duplicate index for one cleaner run."""

    def __init__(self, *, threshold: float = NEAR_DUPLICATE_THRESHOLD) -> None:
        self.threshold = threshold
        self._exact: dict[str, str] = {}
        self._representatives: list[tuple[str, str, set[str], int]] = []
        self._prefix_index: dict[str, set[int]] = {}
        self._suffix_index: dict[str, set[int]] = {}
        self._gram_index: dict[str, set[int]] = {}

    def assign(self, text: str) -> tuple[str, bool, float]:
        canonical = canonicalize_for_dedup(text)
        if not canonical:
            return stable_dedup_group_id("empty"), False, 0.0
        if canonical in self._exact:
            return self._exact[canonical], True, 1.0

        grams = _char_ngrams(canonical)
        candidate_indexes: set[int] = set()
        candidate_indexes.update(self._prefix_index.get(canonical[:6], set()))
        candidate_indexes.update(self._suffix_index.get(canonical[-6:], set()))

        gram_candidates = sorted(
            ((len(self._gram_index.get(gram, ())), gram) for gram in grams if gram in self._gram_index),
            key=lambda item: item[0],
        )
        for _size, gram in gram_candidates[:12]:
            candidate_indexes.update(self._gram_index.get(gram, set()))
            if len(candidate_indexes) >= 128:
                break

        for index in candidate_indexes:
            group_id, representative, _rep_grams, representative_length = self._representatives[index]
            length_ratio = min(len(canonical), representative_length) / max(len(canonical), representative_length)
            if length_ratio < 0.60:
                continue
            score = text_similarity(canonical, representative)
            if score >= self.threshold:
                self._exact[canonical] = group_id
                return group_id, True, score

        group_id = stable_dedup_group_id(canonical)
        self._exact[canonical] = group_id
        representative_index = len(self._representatives)
        self._representatives.append((group_id, canonical, grams, len(canonical)))
        self._prefix_index.setdefault(canonical[:6], set()).add(representative_index)
        self._suffix_index.setdefault(canonical[-6:], set()).add(representative_index)
        for gram in grams:
            self._gram_index.setdefault(gram, set()).add(representative_index)
        return group_id, False, 0.0


__all__ = [
    "DedupIndex",
    "DroppedRecord",
    "FallbackCleanedText",
    "RiskSignalProfile",
    "MAX_CLEAN_TEXT_CHARS",
    "NEAR_DUPLICATE_THRESHOLD",
    "build_cleaned_text",
    "calculate_quality_score",
    "calculate_noise_score",
    "canonicalize_for_dedup",
    "detect_noise_reason",
    "detect_risk_signal_profile",
    "is_blank_or_garbled",
    "normalize_text",
    "shannon_entropy",
    "stable_dedup_group_id",
    "text_similarity",
]
