"""Canonical entity normalization for extraction, graph, and evaluation."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from src.cleaner.text_filter import normalize_text
from src.extractor.entity_extractor import ACCOUNT, CONTACT, TOOL_NAME, URL


SENSITIVE_TYPES = {CONTACT, ACCOUNT, "invite_code", "contact_alias"}


@dataclass(frozen=True)
class NormalizedEntity:
    entity_type: str
    raw_value: str
    normalized_value: str
    canonical_hash: str
    masked_value: str
    confidence: float = 1.0
    normalizer_version: str = "entity_normalizer_v1"
    sensitivity_level: str = "normal"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class EntityNormalizer:
    """Normalize Telegram/WeChat/URL/invite/contact values consistently."""

    version = "entity_normalizer_v1"

    def normalize(
        self,
        *,
        entity_type: str,
        raw_value: Any,
        confidence: float = 1.0,
    ) -> NormalizedEntity:
        raw = normalize_text(str(raw_value or "")).strip(" ,，。;；")
        kind = str(entity_type or "unknown").strip().lower() or "unknown"
        normalized = _normalize_value(kind, raw)
        if kind == ACCOUNT and _looks_like_invite_code(normalized):
            kind = "invite_code"
        sensitivity = "sensitive" if kind in SENSITIVE_TYPES else "normal"
        return NormalizedEntity(
            entity_type=kind,
            raw_value=raw,
            normalized_value=normalized,
            canonical_hash=_hash(f"{kind}:{normalized.lower()}"),
            masked_value=_mask(kind, normalized),
            confidence=max(0.0, min(float(confidence or 0.0), 1.0)),
            sensitivity_level=sensitivity,
        )


def normalize_entity_payload(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with canonical normalized/masked/hash fields attached."""

    normalizer = EntityNormalizer()
    normalized = normalizer.normalize(
        entity_type=str(entity.get("entity_type") or entity.get("type") or "unknown"),
        raw_value=entity.get("normalized_value") or entity.get("entity_value") or entity.get("raw_value") or entity.get("value"),
        confidence=float(entity.get("confidence") or 1.0),
    )
    payload = dict(entity)
    payload.update(
        {
            "entity_type": normalized.entity_type,
            "raw_value": payload.get("raw_value") or payload.get("entity_value"),
            "normalized_value": normalized.normalized_value,
            "canonical_hash": normalized.canonical_hash,
            "masked_value": normalized.masked_value,
            "normalizer_version": normalized.normalizer_version,
            "sensitivity_level": normalized.sensitivity_level,
        }
    )
    return payload


def _normalize_value(entity_type: str, raw: str) -> str:
    value = _normalize_obfuscation(raw)
    lowered = value.lower()
    if raw.lower() in {"telegram", "tg", "飞机", "纸飞机", "小飞机"}:
        return "Telegram"
    if raw in {"QQ", "企鹅"}:
        return "QQ"
    if entity_type == CONTACT and raw in {"微信", "加v", "加V", "VX", "vx"}:
        return "WeChat"
    if entity_type == CONTACT:
        telegram = re.match(r"(?i)^(?:tg|telegram|飞机|纸飞机|小飞机|电报)[:：@\s]*([a-z][a-z0-9_]{2,31})$", value)
        if telegram:
            return f"Telegram:{telegram.group(1)}"
        if re.match(r"(?i)^@[a-z][a-z0-9_]{2,31}$", value):
            return f"Telegram:{value[1:]}"
        wechat = re.match(r"(?i)^(?:微信|vx|v信|wechat|wx)[:：\s]*([a-z][-_a-z0-9]{5,19})$", value)
        if wechat:
            return f"WeChat:{wechat.group(1)}"
        qq = re.match(r"(?i)^(?:qq|企鹅)[:：\s]*([1-9]\d{4,11})$", value)
        if qq:
            return f"QQ:{qq.group(1)}"
    if entity_type == URL:
        url = value.replace("hxxps://", "https://").replace("hxxp://", "http://")
        if re.match(r"(?i)^[a-z0-9-]+(?:\.[a-z0-9-]+)+", url) and "://" not in url:
            url = f"https://{url}"
        return url
    if entity_type in {ACCOUNT, "invite_code"}:
        code_match = re.match(r"(?i)^(?:邀请码|暗号|口令)?[:：\s]*(?:code[:：\s]*)?([a-z0-9_-]{3,24})$", value)
        if code_match:
            return code_match.group(1)
    if entity_type == TOOL_NAME:
        if lowered in {"telegram", "tg", "飞机", "纸飞机", "小飞机"}:
            return "Telegram"
    return value


def _normalize_obfuscation(value: str) -> str:
    text = normalize_text(value)
    text = text.replace("hxxps://", "https://").replace("hxxp://", "http://")
    text = text.replace("[.]", ".").replace("【.】", ".").replace("(.)", ".")
    text = re.sub(r"\s+", "", text) if any(token in text for token in ("[.]", "【.】", "(.)")) else text
    return text.strip(" ,，。;；")


def _looks_like_invite_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{3,24}", value or ""))


def _mask(entity_type: str, value: str) -> str:
    if entity_type not in SENSITIVE_TYPES:
        return value
    if ":" in value:
        prefix, tail = value.split(":", 1)
        return f"{prefix}:***{tail[-2:]}" if len(tail) > 2 else f"{prefix}:***"
    if len(value) >= 7 and value.isdigit():
        return f"{value[:3]}****{value[-4:]}"
    if len(value) <= 2:
        return "***"
    return f"***{value[-2:]}"


def _pii_hash_salt() -> str:
    """Per-deployment secret salt for one-way PII hashing.

    Defaults to empty so local benchmark artifacts stay reproducible and existing
    canonical hashes are unchanged. Set ``BLACKAGENT_PII_HASH_SALT`` in any
    deployment that persists hashed contacts/accounts so the SHA-256 digests are
    salted and not reversible via rainbow tables over small identifier spaces.
    """

    return os.environ.get("BLACKAGENT_PII_HASH_SALT", "")


def _hash(value: str) -> str:
    salt = _pii_hash_salt()
    payload = f"{salt}\x1f{value}" if salt else value
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["EntityNormalizer", "NormalizedEntity", "normalize_entity_payload"]
