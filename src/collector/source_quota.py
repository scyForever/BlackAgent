"""Quota-aware source selection helpers for collection catalogs."""

from __future__ import annotations

from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.collector.source_metadata import source_class_for_record, source_quota_groups_for_record


@dataclass(frozen=True)
class SourceQuotaSelection:
    selected: list[dict[str, Any]]
    quota_counts: dict[str, int]
    warnings: list[str]
    source_name_counts: dict[str, int] = field(default_factory=dict)
    source_name_warnings: list[str] = field(default_factory=list)


def apply_source_min_quotas(
    sources: Iterable[Mapping[str, Any]],
    *,
    max_sources: int,
    minimum_quotas: Mapping[str, int] | None = None,
    source_name_max_quota: int | None = None,
) -> SourceQuotaSelection:
    """Select quota groups first, then fill remaining slots with balanced sources."""

    materialized = [dict(source) for source in sources]
    requested_limit = max(0, int(max_sources or 0))
    limit = requested_limit if requested_limit > 0 else len(materialized)
    name_cap = _positive_int(source_name_max_quota)
    if not materialized:
        return SourceQuotaSelection(
            selected=[],
            quota_counts={},
            warnings=_quota_warnings([], minimum_quotas or {}),
            source_name_counts={},
            source_name_warnings=[],
        )
    if limit >= len(materialized) and name_cap <= 0:
        selected = list(materialized)
        return SourceQuotaSelection(
            selected=selected,
            quota_counts=_quota_counts(selected),
            warnings=_quota_warnings(selected, minimum_quotas or {}),
            source_name_counts=_source_name_counts(selected),
        )

    quotas = {
        str(group): max(0, int(count))
        for group, count in (minimum_quotas or {}).items()
        if str(group).strip() and int(count) > 0
    }
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str, str]] = set()
    selected_name_counts: Counter[str] = Counter()

    def key_for(source: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            str(source.get("source_name") or source.get("name") or ""),
            str(source.get("source_url") or source.get("url") or ""),
            str(source.get("search_query") or source.get("query_term") or ""),
        )

    def can_select(source: Mapping[str, Any]) -> bool:
        if key_for(source) in selected_keys:
            return False
        if name_cap and selected_name_counts[_source_name_key(source)] >= name_cap:
            return False
        return True

    def append_source(source: Mapping[str, Any]) -> None:
        selected.append(dict(source))
        selected_keys.add(key_for(source))
        selected_name_counts[_source_name_key(source)] += 1

    for quota_group, requested in quotas.items():
        if len(selected) >= limit:
            break
        current_count = sum(1 for source in selected if quota_group in source_quota_groups_for_record(source))
        for source in materialized:
            if current_count >= requested or len(selected) >= limit:
                break
            if quota_group not in source_quota_groups_for_record(source):
                continue
            if not can_select(source):
                continue
            append_source(source)
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
            if not can_select(source):
                continue
            append_source(source)
            selected_classes.add(source_class)
            break

    for source in _balanced_order(materialized):
        if len(selected) >= limit:
            break
        if not can_select(source):
            continue
        append_source(source)

    return SourceQuotaSelection(
        selected=selected,
        quota_counts=_quota_counts(selected),
        warnings=_quota_warnings(selected, quotas),
        source_name_counts=_source_name_counts(selected),
        source_name_warnings=_source_name_warnings(materialized, selected, limit=limit, source_name_max_quota=name_cap),
    )


def quota_balanced_source_slice(
    sources: Iterable[Mapping[str, Any]],
    *,
    max_sources: int,
    minimum_quotas: Mapping[str, int] | None = None,
    source_name_max_quota: int | None = None,
) -> list[dict[str, Any]]:
    return apply_source_min_quotas(
        sources,
        max_sources=max_sources,
        minimum_quotas=minimum_quotas,
        source_name_max_quota=source_name_max_quota,
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


def _positive_int(value: int | None) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _source_name_key(source: Mapping[str, Any]) -> str:
    return str(source.get("source_name") or source.get("name") or "unknown_source").strip().lower() or "unknown_source"


def _source_name_counts(sources: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for source in sources:
        name = str(source.get("source_name") or source.get("name") or "unknown_source").strip() or "unknown_source"
        counter[name] += 1
    return dict(sorted(counter.items()))


def _source_name_warnings(
    available_sources: Iterable[Mapping[str, Any]],
    selected_sources: Iterable[Mapping[str, Any]],
    *,
    limit: int,
    source_name_max_quota: int,
) -> list[str]:
    if source_name_max_quota <= 0:
        return []
    available_count = len(list(available_sources))
    expected_count = min(max(0, int(limit or 0)), available_count)
    selected_count = len(list(selected_sources))
    if selected_count >= expected_count:
        return []
    return [f"source_name_quota_underfilled:{selected_count}/{expected_count}"]


__all__ = [
    "SourceQuotaSelection",
    "apply_source_min_quotas",
    "quota_balanced_source_slice",
]
