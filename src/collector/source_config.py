"""Authorized source catalog loading helpers for batch collection."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from src.config_loader import load_yaml_file


class SourceCatalogError(ValueError):
    """Raised when a batch source catalog cannot be normalized."""


def load_source_catalog(path: str | Path) -> list[dict[str, Any]]:
    payload = load_yaml_file(path)
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourceCatalogError("source catalog must contain a non-empty 'sources' list")
    return [normalize_source_definition(item, index=index) for index, item in enumerate(sources, start=1)]


def normalize_source_definition(source: Mapping[str, Any], *, index: int | None = None) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        suffix = f" at index {index}" if index is not None else ""
        raise SourceCatalogError(f"source definition{suffix} must be a mapping")

    normalized = {str(key): value for key, value in source.items()}
    source_name = str(normalized.get("source_name") or normalized.get("name") or "").strip()
    source_url = str(normalized.get("source_url") or normalized.get("url") or "").strip()
    if not source_name or not source_url:
        suffix = f" at index {index}" if index is not None else ""
        raise SourceCatalogError(f"source definition{suffix} requires source_name and source_url")

    allowed_domains = normalized.get("allowed_domains")
    if allowed_domains in (None, "") and normalized.get("allowed_domain"):
        allowed_domains = [normalized.get("allowed_domain")]
    if isinstance(allowed_domains, str):
        allowed_domains = [allowed_domains]
    if allowed_domains is None:
        allowed_domains = []
    if not isinstance(allowed_domains, (list, tuple, set)):
        raise SourceCatalogError(f"allowed_domains must be a list for source {source_name}")

    text_fields = normalized.get("text_fields")
    if isinstance(text_fields, str):
        text_fields = [text_fields]
    if text_fields is None:
        text_fields = []
    if not isinstance(text_fields, (list, tuple, set)):
        raise SourceCatalogError(f"text_fields must be a list for source {source_name}")

    headers = normalized.get("headers") or {}
    if not isinstance(headers, Mapping):
        raise SourceCatalogError(f"headers must be a mapping for source {source_name}")

    normalized["source_name"] = source_name
    normalized["source_url"] = source_url
    normalized["feed_format"] = str(normalized.get("feed_format") or "auto").strip().lower()
    normalized["allowed_domains"] = [str(item).strip() for item in allowed_domains if str(item).strip()]
    normalized["text_fields"] = [str(item).strip() for item in text_fields if str(item).strip()]
    normalized["headers"] = {
        str(key): str(value)
        for key, value in headers.items()
        if key is not None and value not in (None, "")
    }
    return normalized


__all__ = ["SourceCatalogError", "load_source_catalog", "normalize_source_definition"]
