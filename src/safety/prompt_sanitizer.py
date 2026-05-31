"""Minimal, masked payloads for LLM prompts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .pii_masker import PIIMasker


SECRET_FIELD_FRAGMENTS = (
    "auth",
    "authorization",
    "cookie",
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "credential",
    "headers",
)

SOURCE_PROMPT_FIELDS = (
    "source_name",
    "source_type",
    "query_theme",
    "query_term",
    "query_term_stage",
    "search_query",
    "legal_basis",
    "collection_layer",
)

CLUE_PROMPT_FIELDS = (
    "clue_type",
    "key",
    "risk_category",
    "confidence",
    "quality_score",
    "quality_level",
    "threshold_reason",
)

ENTITY_PROMPT_FIELDS = ("entity_type", "normalized_value", "entity_value", "confidence", "context_relevance")


def sanitize_source_for_llm(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return source metadata needed for query rewrite, excluding secrets."""

    payload = {field: source.get(field) for field in SOURCE_PROMPT_FIELDS if source.get(field) not in (None, "")}
    payload["has_query_url_template"] = bool(str(source.get("query_url_template") or "").strip())
    domain = _safe_domain(source.get("source_url") or source.get("query_url_template"))
    if domain:
        payload["source_domain"] = domain
    return payload


def sanitize_entity_for_llm(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Return an entity card with contact/account values masked or hashed."""

    payload = {field: entity.get(field) for field in ENTITY_PROMPT_FIELDS if entity.get(field) not in (None, "")}
    entity_type = str(payload.get("entity_type") or "").strip().lower()
    raw_value = str(payload.get("normalized_value") or payload.get("entity_value") or "")
    if entity_type in {"contact", "account"}:
        payload.pop("normalized_value", None)
        payload.pop("entity_value", None)
        payload["value_hash"] = _short_hash(raw_value)
        payload["value_preview"] = PIIMasker().mask_text(raw_value)
    elif raw_value:
        payload["normalized_value"] = PIIMasker().mask_text(raw_value)
        payload.pop("entity_value", None)
    return payload


def sanitize_clue_for_llm(clue: Mapping[str, Any], *, stable_id: bool = True) -> dict[str, Any]:
    """Return the smallest useful clue card for refinement prompts."""

    payload = {field: clue.get(field) for field in CLUE_PROMPT_FIELDS if clue.get(field) not in (None, "")}
    if "key" in payload:
        payload["key"] = _mask_or_hash_value(payload["key"])
    payload["clue_id"] = stable_clue_card_id(clue) if stable_id else str(clue.get("clue_id") or "unknown_clue")
    payload["evidence_trace_ids"] = _string_list(clue.get("evidence_trace_ids"))[:8]
    payload["source_names"] = _string_list(clue.get("source_names"))[:6]
    entity_values = [_mask_or_hash_value(value) for value in _string_list(clue.get("entity_values"))[:8]]
    if entity_values:
        payload["entity_values"] = entity_values
    quality = clue.get("quality") if isinstance(clue.get("quality"), Mapping) else {}
    if quality:
        payload["quality"] = {
            key: quality.get(key)
            for key in ("pass_threshold", "review_required", "reasons")
            if quality.get(key) not in (None, "")
        }
    return payload


def stable_clue_card_id(clue: Mapping[str, Any]) -> str:
    stable_payload = {
        "clue_type": clue.get("clue_type"),
        "key": clue.get("key"),
        "risk_category": clue.get("risk_category"),
        "evidence_trace_ids": sorted(_string_list(clue.get("evidence_trace_ids"))),
        "source_names": sorted(_string_list(clue.get("source_names"))),
        "entity_values": sorted(_mask_or_hash_value(value) for value in _string_list(clue.get("entity_values"))),
    }
    return "card_" + _short_hash(stable_payload)


def stable_clue_refine_cache_key(clues: list[Mapping[str, Any]], *, query: str, intent: Mapping[str, Any]) -> str:
    payload = {
        "prompt_version": "clue_refine_sanitized_v1",
        "query": str(query or "").strip(),
        "intent": {
            "risk_types": intent.get("risk_types"),
            "quality_profile": intent.get("quality_profile"),
            "require_cross_source": intent.get("require_cross_source"),
            "require_evidence_chain": intent.get("require_evidence_chain"),
        },
        "clues": [sanitize_clue_for_llm(clue, stable_id=True) for clue in clues],
    }
    return "clue_refine:" + _short_hash(payload, length=32)


def stable_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _mask_or_hash_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    masked = PIIMasker().mask_text(text)
    if masked != text or _looks_like_account_or_contact(text):
        return f"hash:{_short_hash(text)}"
    return masked


def _looks_like_account_or_contact(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in ("tg:", "telegram", "wechat", "wx:", "qq:", "uid:", "@")):
        return True
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text):
        return True
    return bool(re.fullmatch(r"[A-Za-z][-_A-Za-z0-9]{4,31}", text))


def _short_hash(value: Any, *, length: int = 12) -> str:
    canonical = stable_json_dumps(value)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:length]


def _safe_domain(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "{query}" in text:
        text = text.replace("{query}", "")
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    hostname = parsed.hostname
    return hostname.lower() if hostname else None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = str(value).strip()
        return [text] if text else []
    try:
        return [str(item).strip() for item in value if str(item).strip()]
    except TypeError:
        text = str(value).strip()
        return [text] if text else []


__all__ = [
    "sanitize_clue_for_llm",
    "sanitize_entity_for_llm",
    "sanitize_source_for_llm",
    "stable_clue_card_id",
    "stable_clue_refine_cache_key",
    "stable_json_dumps",
]
