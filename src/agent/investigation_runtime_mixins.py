"""Composed investigation runtime helper mixin surface."""

from __future__ import annotations

from typing import Any, Mapping

from .runtime_phase_services import InvestigationPhaseMixin
from .runtime_collection_services import InvestigationCollectionMixin
from .runtime_quality_services import InvestigationQualityMixin
from .runtime_clue_services import InvestigationClueMixin
from .runtime_config_services import InvestigationConfigMixin


class InvestigationRuntimeMixin(
    InvestigationPhaseMixin,
    InvestigationCollectionMixin,
    InvestigationQualityMixin,
    InvestigationClueMixin,
    InvestigationConfigMixin,
):
    """Compatibility surface composed from focused runtime service mixins."""


def _normalize_source_pref(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"telegram", "tg", "电报"}:
        return "telegram"
    if text in {"forum", "论坛", "贴吧"}:
        return "forum"
    if text in {"im", "chat", "群", "私聊"}:
        return "im"
    return text


def _as_investigation_processing_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose local processing as one integrated step in the investigation flow."""

    normalized = dict(payload)
    normalized["mode"] = "investigation_processing"
    return normalized


__all__ = ["InvestigationRuntimeMixin"]
