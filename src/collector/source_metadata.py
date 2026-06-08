"""Collection metadata helpers for evidence-grade source intake.

The helpers in this module deliberately stay deterministic and dependency-free.
They annotate raw records with provenance, incremental cursor, quality, and
failure-reason fields so later phase reports can distinguish:

* what was collected from an authorized source,
* how fresh/deduplicable the record is,
* whether the source is over-represented, and
* why a live source attempt failed without encouraging bypass behavior.
"""

from __future__ import annotations

import socket
from hashlib import sha256
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib.parse import urlparse


SOURCE_ACCESS_TYPES = {
    "public_compliant",
    "authorized_partner",
    "internal_authorized",
    "loopback_demo",
    "manual_upload",
}

SOURCE_CLASS_ALIASES: dict[str, set[str]] = {
    "im_or_group": {"im", "group", "telegram", "tg"},
    "social_or_forum": {
        "social",
        "forum",
        "news",
        "blog",
        "tieba",
        "short_video",
        "x",
        "twitter",
        "article",
        "public_account",
        "wechat_public",
        "wechat_public_account",
        "wechat",
        "rss",
        "html_article",
    },
    "vertical_or_technical": {"vertical", "threat_intel", "technical", "techforum", "technical_community"},
}

ARTICLE_SOURCE_MARKERS = {
    "article",
    "public_account",
    "wechat_public",
    "wechat_public_account",
    "wechat",
    "rss",
    "html_article",
}

SECONDHAND_SOURCE_MARKERS = {
    "secondhand",
    "second_hand",
    "used_goods",
    "marketplace",
    "二手",
    "闲鱼",
    "交易市场",
}

CROWDSOURCING_SOURCE_MARKERS = {
    "crowdsourcing",
    "crowd",
    "task_platform",
    "task_market",
    "众包",
    "任务平台",
    "接单平台",
}

TOOL_MARKERS = {
    "群控",
    "脚本",
    "协议号",
    "卡密",
    "拉群端",
    "开控",
    "软件",
    "自动注册",
    "改机",
    "外挂",
    "工具交易",
}

FAILURE_REASON_ALIASES = {
    "403": "403",
    "429": "429",
    "captcha": "captcha",
    "验证码": "captcha",
    "login_required": "login_required",
    "401": "login_required",
    "robots": "robots_uncertain",
    "terms": "robots_uncertain",
    "timeout": "timeout",
    "timed out": "timeout",
}


def build_collection_metadata(record: Mapping[str, Any], *, content_text: str, now_iso: str) -> dict[str, Any]:
    """Return deterministic collection metadata for one raw row."""

    from src.cleaner.text_filter import normalize_text

    data = {str(key): value for key, value in record.items()}
    source_name = str(data.get("source_name") or "unknown_source")
    source_type = str(data.get("source_type") or data.get("type") or "unknown")
    source_url = str(data.get("source_url") or data.get("url") or "")
    crawl_time = str(data.get("crawl_time") or data.get("created_at") or now_iso)
    publish_time = str(data.get("publish_time") or crawl_time)
    content_hash = str(data.get("content_hash") or sha256(normalize_text(content_text).encode("utf-8")).hexdigest())
    snapshot_date = crawl_time[:10] if len(crawl_time) >= 10 else now_iso[:10]
    snapshot_seed = sha256(f"{source_name}|{source_type}|{source_url}|{snapshot_date}".encode("utf-8")).hexdigest()[:12]

    return {
        "content_hash": content_hash,
        "last_seen_at": str(data.get("last_seen_at") or crawl_time),
        "last_cursor": str(
            data.get("last_cursor")
            or data.get("feed_row_index")
            or data.get("result_rank")
            or data.get("message_id")
            or data.get("cursor")
            or publish_time
            or content_hash[:16]
        ),
        "source_snapshot_id": str(data.get("source_snapshot_id") or f"{source_name}:{snapshot_date}:{snapshot_seed}"),
        "source_access_type": normalize_source_access_type(
            data.get("source_access_type"),
            legal_basis=data.get("legal_basis"),
            source_name=source_name,
            source_url=source_url,
        ),
        "source_class": source_class_for_record(data),
        "collection_quality": build_collection_quality_profile(data, content_text=content_text),
    }


def build_collection_quality_profile(record: Mapping[str, Any], *, content_text: str) -> dict[str, Any]:
    """Score pre-cleaning collection quality and explain the inputs."""

    from src.cleaner.text_filter import (
        calculate_noise_score,
        calculate_quality_score,
        detect_noise_reason,
        detect_risk_signal_profile,
        normalize_text,
    )

    extra_terms = [
        str(item)
        for key in ("matched_keywords", "matched_themes")
        for item in (record.get(key) or [])
        if str(item).strip()
    ]
    normalized = normalize_text(content_text)
    noise_score = calculate_noise_score(normalized)
    risk_profile = detect_risk_signal_profile(normalized, extra_terms=extra_terms)
    risk_markers = list(risk_profile.risk_markers)
    contact_or_link_hits = sum(1 for marker in risk_markers if marker in {"contact_handle", "destination_url"})
    tool_word_hits = sum(1 for marker in risk_markers if marker in TOOL_MARKERS)
    slang_hits = max(0, len([marker for marker in risk_markers if marker not in {"contact_handle", "destination_url"}]))
    noise_reason = detect_noise_reason(normalized, noise_score=noise_score, risk_score=risk_profile.risk_score)
    defensive_probability = 0.85 if noise_reason == "defensive_context_noise" else 0.0
    tutorial_probability = 0.75 if noise_reason == "generic_guide_noise" else 0.0
    duplicate_probability = _duplicate_probability(record)
    collection_quality_score = calculate_quality_score(
        normalized,
        noise_score=noise_score,
        risk_score=risk_profile.risk_score,
        entropy=risk_profile.text_entropy,
    )
    return {
        "slang_hit_count": slang_hits,
        "contact_link_tool_hit_count": contact_or_link_hits + tool_word_hits,
        "duplicate_probability": duplicate_probability,
        "defensive_context_probability": defensive_probability,
        "spam_or_tutorial_noise_probability": round(max(noise_score, tutorial_probability), 4),
        "risk_score": risk_profile.risk_score,
        "risk_level": risk_profile.risk_level,
        "risk_categories": list(risk_profile.risk_categories),
        "risk_markers": risk_markers,
        "quality_score": collection_quality_score,
        "noise_reason": noise_reason,
        "quality_version": "collection_quality_v1",
    }


def normalize_source_access_type(
    value: Any,
    *,
    legal_basis: Any = None,
    source_name: str = "",
    source_url: str = "",
) -> str:
    """Normalize source provenance into the five答辩-friendly buckets."""

    raw = str(value or "").strip().lower()
    if raw in SOURCE_ACCESS_TYPES:
        return raw
    basis = str(getattr(legal_basis, "value", legal_basis) or "").strip().upper()
    parsed_host = (urlparse(str(source_url or "")).hostname or "").lower()
    name_text = str(source_name or "").lower()
    if "loopback" in name_text or parsed_host in {"127.0.0.1", "localhost", "::1"}:
        return "loopback_demo"
    if "manual" in name_text:
        return "manual_upload"
    if basis == "PUBLIC_COMPLIANT_DATA":
        return "public_compliant"
    if basis in {"AUTHORIZED_PARTNER", "THIRD_PARTY_AUTHORIZED_FEED"}:
        return "authorized_partner"
    if basis == "INTERNAL_AUTHORIZED_SOURCE":
        return "internal_authorized"
    return "manual_upload"


def source_class_for_record(record: Mapping[str, Any]) -> str:
    """Map a row/source definition to the collection quota class."""

    existing = str(record.get("source_class") or "").strip().lower()
    if existing in SOURCE_CLASS_ALIASES or existing == "other_authorized":
        return existing
    source_type = str(record.get("source_type") or record.get("type") or "").strip().lower()
    platform = str(record.get("platform") or "").strip().lower()
    source_name = str(record.get("source_name") or record.get("name") or "").strip().lower()
    if platform in {"x", "twitter"} or source_name.startswith("x_") or "x_blackgray" in source_name:
        return "social_or_forum"
    if is_article_source_record(record):
        return "social_or_forum"
    if platform in {"telegram", "tg"}:
        return "im_or_group"
    markers = {source_type, platform}
    for source_class, aliases in SOURCE_CLASS_ALIASES.items():
        if markers & aliases:
            return source_class
    if "telegram" in source_name:
        return "im_or_group"
    if "tg_" in source_name or source_name.startswith("tg"):
        return "im_or_group"
    if any(marker in source_name for marker in ("forum", "tieba", "x_blackgray", "social")):
        return "social_or_forum"
    if any(marker in source_name for marker in ("tech", "vertical", "market", "threat")):
        return "vertical_or_technical"
    return "other_authorized"


def is_article_source_record(record: Mapping[str, Any]) -> bool:
    """Return True for stable public article/public-account intake sources."""

    source_type = str(record.get("source_type") or record.get("type") or "").strip().lower()
    platform = str(record.get("platform") or "").strip().lower()
    return bool({source_type, platform} & ARTICLE_SOURCE_MARKERS)


def source_quota_groups_for_record(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return granular source quota groups while preserving broad source classes."""

    explicit = record.get("source_quota_groups") or record.get("quota_groups") or record.get("source_quota_group")
    groups: list[str] = []
    if isinstance(explicit, str):
        groups.extend(item.strip() for item in explicit.split(","))
    elif explicit:
        try:
            groups.extend(str(item).strip() for item in explicit)
        except TypeError:
            groups.append(str(explicit).strip())

    existing_class = str(record.get("source_class") or "").strip().lower()
    if existing_class in {
        "public_account_or_article",
        "public_account_article",
        "secondhand_market",
        "crowdsourcing_platform",
    }:
        groups.append(existing_class)

    text = " ".join(
        str(record.get(field) or "").strip().lower()
        for field in ("source_type", "type", "platform", "source_name", "name", "source_url", "url")
    )
    has_granular_non_vertical = any(marker in text for marker in SECONDHAND_SOURCE_MARKERS | CROWDSOURCING_SOURCE_MARKERS)
    if source_class_for_record(record) == "vertical_or_technical" and not has_granular_non_vertical:
        groups.append("vertical_or_technical")
    if is_article_source_record(record):
        groups.append("public_account_or_article")
        groups.append("public_account_article")
    if any(marker in text for marker in SECONDHAND_SOURCE_MARKERS):
        groups.append("secondhand_market")
    if any(marker in text for marker in CROWDSOURCING_SOURCE_MARKERS):
        groups.append("crowdsourcing_platform")
    return tuple(dict.fromkeys(group for group in groups if group))


def classify_collection_failure(exc_or_text: Any) -> str:
    """Return a stable failure reason for compliance audit reports."""

    if isinstance(exc_or_text, urllib_error.HTTPError):
        if exc_or_text.code in {401, 403}:
            return "login_required" if exc_or_text.code == 401 else "403"
        if exc_or_text.code == 429:
            return "429"
        return f"http_{exc_or_text.code}"
    if isinstance(exc_or_text, (TimeoutError, socket.timeout)):
        return "timeout"
    text = f"{type(exc_or_text).__name__}:{exc_or_text}".lower()
    for marker, reason in FAILURE_REASON_ALIASES.items():
        if marker in text:
            return reason
    return "other"


def _duplicate_probability(record: Mapping[str, Any]) -> float:
    for key in ("duplicate_probability", "duplicate_rate"):
        value = record.get(key)
        if value not in (None, ""):
            try:
                return round(max(0.0, min(1.0, float(value))), 4)
            except (TypeError, ValueError):
                return 0.0
    if bool(record.get("is_duplicate")):
        return 1.0
    return 0.0


__all__ = [
    "ARTICLE_SOURCE_MARKERS",
    "SOURCE_ACCESS_TYPES",
    "CROWDSOURCING_SOURCE_MARKERS",
    "SECONDHAND_SOURCE_MARKERS",
    "build_collection_metadata",
    "build_collection_quality_profile",
    "classify_collection_failure",
    "is_article_source_record",
    "normalize_source_access_type",
    "source_quota_groups_for_record",
    "source_class_for_record",
]
