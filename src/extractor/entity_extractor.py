"""Basic deterministic entity extractor for BlackAgent MVP."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Any, Iterable

from src.collector.base_collector import get_record_field
from src.cleaner.text_filter import normalize_text
from src.rules import RuleRegistry


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
    def __init__(self, rule_registry: RuleRegistry | None = None) -> None:
        self.rule_registry = rule_registry or RuleRegistry()
        self.patterns = _compile_pattern_specs(self.rule_registry.entity_pattern_specs(scopes={"basic"}))
        self.term_specs = [
            (str(spec.get("entity_type") or "unknown"), tuple(str(term) for term in spec.get("terms", ()) if str(term).strip()))
            for spec in self.rule_registry.entity_pattern_specs(scopes={"basic"})
            if spec.get("terms")
        ]

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

        for regex, entity_type in self.patterns:
            for match in regex.finditer(text):
                group_index = _first_group_index(match)
                value = match.group(group_index) if group_index is not None else match.group(0)
                value_start = match.start(group_index) if group_index is not None else match.start()
                add(entity_type, value, value_start, value_start + len(value))
        for entity_type, terms in self.term_specs:
            for term in terms:
                start = text.find(term)
                while start != -1:
                    add(entity_type, term, start, start + len(term))
                    start = text.find(term, start + len(term))

        return entities

    def extract_batch(self, items: Iterable[Any]) -> list[list[Any]]:
        return [self.extract(item) for item in items]


def _compile_pattern_specs(specs: Iterable[dict[str, Any]]) -> list[tuple[re.Pattern[str], str]]:
    compiled: list[tuple[re.Pattern[str], str]] = []
    for spec in specs:
        entity_type = str(spec.get("entity_type") or "unknown")
        for pattern in spec.get("patterns", []):
            try:
                compiled.append((re.compile(str(pattern), re.IGNORECASE), entity_type))
            except re.error:
                continue
    return compiled


def _first_group_index(match: re.Match[str]) -> int | None:
    for index, value in enumerate(match.groups(), start=1):
        if value:
            return index
    return None


__all__ = [
    "ACCOUNT",
    "CONTACT",
    "TOOL_NAME",
    "URL",
    "BasicEntityExtractor",
    "FallbackEntityExtractionResult",
    "build_entity_result",
]
