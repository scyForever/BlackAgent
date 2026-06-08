"""Source-aware entity filtering and high-value ordering."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from src.cleaner.text_filter import normalize_text


PSEUDO_VALUES = {
    "image",
    "channel",
    "feedback",
    "follow",
    "follow us",
    "linkedin",
    "twitter",
    "home",
    "https",
    "http",
}

IMAGE_OR_TEMPLATE_URL_MARKERS = (
    "/logo.",
    "logo.png",
    "logo.jpg",
    "logo.jpeg",
    "logo.webp",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    "static.",
    "cdn.",
)


def filter_and_order_entities(entities: Iterable[Any], record: Mapping[str, Any] | Any) -> list[Any]:
    """Drop page-template entities and sort high-value entities first."""

    text = _record_text(record)
    source_urls = _source_urls(record)
    filtered: list[Any] = []
    for entity in entities:
        normalized = _contextual_entity(entity, text)
        if _is_pseudo_entity(normalized, text, source_urls):
            continue
        filtered.append(normalized)
    return sorted(filtered, key=lambda item: (_entity_priority(item), _offset(item), _entity_type(item), _normalized_value(item).lower()))


def _contextual_entity(entity: Any, text: str) -> Any:
    entity_type = _entity_type(entity)
    normalized = _normalized_value(entity)
    raw = _entity_value(entity)
    local_prefix = text[max(0, _offset(entity) - 16) : _offset(entity)].lower()
    if entity_type == "contact" and normalized and ":" not in normalized:
        if any(marker in local_prefix for marker in ("telegram", "飞机", "纸飞机", "@")) or _high_value_contact_context(text, entity):
            return _replace_entity(
                entity,
                entity_type="contact",
                normalized_value=f"Telegram:{normalized.lstrip('@')}",
                masked_value=_mask_sensitive(f"Telegram:{normalized.lstrip('@')}"),
                sensitivity_level="sensitive",
            )
    if entity_type == "invite_code" and any(marker in local_prefix for marker in ("uid", "用户id", "账号", "账户")):
        return _replace_entity(
            entity,
            entity_type="account",
            normalized_value=normalized or raw,
            masked_value=_mask_sensitive(normalized or raw),
            sensitivity_level="sensitive",
        )
    if entity_type == "contact" and normalized.lower().startswith("wechat:") and raw.lower().startswith("wx"):
        return _replace_entity(
            entity,
            entity_type="contact",
            normalized_value=f"WeChat:{raw}",
            masked_value=_mask_sensitive(f"WeChat:{raw}"),
            sensitivity_level="sensitive",
        )
    return entity


def _is_pseudo_entity(entity: Any, text: str, source_urls: set[str]) -> bool:
    value = _normalized_value(entity).strip()
    lowered = value.lower().strip()
    if not lowered or lowered in PSEUDO_VALUES:
        return True
    entity_type = _entity_type(entity)
    if entity_type == "slang_term" and lowered in PSEUDO_VALUES:
        return True
    if entity_type == "url":
        normalized_url = _normalize_url_for_compare(value)
        if normalized_url in source_urls:
            return True
        if any(marker in lowered for marker in IMAGE_OR_TEMPLATE_URL_MARKERS):
            if _looks_like_page_boilerplate_context(text, _offset(entity)):
                return True
    if entity_type == "contact" and lowered.startswith("telegram:"):
        if _looks_like_neutral_channel_context(text, _offset(entity)):
            return True
    return False


def _entity_priority(entity: Any) -> int:
    entity_type = _entity_type(entity)
    value = _normalized_value(entity).lower()
    if entity_type == "contact" and value.startswith(("telegram:", "wechat:", "qq:")):
        return 0
    if entity_type in {"url", "domain"}:
        return 1
    if entity_type == "account":
        return 2
    if entity_type == "invite_code":
        return 3
    if entity_type == "settlement":
        return 4
    if entity_type == "tool_name":
        return 5
    if entity_type == "contact":
        return 6
    return 9


def _looks_like_page_boilerplate_context(text: str, start: int) -> bool:
    window = normalize_text(text[max(0, start - 64) : start + 64]).lower()
    return any(marker in window for marker in ("image", "logo", "follow us", "linkedin", "twitter", "home", "channel"))


def _looks_like_neutral_channel_context(text: str, start: int) -> bool:
    window = normalize_text(text[max(0, start - 32) : start + 32]).lower()
    if any(marker in window for marker in ("出售", "接单", "上车", "低价", "联系", "客服", "咨询", "价格")):
        return False
    return "channel" in window or "follow us" in window


def _source_urls(record: Mapping[str, Any] | Any) -> set[str]:
    if not isinstance(record, Mapping):
        return set()
    urls = set()
    for field in ("source_url", "query_url_template", "page_url", "search_query_url", "raw_payload_uri"):
        value = str(record.get(field) or "").strip()
        if value:
            urls.add(_normalize_url_for_compare(value))
    return urls


def _normalize_url_for_compare(value: str) -> str:
    text = str(value or "").strip().lower().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme:
        return parsed._replace(query="", fragment="").geturl().rstrip("/")
    return text


def _record_text(record: Mapping[str, Any] | Any) -> str:
    if isinstance(record, Mapping):
        return normalize_text(str(record.get("clean_text") or record.get("content_text") or record.get("text") or ""))
    return normalize_text(str(record))


def _replace_entity(entity: Any, **updates: Any) -> Any:
    entity_type = str(updates.get("entity_type") or _entity_type(entity) or "unknown").strip().lower() or "unknown"
    normalized = str(updates.get("normalized_value") or _normalized_value(entity)).strip()
    if normalized:
        updates.setdefault("canonical_hash", _canonical_hash(entity_type, normalized))
    try:
        return replace(entity, **updates)
    except TypeError:
        if hasattr(entity, "model_copy"):
            return entity.model_copy(update=updates)
        data = dict(getattr(entity, "__dict__", {}))
        data.update(updates)
        return data


def _mask_sensitive(value: str) -> str:
    if ":" in value:
        prefix, tail = value.split(":", 1)
        return f"{prefix}:***{tail[-2:]}" if len(tail) > 2 else f"{prefix}:***"
    return f"***{value[-2:]}" if len(value) > 2 else "***"


def _high_value_contact_context(text: str, entity: Any) -> bool:
    window = normalize_text(text[max(0, _offset(entity) - 48) : _offset(entity) + 64]).lower()
    return any(marker in window for marker in ("出售", "接单", "上车", "低价", "联系", "客服", "咨询", "价格", "引流", "群控"))


def _canonical_hash(entity_type: str, normalized_value: str) -> str:
    return hashlib.sha256(f"{entity_type}:{normalized_value.lower()}".encode("utf-8")).hexdigest()


def _entity_type(entity: Any) -> str:
    return str(getattr(entity, "entity_type", "") or _entity_mapping_value(entity, "entity_type")).strip().lower()


def _entity_value(entity: Any) -> str:
    return str(getattr(entity, "entity_value", "") or _entity_mapping_value(entity, "entity_value") or "").strip()


def _normalized_value(entity: Any) -> str:
    return str(getattr(entity, "normalized_value", "") or _entity_mapping_value(entity, "normalized_value") or _entity_value(entity)).strip()


def _offset(entity: Any) -> int:
    try:
        return int(getattr(entity, "start_offset", None) if getattr(entity, "start_offset", None) is not None else _entity_mapping_value(entity, "start_offset") or 0)
    except (TypeError, ValueError):
        return 0


def _entity_mapping_value(entity: Any, key: str) -> Any:
    return entity.get(key) if isinstance(entity, Mapping) else None


__all__ = ["filter_and_order_entities"]
