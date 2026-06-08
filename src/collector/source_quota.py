"""Quota-aware source selection helpers for collection catalogs."""

from __future__ import annotations

from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.collector.source_metadata import source_class_for_record, source_quota_groups_for_record


@dataclass(frozen=True)
class SourceQuotaSelection:
    selected: list[dict[str, Any]]
    quota_counts: dict[str, int]
    warnings: list[str]


def apply_source_min_quotas(
    sources: Iterable[Mapping[str, Any]],
    *,
    max_sources: int,
    minimum_quotas: Mapping[str, int] | None = None,
) -> SourceQuotaSelection:
    """Select quota groups first, then fill remaining slots with balanced sources."""

    materialized = [dict(source) for source in sources]
    limit = max(0, int(max_sources or 0))
    if limit <= 0 or limit >= len(materialized):
        selected = list(materialized)
        return SourceQuotaSelection(
            selected=selected,
            quota_counts=_quota_counts(selected),
            warnings=_quota_warnings(selected, minimum_quotas or {}),
        )

    quotas = {
        str(group): max(0, int(count))
        for group, count in (minimum_quotas or {}).items()
        if str(group).strip() and int(count) > 0
    }
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str, str]] = set()

    def key_for(source: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            str(source.get("source_name") or source.get("name") or ""),
            str(source.get("source_url") or source.get("url") or ""),
            str(source.get("search_query") or source.get("query_term") or ""),
        )

    for quota_group, requested in quotas.items():
        if len(selected) >= limit:
            break
        current_count = sum(1 for source in selected if quota_group in source_quota_groups_for_record(source))
        for source in materialized:
            if current_count >= requested or len(selected) >= limit:
                break
            source_key = key_for(source)
            if source_key in selected_keys:
                continue
            if quota_group not in source_quota_groups_for_record(source):
                continue
            selected.append(dict(source))
            selected_keys.add(source_key)
            current_count += 1

    selected_classes = {source_class_for_record(source) for source in selected}
    available_classes = _available_classes(materialized)
    for source_class in available_classes:
        if len(selected) >= limit:
            break
        if source_class in selected_classes:
            continue
        for source in materialized:
            if source_class_for_record(source) != source_class:
                continue
            source_key = key_for(source)
            if source_key in selected_keys:
                continue
            selected.append(dict(source))
            selected_keys.add(source_key)
            selected_classes.add(source_class)
            break

    for source in _balanced_order(materialized):
        if len(selected) >= limit:
            break
        source_key = key_for(source)
        if source_key in selected_keys:
            continue
        selected.append(dict(source))
        selected_keys.add(source_key)

    return SourceQuotaSelection(
        selected=selected,
        quota_counts=_quota_counts(selected),
        warnings=_quota_warnings(selected, quotas),
    )


def quota_balanced_source_slice(
    sources: Iterable[Mapping[str, Any]],
    *,
    max_sources: int,
    minimum_quotas: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    return apply_source_min_quotas(
        sources,
        max_sources=max_sources,
        minimum_quotas=minimum_quotas,
    ).selected


def _balanced_order(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: OrderedDict[tuple[str, str], deque[dict[str, Any]]] = OrderedDict()
    for source in sources:
        key = (
            source_class_for_record(source),
            str(source.get("source_name") or source.get("name") or "unknown_source"),
        )
        grouped.setdefault(key, deque()).append(source)

    ordered: list[dict[str, Any]] = []
    while grouped:
        for key in list(grouped.keys()):
            queue = grouped.get(key)
            if not queue:
                grouped.pop(key, None)
                continue
            ordered.append(dict(queue.popleft()))
            if not queue:
                grouped.pop(key, None)
    return ordered


def _available_classes(sources: Iterable[Mapping[str, Any]]) -> list[str]:
    classes: list[str] = []
    seen: set[str] = set()
    for source in sources:
        source_class = source_class_for_record(source)
        if source_class in seen:
            continue
        seen.add(source_class)
        classes.append(source_class)
    return classes


def _quota_counts(sources: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for source in sources:
        for group in source_quota_groups_for_record(source):
            counter[group] += 1
    return dict(sorted(counter.items()))


def _quota_warnings(sources: Iterable[Mapping[str, Any]], quotas: Mapping[str, int]) -> list[str]:
    counts = _quota_counts(sources)
    warnings: list[str] = []
    for group, requested in sorted((str(key), int(value)) for key, value in quotas.items()):
        actual = counts.get(group, 0)
        if actual < requested:
            warnings.append(f"{group}_quota_underfilled:{actual}/{requested}")
    return warnings


__all__ = [
    "SourceQuotaSelection",
    "apply_source_min_quotas",
    "quota_balanced_source_slice",
]
