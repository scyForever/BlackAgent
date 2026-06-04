"""Collector package for BlackAgent deterministic backbone."""

from .base_collector import BaseCollector, build_raw_intelligence, get_record_field, model_dump
from .http_feed_collector import HTTPFeedCollector, HTTPFeedConfig, NetworkCollectionDisabled, SourceAuthorizationError
from .im_collector import DEFAULT_FIXTURE_PATH, MockCollector
from .relevance import (
    DEFAULT_BLACKGRAY_INCLUDE_KEYWORDS,
    DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS,
    KeywordRelevanceDecision,
    decide_text_relevance,
    get_theme_search_variants,
    get_theme_search_terms,
    load_theme_synonym_registry,
    normalize_keywords,
    normalize_themes,
)
from .source_config import SourceCatalogError, load_source_catalog
from .source_metadata import (
    SOURCE_ACCESS_TYPES,
    build_collection_metadata,
    build_collection_quality_profile,
    classify_collection_failure,
    normalize_source_access_type,
    source_class_for_record,
)

__all__ = [
    "BaseCollector",
    "DEFAULT_FIXTURE_PATH",
    "DEFAULT_BLACKGRAY_INCLUDE_KEYWORDS",
    "DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS",
    "HTTPFeedCollector",
    "HTTPFeedConfig",
    "KeywordRelevanceDecision",
    "MockCollector",
    "NetworkCollectionDisabled",
    "SourceCatalogError",
    "SourceAuthorizationError",
    "SOURCE_ACCESS_TYPES",
    "build_collection_metadata",
    "build_collection_quality_profile",
    "build_raw_intelligence",
    "classify_collection_failure",
    "decide_text_relevance",
    "get_record_field",
    "get_theme_search_variants",
    "get_theme_search_terms",
    "load_source_catalog",
    "load_theme_synonym_registry",
    "model_dump",
    "normalize_source_access_type",
    "normalize_keywords",
    "normalize_themes",
    "source_class_for_record",
]
