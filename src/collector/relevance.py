"""Keyword and theme-based relevance filtering for black/gray raw collection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import re
from typing import Iterable

from src.cleaner.text_filter import normalize_intel_text, normalize_text
from src.config_loader import load_yaml_file, resolve_project_path


DEFAULT_BLACKGRAY_INCLUDE_KEYWORDS: tuple[str, ...] = (
    "诈骗引流",
    "接码",
    "跑分",
    "刷单",
    "刷单作弊",
    "补单",
    "放单",
    "返佣",
    "返利",
    "高佣",
    "日结",
    "引流",
    "拉新",
    "私域导流",
    "引流到私域",
    "加v",
    "加微",
    "加vx",
    "拉群",
    "私聊进群",
    "落地页",
    "点赞任务",
    "关注任务",
    "做任务",
    "垫付",
    "垫付单",
    "代付",
    "刷流水",
    "连单",
    "卡单",
    "手工单",
    "账号交易",
    "帐号交易",
    "账号买卖",
    "帐号买卖",
    "实名号",
    "白号",
    "老号",
    "养号",
    "众包任务",
    "众包",
    "真人众包",
    "接单",
    "任务单",
    "外包任务",
    "拉人",
    "群发",
    "采集群成员",
    "收号",
    "卖号",
    "协议号",
    "群控",
    "脚本",
    "userbot",
    "automation",
    "爬虫",
    "卡密",
    "批量注册",
    "改机",
    "外挂",
    "打粉",
)

DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS: tuple[str, ...] = (
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

DEFAULT_THEME_SYNONYM_CONFIG_PATH = "config/theme_synonyms.yaml"
CROWDSOURCING_THEME = "众包任务"
WEAK_CROWDSOURCING_TERMS: tuple[str, ...] = (
    "automation",
    "userbot",
    "telethon",
    "pyrogram",
    "marketing",
    "lead",
    "dm",
)
WEAK_CROWDSOURCING_SUPPORT_MARKERS: tuple[str, ...] = (
    "群发",
    "拉人",
    "拉群",
    "采集群成员",
    "群成员采集",
    "批量私信",
    "工作室",
    "接单",
    "接任务",
    "承接",
    "定制",
    "搭建",
    "出售",
    "代发",
    "代聊",
    "代做",
    "代开",
    "价格",
    "报价",
    "联系客服",
    "唯一客服",
    "获客",
    "引流",
)
ASCII_TOKEN_RE = re.compile(r"^[a-z0-9_-]+(?:\s+[a-z0-9_-]+)*$", re.IGNORECASE)
ASCII_BOUNDARY_CLASS = "a-z0-9_"


@dataclass(frozen=True)
class KeywordRelevanceDecision:
    relevant: bool
    matched_keywords: tuple[str, ...]
    excluded_keywords: tuple[str, ...]
    hit_count: int
    matched_themes: tuple[str, ...] = ()
    excluded_themes: tuple[str, ...] = ()
    policy_version: str = "keyword_relevance_v6"

    def model_dump(self) -> dict[str, object]:
        return asdict(self)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in values:
        value = normalize_text(str(raw))
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(value)
    return tuple(ordered)


def normalize_keywords(values: Iterable[str] | None) -> tuple[str, ...]:
    return _ordered_unique(values or ())


@lru_cache(maxsize=512)
def _compiled_ascii_term_pattern(term: str) -> re.Pattern[str] | None:
    normalized = normalize_text(term)
    if not normalized or not ASCII_TOKEN_RE.fullmatch(normalized):
        return None
    escaped = re.escape(normalized.lower()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![{ASCII_BOUNDARY_CLASS}]){escaped}(?![{ASCII_BOUNDARY_CLASS}])", re.IGNORECASE)


def _contains_term(normalized_text: str, term: str) -> bool:
    normalized_term = normalize_intel_text(term)
    if not normalized_term:
        return False
    pattern = _compiled_ascii_term_pattern(normalized_term)
    if pattern is not None:
        return bool(pattern.search(normalized_text))
    return normalized_term.lower() in normalized_text


@lru_cache(maxsize=1)
def load_theme_synonym_registry() -> dict[str, dict[str, tuple[str, ...]]]:
    payload = load_yaml_file(resolve_project_path(DEFAULT_THEME_SYNONYM_CONFIG_PATH))
    themes_payload = payload.get("themes") or {}
    if not isinstance(themes_payload, dict):
        raise ValueError("theme_synonyms.yaml must contain a mapping 'themes'")

    registry: dict[str, dict[str, tuple[str, ...]]] = {}
    for raw_theme, raw_cfg in themes_payload.items():
        theme = normalize_text(str(raw_theme))
        if not theme:
            continue
        match_terms: Iterable[str]
        search_terms: Iterable[str]
        if isinstance(raw_cfg, dict):
            match_terms = raw_cfg.get("match_terms") or ()
            search_terms = raw_cfg.get("search_terms") or ()
            variant_search_terms = raw_cfg.get("variant_search_terms") or ()
        else:
            match_terms = raw_cfg or ()
            search_terms = ()
            variant_search_terms = ()
        registry[theme] = {
            "match_terms": _ordered_unique((theme, *(str(item) for item in match_terms))),
            "search_terms": _ordered_unique(str(item) for item in search_terms),
            "variant_search_terms": _ordered_unique(str(item) for item in variant_search_terms),
        }
    return registry


@lru_cache(maxsize=1)
def _theme_term_to_canonical() -> dict[str, str]:
    registry = load_theme_synonym_registry()
    return {
        normalize_text(term).lower(): theme
        for theme, cfg in registry.items()
        for term in cfg.get("match_terms", ())
    }


def normalize_themes(values: Iterable[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    alias_map = _theme_term_to_canonical()
    for raw in values or ():
        candidate = normalize_text(str(raw))
        if not candidate:
            continue
        canonical = alias_map.get(candidate.lower(), candidate)
        lowered = canonical.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(canonical)
    return tuple(normalized)


def _theme_variants(theme: str) -> tuple[str, ...]:
    normalized_theme = normalize_text(theme)
    canonical = _theme_term_to_canonical().get(normalized_theme.lower(), normalized_theme)
    cfg = load_theme_synonym_registry().get(canonical, {})
    variants = cfg.get("match_terms")
    if variants:
        return _ordered_unique(variants)
    return (canonical,)


def get_theme_search_terms(theme: str, *, limit: int | None = None) -> tuple[str, ...]:
    return tuple(item["term"] for item in get_theme_search_variants(theme, limit=limit))


def get_theme_search_variants(theme: str, *, limit: int | None = None) -> tuple[dict[str, str], ...]:
    normalized_theme = normalize_text(theme)
    canonical = _theme_term_to_canonical().get(normalized_theme.lower(), normalized_theme)
    cfg = load_theme_synonym_registry().get(canonical, {})
    core_terms = _ordered_unique(cfg.get("search_terms") or cfg.get("match_terms") or (canonical,))
    variant_terms = _ordered_unique(cfg.get("variant_search_terms") or ())
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for stage, terms in (("core", core_terms), ("variant", variant_terms)):
        for term in terms:
            lowered = term.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            items.append({"term": term, "stage": stage})
    if limit is not None and limit > 0:
        items = items[:limit]
    return tuple(items)


def _match_theme_variants(normalized_text: str, themes: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    matched_themes: list[str] = []
    matched_terms: list[str] = []
    for theme in normalize_themes(themes):
        variants = _theme_variants(theme)
        hits = [variant for variant in variants if _contains_term(normalized_text, variant)]
        if not hits:
            continue
        matched_themes.append(theme)
        matched_terms.extend(hits)
    return _ordered_unique(matched_themes), _ordered_unique(matched_terms)


@lru_cache(maxsize=1)
def _crowdsourcing_theme_term_lowers() -> tuple[str, ...]:
    return tuple(normalize_text(term).lower() for term in _theme_variants(CROWDSOURCING_THEME))


@lru_cache(maxsize=1)
def _weak_crowdsourcing_term_lowers() -> tuple[str, ...]:
    return tuple(normalize_text(term).lower() for term in WEAK_CROWDSOURCING_TERMS)


def _keep_crowdsourcing_theme(normalized_text: str, crowd_hits: Iterable[str]) -> bool:
    weak_hits = set(_weak_crowdsourcing_term_lowers())
    if any(normalize_text(term).lower() not in weak_hits for term in crowd_hits):
        return True
    return any(_contains_term(normalized_text, marker) for marker in WEAK_CROWDSOURCING_SUPPORT_MARKERS)


def _prune_crowdsourcing_theme(
    normalized_text: str,
    matched_themes: tuple[str, ...],
    matched_theme_terms: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if CROWDSOURCING_THEME not in matched_themes:
        return matched_themes, matched_theme_terms

    crowd_term_lowers = set(_crowdsourcing_theme_term_lowers())
    crowd_hits = [term for term in matched_theme_terms if normalize_text(term).lower() in crowd_term_lowers]
    if not crowd_hits or _keep_crowdsourcing_theme(normalized_text, crowd_hits):
        return matched_themes, matched_theme_terms

    filtered_themes = tuple(theme for theme in matched_themes if theme != CROWDSOURCING_THEME)
    filtered_terms = tuple(
        term for term in matched_theme_terms if normalize_text(term).lower() not in crowd_term_lowers
    )
    return filtered_themes, filtered_terms


def decide_text_relevance(
    text: str | None,
    *,
    include_keywords: Iterable[str] | None = None,
    exclude_keywords: Iterable[str] | None = None,
    include_themes: Iterable[str] | None = None,
    exclude_themes: Iterable[str] | None = None,
    min_keyword_hits: int = 1,
) -> KeywordRelevanceDecision:
    normalized_text = normalize_intel_text(text).lower()
    include = normalize_keywords(include_keywords)
    exclude = normalize_keywords(exclude_keywords)
    include_theme_names = normalize_themes(include_themes)
    exclude_theme_names = normalize_themes(exclude_themes)

    matched_exact = tuple(keyword for keyword in include if _contains_term(normalized_text, keyword))
    excluded_exact = tuple(keyword for keyword in exclude if _contains_term(normalized_text, keyword))
    matched_themes, matched_theme_terms = _match_theme_variants(normalized_text, include_theme_names)
    excluded_themes, excluded_theme_terms = _match_theme_variants(normalized_text, exclude_theme_names)
    matched_themes, matched_theme_terms = _prune_crowdsourcing_theme(normalized_text, matched_themes, matched_theme_terms)

    matched = _ordered_unique((*matched_exact, *matched_theme_terms))
    excluded = _ordered_unique((*excluded_exact, *excluded_theme_terms))
    hit_count = len(matched)
    has_positive_rules = bool(include or include_theme_names)
    relevant = not excluded and (hit_count >= max(1, int(min_keyword_hits or 1)) if has_positive_rules else bool(normalized_text))
    return KeywordRelevanceDecision(
        relevant=relevant,
        matched_keywords=matched,
        excluded_keywords=excluded,
        hit_count=hit_count,
        matched_themes=matched_themes,
        excluded_themes=excluded_themes,
    )


__all__ = [
    "DEFAULT_BLACKGRAY_INCLUDE_KEYWORDS",
    "DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS",
    "KeywordRelevanceDecision",
    "decide_text_relevance",
    "get_theme_search_variants",
    "get_theme_search_terms",
    "load_theme_synonym_registry",
    "normalize_keywords",
    "normalize_themes",
]
