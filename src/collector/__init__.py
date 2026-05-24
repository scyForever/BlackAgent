"""Collector package for BlackAgent deterministic backbone."""

from .base_collector import BaseCollector, build_raw_intelligence, get_record_field, model_dump
from .http_feed_collector import HTTPFeedCollector, HTTPFeedConfig, NetworkCollectionDisabled, SourceAuthorizationError
from .im_collector import DEFAULT_FIXTURE_PATH, MockCollector
from .source_config import SourceCatalogError, load_source_catalog

__all__ = [
    "BaseCollector",
    "DEFAULT_FIXTURE_PATH",
    "HTTPFeedCollector",
    "HTTPFeedConfig",
    "MockCollector",
    "NetworkCollectionDisabled",
    "SourceCatalogError",
    "SourceAuthorizationError",
    "build_raw_intelligence",
    "get_record_field",
    "load_source_catalog",
    "model_dump",
]
