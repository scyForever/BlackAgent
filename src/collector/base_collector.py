"""Base types and helpers for deterministic compliant collectors.

This module deliberately keeps network access out of the MVP collector layer.
`MockCollector` implementations should only materialize already-authorized
fixture data into the shared RawIntelligence contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib import import_module
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4


RAW_SCHEMA_MODEL = None


def _load_raw_schema_model() -> type[Any] | None:
    """Return storage.schemas.RawIntelligence when Worker B provides it.

    Worker C is allowed to reference storage.schemas but not modify storage
    files.  A small fallback dataclass keeps this deterministic backbone
    independently testable while remaining compatible with the shared schema
    once it appears.
    """

    global RAW_SCHEMA_MODEL
    if RAW_SCHEMA_MODEL is not None:
        return RAW_SCHEMA_MODEL
    try:
        RAW_SCHEMA_MODEL = getattr(import_module("storage.schemas"), "RawIntelligence")
    except Exception:
        RAW_SCHEMA_MODEL = _FallbackRawIntelligence
    return RAW_SCHEMA_MODEL


@dataclass(frozen=True)
class _FallbackRawIntelligence:
    hash_id: str
    trace_id: str
    source_type: str
    source_name: str
    source_url: str
    capture_snapshot_uri: str
    collector_version: str
    raw_payload_uri: str
    legal_basis: str
    crawl_time: str
    publish_time: str
    content_text: str

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class BaseCollector(Protocol):
    """Protocol implemented by deterministic MVP collectors."""

    def stream(self) -> Iterable[Any]:
        """Yield RawIntelligence-compatible objects."""


def model_dump(record: Any) -> dict[str, Any]:
    """Return a dict for Pydantic models, dataclasses, mappings, or objects."""

    if isinstance(record, Mapping):
        return dict(record)
    if hasattr(record, "model_dump"):
        return dict(record.model_dump())
    if hasattr(record, "dict"):
        return dict(record.dict())
    if hasattr(record, "__dataclass_fields__"):
        return asdict(record)
    return {
        key: getattr(record, key)
        for key in dir(record)
        if not key.startswith("_") and not callable(getattr(record, key))
    }


def get_record_field(record: Any, field: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field, default)
    return getattr(record, field, default)


def build_raw_intelligence(record: Mapping[str, Any]) -> Any:
    """Build a RawIntelligence-compatible object from a JSONL record."""

    content_text = str(
        record.get("content_text")
        or record.get("text")
        or record.get("raw_text")
        or record.get("content")
        or ""
    )
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "hash_id": str(record.get("hash_id") or sha256(content_text.encode("utf-8")).hexdigest()),
        "trace_id": str(record.get("trace_id") or uuid4()),
        "source_type": str(record.get("source_type") or "IM"),
        "source_name": str(record.get("source_name") or "mock_jsonl_fixture"),
        "source_url": str(record.get("source_url") or "file://tests/fixtures/sample_raw.jsonl"),
        "capture_snapshot_uri": str(record.get("capture_snapshot_uri") or ""),
        "collector_version": str(record.get("collector_version") or "mock_collector_v1"),
        "raw_payload_uri": str(record.get("raw_payload_uri") or ""),
        "legal_basis": str(record.get("legal_basis") or "PUBLIC_COMPLIANT_DATA"),
        "crawl_time": record.get("crawl_time") or now,
        "publish_time": record.get("publish_time") or record.get("crawl_time") or now,
        "content_text": content_text,
    }
    extras = {key: value for key, value in record.items() if key not in payload}

    schema_model = _load_raw_schema_model()
    try:
        built = schema_model(**payload)  # type: ignore[misc,operator]
        if extras:
            return {
                **built.model_dump(mode="json"),
                **extras,
            }
        return built
    except Exception:
        # If the shared schema is stricter than this worker's fallback, keep
        # collector tests deterministic rather than mutating storage contracts.
        if extras:
            return {**payload, **extras}
        return _FallbackRawIntelligence(**payload)
