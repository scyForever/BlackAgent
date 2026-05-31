"""Basic deterministic entity extractor for BlackAgent MVP."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Any, Iterable

from src.collector.base_collector import get_record_field
from src.cleaner.text_filter import normalize_text


URL = "url"
CONTACT = "contact"
ACCOUNT = "account"
TOOL_NAME = "tool_name"


@dataclass(frozen=True)
class FallbackEntityExtractionResult:
    entity_type: str
    entity_value: str
    start_offset: int
    end_offset: int
    source_trace_id: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


ENTITY_SCHEMA_MODEL = None


def _load_entity_schema_model() -> type[Any]:
    global ENTITY_SCHEMA_MODEL
    if ENTITY_SCHEMA_MODEL is not None:
        return ENTITY_SCHEMA_MODEL
    try:
        ENTITY_SCHEMA_MODEL = getattr(import_module("storage.schemas"), "EntityExtractionResult")
    except Exception:
        ENTITY_SCHEMA_MODEL = FallbackEntityExtractionResult
    return ENTITY_SCHEMA_MODEL


def _schema_fields(model: type[Any]) -> set[str]:
    return set(getattr(model, "model_fields", {}) or getattr(model, "__annotations__", {}) or [])


def build_entity_result(
    *,
    entity_type: str,
    entity_value: str,
    start_offset: int,
    end_offset: int,
    source_trace_id: str | None = None,
) -> Any:
    payload = {
        "entity_type": entity_type,
        "entity_value": entity_value,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "source_trace_id": source_trace_id,
    }
    model = _load_entity_schema_model()
    fields = _schema_fields(model)
    candidate = {key: value for key, value in payload.items() if not fields or key in fields}
    try:
        return model(**candidate)  # type: ignore[misc,operator]
    except Exception:
        return FallbackEntityExtractionResult(**payload)


class BasicEntityExtractor:
    URL_RE = re.compile(
        r"(?i)(?:https?://[^\s\"'<>，。；;]+|(?:[a-z0-9-]+\.)+(?:com|cn|net|org|top|xyz|io|cc|info|me)(?:/[^\s\"'<>，。；;]*)?)"
    )
    EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
    PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
    QQ_RE = re.compile(r"(?i)(?:QQ|企鹅|🐧)[:：\s]*([1-9]\d{4,11})")
    WECHAT_RE = re.compile(r"(?i)(?:微信|VX|V\s*X|V信|微(?:信)?|薇(?:信)?|围信|威信|wechat|wx)[:：\s]*([a-zA-Z][-_a-zA-Z0-9]{5,19})")
    TELEGRAM_RE = re.compile(r"(?i)(?:Telegram|TG|TG号|飞机|纸飞机|小飞机|电报|✈️?|🛩️?)[:：\s@]*([a-zA-Z][a-zA-Z0-9_]{2,31})|(?<!\w)@([A-Za-z][A-Za-z0-9_]{2,31})")
    ACCOUNT_RE = re.compile(r"(?i)(?:账号|账户|UID|用户ID|群号|ID)[:：\s#]*([A-Za-z0-9_-]{4,32})")
    TOOL_TERMS = (
        "群控",
        "脚本",
        "外挂",
        "接码平台",
        "接码",
        "改机",
        "打粉工具",
        "自动化工具",
        "卡密",
        "跑分平台",
    )

    def extract(self, item: Any) -> list[Any]:
        text = normalize_text(str(get_record_field(item, "clean_text") or get_record_field(item, "content_text") or item))
        source_trace_id = get_record_field(item, "source_trace_id") or get_record_field(item, "trace_id")
        entities: list[Any] = []
        seen: set[tuple[str, str, int, int]] = set()

        def add(entity_type: str, value: str, start: int, end: int) -> None:
            cleaned_value = value.strip(" \t\r\n,，。.;；")
            if not cleaned_value:
                return
            key = (entity_type, cleaned_value, start, end)
            if key in seen:
                return
            seen.add(key)
            entities.append(
                build_entity_result(
                    entity_type=entity_type,
                    entity_value=cleaned_value,
                    start_offset=start,
                    end_offset=end,
                    source_trace_id=str(source_trace_id) if source_trace_id else None,
                )
            )

        for match in self.URL_RE.finditer(text):
            add(URL, match.group(0), match.start(), match.end())
        for regex in (self.EMAIL_RE, self.PHONE_RE):
            for match in regex.finditer(text):
                add(CONTACT, match.group(0), match.start(), match.end())
        for regex in (self.QQ_RE, self.WECHAT_RE, self.TELEGRAM_RE):
            for match in regex.finditer(text):
                value = next(group for group in match.groups() if group)
                value_start = match.start(match.groups().index(value) + 1)
                add(CONTACT, value, value_start, value_start + len(value))
        for match in self.ACCOUNT_RE.finditer(text):
            add(ACCOUNT, match.group(1), match.start(1), match.end(1))
        for term in self.TOOL_TERMS:
            start = text.find(term)
            while start != -1:
                add(TOOL_NAME, term, start, start + len(term))
                start = text.find(term, start + len(term))

        return entities

    def extract_batch(self, items: Iterable[Any]) -> list[list[Any]]:
        return [self.extract(item) for item in items]


__all__ = [
    "ACCOUNT",
    "CONTACT",
    "TOOL_NAME",
    "URL",
    "BasicEntityExtractor",
    "FallbackEntityExtractionResult",
    "build_entity_result",
]
