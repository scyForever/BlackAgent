"""Authorized source catalog loading helpers for batch collection."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from src.config_loader import load_yaml_file

from .relevance import get_theme_search_variants


class SourceCatalogError(ValueError):
    """Raised when a batch source catalog cannot be normalized."""


def load_source_catalog(path: str | Path) -> list[dict[str, Any]]:
    payload = load_yaml_file(path)
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourceCatalogError("source catalog must contain a non-empty 'sources' list")
    expanded: list[dict[str, Any]] = []
    for index, item in enumerate(sources, start=1):
        expanded.extend(expand_source_definition(normalize_source_definition(item, index=index)))
    return expanded


def normalize_source_definition(source: Mapping[str, Any], *, index: int | None = None) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        suffix = f" at index {index}" if index is not None else ""
        raise SourceCatalogError(f"source definition{suffix} must be a mapping")

    normalized = {str(key): value for key, value in source.items()}
    source_name = str(normalized.get("source_name") or normalized.get("name") or "").strip()
    source_url = str(normalized.get("source_url") or normalized.get("url") or "").strip()
    query_url_template = str(normalized.get("query_url_template") or "").strip()
    if not source_name or (not source_url and not query_url_template):
        suffix = f" at index {index}" if index is not None else ""
        raise SourceCatalogError(f"source definition{suffix} requires source_name and source_url or query_url_template")

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

    query_seed_terms = normalized.get("query_seed_terms")
    if isinstance(query_seed_terms, str):
        query_seed_terms = [query_seed_terms]
    if query_seed_terms is None:
        query_seed_terms = []
    if not isinstance(query_seed_terms, (list, tuple, set)):
        raise SourceCatalogError(f"query_seed_terms must be a list for source {source_name}")

    query_global_terms = normalized.get("query_global_terms")
    if isinstance(query_global_terms, str):
        query_global_terms = [query_global_terms]
    if query_global_terms is None:
        query_global_terms = []
    if not isinstance(query_global_terms, (list, tuple, set)):
        raise SourceCatalogError(f"query_global_terms must be a list for source {source_name}")

    query_themes = normalized.get("query_themes")
    if isinstance(query_themes, str):
        query_themes = [query_themes]
    if query_themes is None:
        query_themes = []
    if not isinstance(query_themes, (list, tuple, set)):
        raise SourceCatalogError(f"query_themes must be a list for source {source_name}")

    headers = normalized.get("headers") or {}
    if not isinstance(headers, Mapping):
        raise SourceCatalogError(f"headers must be a mapping for source {source_name}")

    include_keywords = normalized.get("include_keywords")
    if isinstance(include_keywords, str):
        include_keywords = [include_keywords]
    if include_keywords is None:
        include_keywords = []
    if not isinstance(include_keywords, (list, tuple, set)):
        raise SourceCatalogError(f"include_keywords must be a list for source {source_name}")

    exclude_keywords = normalized.get("exclude_keywords")
    if isinstance(exclude_keywords, str):
        exclude_keywords = [exclude_keywords]
    if exclude_keywords is None:
        exclude_keywords = []
    if not isinstance(exclude_keywords, (list, tuple, set)):
        raise SourceCatalogError(f"exclude_keywords must be a list for source {source_name}")

    include_themes = normalized.get("include_themes")
    if isinstance(include_themes, str):
        include_themes = [include_themes]
    if include_themes is None:
        include_themes = []
    if not isinstance(include_themes, (list, tuple, set)):
        raise SourceCatalogError(f"include_themes must be a list for source {source_name}")

    exclude_themes = normalized.get("exclude_themes")
    if isinstance(exclude_themes, str):
        exclude_themes = [exclude_themes]
    if exclude_themes is None:
        exclude_themes = []
    if not isinstance(exclude_themes, (list, tuple, set)):
        raise SourceCatalogError(f"exclude_themes must be a list for source {source_name}")

    min_keyword_hits = normalized.get("min_keyword_hits", 1)
    try:
        min_keyword_hits = int(min_keyword_hits)
    except (TypeError, ValueError) as exc:
        raise SourceCatalogError(f"min_keyword_hits must be an integer for source {source_name}") from exc
    if min_keyword_hits <= 0:
        raise SourceCatalogError(f"min_keyword_hits must be positive for source {source_name}")

    query_term_limit = normalized.get("query_term_limit", 0)
    try:
        query_term_limit = int(query_term_limit or 0)
    except (TypeError, ValueError) as exc:
        raise SourceCatalogError(f"query_term_limit must be an integer for source {source_name}") from exc
    if query_term_limit < 0:
        raise SourceCatalogError(f"query_term_limit must be non-negative for source {source_name}")

    normalized["source_name"] = source_name
    normalized["source_url"] = source_url
    normalized["query_url_template"] = query_url_template
    normalized["query_seed_terms"] = [str(item).strip() for item in query_seed_terms if str(item).strip()]
    normalized["query_global_terms"] = [str(item).strip() for item in query_global_terms if str(item).strip()]
    normalized["query_themes"] = [str(item).strip() for item in query_themes if str(item).strip()]
    normalized["query_term_limit"] = query_term_limit
    normalized["feed_format"] = str(normalized.get("feed_format") or "auto").strip().lower()
    normalized["allowed_domains"] = [str(item).strip() for item in allowed_domains if str(item).strip()]
    normalized["text_fields"] = [str(item).strip() for item in text_fields if str(item).strip()]
    normalized["headers"] = {
        str(key): str(value)
        for key, value in headers.items()
        if key is not None and value not in (None, "")
    }
    normalized["include_keywords"] = [str(item).strip() for item in include_keywords if str(item).strip()]
    normalized["exclude_keywords"] = [str(item).strip() for item in exclude_keywords if str(item).strip()]
    normalized["include_themes"] = [str(item).strip() for item in include_themes if str(item).strip()]
    normalized["exclude_themes"] = [str(item).strip() for item in exclude_themes if str(item).strip()]
    normalized["min_keyword_hits"] = min_keyword_hits
    return normalized


def build_query_variants(
    *,
    query_seed_terms: list[str],
    query_global_terms: list[str],
    query_themes: list[str],
    query_term_limit: int,
) -> list[dict[str, Any]]:
    base_terms = [term for term in query_seed_terms if term]
    variants: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    index = 1

    def append_variant(query: str, *, theme: str | None, term: str | None) -> None:
        nonlocal index
        normalized_query = " ".join(query.split()).strip()
        if not normalized_query:
            return
        dedupe_key = normalized_query.lower()
        if dedupe_key in seen_queries:
            return
        seen_queries.add(dedupe_key)
        variants.append({"query": normalized_query, "theme": theme, "term": term, "index": index})
        index += 1

    for term in query_global_terms:
        query = " ".join([*base_terms, term]).strip()
        append_variant(query, theme=None, term=term)

    per_theme_limit = query_term_limit or 3
    for theme in query_themes:
        for item in get_theme_search_variants(theme, limit=per_theme_limit):
            term = str(item["term"])
            query = " ".join([*base_terms, term]).strip()
            append_variant(query, theme=theme, term=term)
            variants[-1]["term_stage"] = str(item.get("stage") or "core")

    if variants:
        return variants
    if not base_terms:
        return []
    return [{"query": " ".join(base_terms), "theme": None, "term": None, "index": 1}]


def expand_source_definition(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    normalized = dict(source)
    query_url_template = str(normalized.get("query_url_template") or "").strip()
    if not query_url_template:
        return [normalized]

    variants = build_query_variants(
        query_seed_terms=list(normalized.get("query_seed_terms") or []),
        query_global_terms=list(normalized.get("query_global_terms") or []),
        query_themes=list(normalized.get("query_themes") or []),
        query_term_limit=int(normalized.get("query_term_limit", 0) or 0),
    )
    if not variants:
        return [normalized]

    expanded: list[dict[str, Any]] = []
    for variant in variants:
        item = dict(normalized)
        item["source_url"] = query_url_template.format(query=quote(str(variant["query"]), safe=""))
        item["search_query"] = str(variant["query"])
        item["query_theme"] = variant["theme"]
        item["query_term"] = variant["term"]
        item["query_term_stage"] = variant.get("term_stage", "core")
        item["query_variant_index"] = variant["index"]
        expanded.append(item)
    return expanded


__all__ = [
    "SourceCatalogError",
    "build_query_variants",
    "expand_source_definition",
    "load_source_catalog",
    "normalize_source_definition",
]
