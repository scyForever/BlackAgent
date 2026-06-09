"""Risk clue, playbook, and candidate countermeasure generation."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from uuid import uuid4

from src.collector.base_collector import get_record_field
from src.cleaner.text_filter import canonicalize_for_dedup, normalize_text


@dataclass(frozen=True)
class RiskClue:
    clue_id: str
    clue_type: str
    key: str
    risk_category: str
    evidence_trace_ids: list[str]
    source_names: list[str]
    entity_values: list[str]
    confidence: float
    threshold_reason: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CheatingPlaybook:
    playbook_id: str
    risk_category: str
    clue_ids: list[str]
    lifecycle_elements: dict[str, list[str]]
    evidence_trace_ids: list[str]
    confidence: float
    summary: str
    requires_human_approval: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CountermeasureStrategy:
    strategy_id: str
    target_id: str
    target_type: str
    evidence_trace_ids: list[str]
    recommendation: str
    expected_false_positive_surface: str
    gray_release_scope: str
    allowed_actions: list[str]
    forbidden_actions: list[str]
    requires_human_approval: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CountermeasureSummary:
    summary_id: str
    target_id: str
    suspicious_entities: list[str]
    evidence_trace_ids: list[str]
    risk_focus: list[str]
    review_recommendation: str
    monitoring_keywords: list[str]
    confidence: float
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceChain:
    clue_id: str
    clue_type: str
    risk_category: str
    source_name: str
    source_type: str
    source_trace_id: str
    raw_excerpt: str
    matched_rules: list[str]
    extracted_entities: list[str]
    related_entities: list[str]
    confidence: float

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceChainRenderer:
    """Render every clue into source-backed, reviewable evidence rows."""

    def render(
        self,
        clues: Iterable[RiskClue | Mapping[str, Any]],
        records: Iterable[Mapping[str, Any] | Any],
        *,
        entities: Iterable[Mapping[str, Any] | Any] = (),
    ) -> list[EvidenceChain]:
        record_by_trace = {_trace_id(record): record for record in records}
        entities_by_trace: dict[str, list[str]] = defaultdict(list)
        for entity in entities:
            trace_id = str(get_record_field(entity, "source_trace_id") or "")
            value = str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or "")
            if trace_id and value:
                entities_by_trace[trace_id].append(value)

        rows: list[EvidenceChain] = []
        for clue in clues:
            clue_data = clue.model_dump() if hasattr(clue, "model_dump") else dict(clue)
            for trace_id in list(clue_data.get("evidence_trace_ids") or []):
                record = record_by_trace.get(str(trace_id), {})
                rows.append(
                    EvidenceChain(
                        clue_id=str(clue_data.get("clue_id") or ""),
                        clue_type=str(clue_data.get("clue_type") or ""),
                        risk_category=str(clue_data.get("risk_category") or "unknown"),
                        source_name=str(get_record_field(record, "source_name") or "unknown_source"),
                        source_type=str(get_record_field(record, "source_type") or "unknown"),
                        source_trace_id=str(trace_id),
                        raw_excerpt=_excerpt(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or "")),
                        matched_rules=_ordered_strings([clue_data.get("threshold_reason"), clue_data.get("clue_type")]),
                        extracted_entities=_ordered_strings(entities_by_trace.get(str(trace_id), [])),
                        related_entities=_ordered_strings(clue_data.get("entity_values") or []),
                        confidence=round(float(clue_data.get("confidence") or 0.0), 4),
                    )
                )
        return rows


class RiskClueAggregator:
    """Phase II clue aggregator using PRD hard thresholds."""

    CONTACT_TYPES = {"contact", "account"}
    URL_TYPES = {"url", "domain"}
    OVERLAP_SUPPORT_TYPES = {"domain", "url", "settlement", "invite_code", "price"}

    def aggregate(
        self,
        *,
        records: Iterable[Mapping[str, Any] | Any],
        classifications: Iterable[Mapping[str, Any] | Any],
        entities: Iterable[Mapping[str, Any] | Any],
    ) -> list[RiskClue]:
        record_by_trace = {_trace_id(record): record for record in records}
        category_by_trace = {
            str(get_record_field(item, "source_trace_id") or "unknown"): str(get_record_field(item, "risk_category") or "unknown")
            for item in classifications
        }
        entity_list = list(entities)
        clues: list[RiskClue] = []
        clues.extend(self._contact_clues(record_by_trace, category_by_trace, entity_list))
        clues.extend(self._url_clues(record_by_trace, category_by_trace, entity_list))
        clues.extend(
            self._shared_entity_clues(
                record_by_trace,
                category_by_trace,
                entity_list,
                entity_type="invite_code",
                clue_type="shared_invite_code_multi_source",
                reason="same_invite_code_appears_in_at_least_2_sources",
            )
        )
        clues.extend(
            self._shared_entity_clues(
                record_by_trace,
                category_by_trace,
                entity_list,
                entity_type="settlement",
                clue_type="shared_settlement_multi_source",
                reason="same_settlement_method_appears_in_at_least_2_sources",
            )
        )
        clues.extend(
            self._shared_entity_clues(
                record_by_trace,
                category_by_trace,
                entity_list,
                entity_type="tool_name",
                clue_type="shared_tool_multi_source",
                reason="same_tool_name_appears_in_at_least_2_sources",
            )
        )
        clues.extend(self._contextual_corroboration_clues(record_by_trace, category_by_trace, entity_list))
        clues.extend(self._contextual_contact_clues(record_by_trace, category_by_trace, entity_list))
        clues.extend(self._contextual_account_tool_overlap_clues(record_by_trace, category_by_trace, entity_list))
        clues.extend(self._contextual_tool_trade_cluster_clues(record_by_trace, category_by_trace, entity_list))
        clues.extend(self._account_tool_overlap_clues(record_by_trace, category_by_trace, entity_list))
        clues.extend(self._template_clues(record_by_trace, category_by_trace))
        return clues

    def _contact_clues(self, records: dict[str, Any], categories: dict[str, str], entities: Iterable[Any]) -> list[RiskClue]:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for entity in entities:
            if str(get_record_field(entity, "entity_type") or "").lower() in self.CONTACT_TYPES:
                key = _contact_clue_group_key(str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or ""))
                if key:
                    grouped[key].append(entity)
        clues: list[RiskClue] = []
        for key, group in grouped.items():
            value = _contact_clue_display_value(key, group)
            traces = sorted({str(get_record_field(entity, "source_trace_id") or "unknown") for entity in group})
            sources = {
                str(get_record_field(records.get(trace), "source_name") or get_record_field(records.get(trace), "source_type") or trace)
                for trace in traces
            }
            if len(traces) >= 2 and len(sources) >= 2 and self._within_48h([records.get(trace) for trace in traces if trace in records]):
                clues.append(self._make_clue("shared_contact_48h", value, traces, records, categories, [value], "same_contact_appears_in_at_least_2_sources_within_48h"))
        return clues

    def _url_clues(self, records: dict[str, Any], categories: dict[str, str], entities: Iterable[Any]) -> list[RiskClue]:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for entity in entities:
            if str(get_record_field(entity, "entity_type") or "").lower() in self.URL_TYPES:
                key = _domain(str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or ""))
                if key:
                    grouped[key].append(entity)
        clues: list[RiskClue] = []
        for value, group in grouped.items():
            traces = sorted({str(get_record_field(entity, "source_trace_id") or "unknown") for entity in group})
            sources = {str(get_record_field(records.get(trace), "source_name") or get_record_field(records.get(trace), "source_type") or trace) for trace in traces}
            if len(sources) >= 2:
                clues.append(self._make_clue("shared_domain_multi_source", value, traces, records, categories, [value], "same_domain_appears_in_at_least_2_sources"))
        return clues

    def _shared_entity_clues(
        self,
        records: dict[str, Any],
        categories: dict[str, str],
        entities: Iterable[Any],
        *,
        entity_type: str,
        clue_type: str,
        reason: str,
    ) -> list[RiskClue]:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for entity in entities:
            if str(get_record_field(entity, "entity_type") or "").lower() == entity_type:
                key = _simple_entity_group_key(str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or ""))
                if key:
                    grouped[key].append(entity)

        clues: list[RiskClue] = []
        for key, group in grouped.items():
            traces = sorted({str(get_record_field(entity, "source_trace_id") or "unknown") for entity in group})
            sources = {
                str(get_record_field(records.get(trace), "source_name") or get_record_field(records.get(trace), "source_type") or trace)
                for trace in traces
            }
            if len(traces) >= 2 and len(sources) >= 2:
                value = _simple_entity_display_value(key, group)
                clues.append(self._make_clue(clue_type, value, traces, records, categories, [value], reason))
        return clues

    def _account_tool_overlap_clues(self, records: dict[str, Any], categories: dict[str, str], entities: Iterable[Any]) -> list[RiskClue]:
        by_trace: dict[str, list[Any]] = defaultdict(list)
        for entity in entities:
            trace = str(get_record_field(entity, "source_trace_id") or "unknown")
            by_trace[trace].append(entity)

        grouped: dict[str, list[tuple[str, list[Any]]]] = defaultdict(list)
        for trace, trace_entities in by_trace.items():
            types = {str(get_record_field(entity, "entity_type") or "").lower() for entity in trace_entities}
            if "account" not in types or "tool_name" not in types or not types.intersection(self.OVERLAP_SUPPORT_TYPES):
                continue
            for entity in trace_entities:
                if str(get_record_field(entity, "entity_type") or "").lower() in self.CONTACT_TYPES:
                    key = _contact_clue_group_key(str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or ""))
                    if key:
                        grouped[key].append((trace, trace_entities))

        clues: list[RiskClue] = []
        for key, observations in grouped.items():
            traces = sorted({trace for trace, _ in observations})
            sources = {
                str(get_record_field(records.get(trace), "source_name") or get_record_field(records.get(trace), "source_type") or trace)
                for trace in traces
            }
            if len(traces) < 2 and len(sources) < 2:
                continue
            value = _contact_clue_display_value(key, (entity for _, trace_entities in observations for entity in trace_entities if str(get_record_field(entity, "entity_type") or "").lower() in self.CONTACT_TYPES))
            related_values = [value]
            for trace, trace_entities in sorted(observations, key=lambda item: item[0]):
                if trace not in traces:
                    continue
                for entity in trace_entities:
                    entity_type = str(get_record_field(entity, "entity_type") or "").lower()
                    if entity_type == "tool_name" or entity_type in self.OVERLAP_SUPPORT_TYPES:
                        related_value = _overlap_entity_display_value(entity)
                        if related_value:
                            related_values.append(related_value)
            clues.append(
                self._make_clue(
                    "entity_graph_account_tool_overlap",
                    value,
                    traces,
                    records,
                    categories,
                    _ordered_strings(related_values),
                    "same_contact_or_account_overlaps_tool_and_trade_entities_in_at_least_2_traces_or_sources",
                )
            )
        return clues

    def _contextual_corroboration_clues(self, records: dict[str, Any], categories: dict[str, str], entities: Iterable[Any]) -> list[RiskClue]:
        """Bridge singleton high-value identifiers when another source explicitly corroborates them."""

        by_trace: dict[str, list[Any]] = defaultdict(list)
        traces_by_type_key: dict[tuple[str, str], set[str]] = defaultdict(set)
        for entity in entities:
            trace = str(get_record_field(entity, "source_trace_id") or "unknown")
            by_trace[trace].append(entity)
            entity_type = str(get_record_field(entity, "entity_type") or "").lower()
            key = _bridge_entity_group_key(entity)
            if key:
                traces_by_type_key[(entity_type, key)].add(trace)

        clue_specs = {
            "domain": ("shared_domain_multi_source", "single_identifier_with_authorized_cross_source_corroboration"),
            "url": ("shared_domain_multi_source", "single_identifier_with_authorized_cross_source_corroboration"),
            "invite_code": ("shared_invite_code_multi_source", "single_identifier_with_authorized_cross_source_corroboration"),
            "settlement": ("shared_settlement_multi_source", "single_identifier_with_authorized_cross_source_corroboration"),
        }
        clues: list[RiskClue] = []
        existing: set[tuple[str, str, tuple[str, ...]]] = set()
        for trace, trace_entities in by_trace.items():
            record = records.get(trace)
            if record is None:
                continue
            trace_entity_types = {
                str(get_record_field(item, "entity_type") or "").lower()
                for item in trace_entities
            }
            record_source = _source_name(record, trace)
            record_time = _parse_time(get_record_field(record, "publish_time") or get_record_field(record, "crawl_time"))
            for entity in trace_entities:
                entity_type = str(get_record_field(entity, "entity_type") or "").lower()
                if entity_type not in clue_specs:
                    continue
                value = _bridge_entity_display_value(entity)
                key = _bridge_entity_group_key(entity)
                if not key:
                    continue
                if entity_type == "settlement" and _is_contextual_settlement_bridge_value(value) and trace_entity_types.intersection(self.CONTACT_TYPES):
                    continue
                if len(traces_by_type_key.get((entity_type, key), set())) >= 2 and not _is_contextual_settlement_bridge_value(value):
                    continue
                bridge_trace = self._best_corroborating_trace(
                    records,
                    categories,
                    source_trace=trace,
                    source_name=record_source,
                    source_time=record_time,
                    category=categories.get(trace, "unknown"),
                )
                if not bridge_trace:
                    continue
                clue_type, reason = clue_specs[entity_type]
                traces = sorted({trace, bridge_trace})
                identity = (clue_type, key, tuple(traces))
                if identity in existing:
                    continue
                existing.add(identity)
                clues.append(
                    self._make_clue(
                        clue_type,
                        value,
                        traces,
                        records,
                        categories,
                        [value],
                        reason,
                    )
                )
        return clues

    def _contextual_contact_clues(self, records: dict[str, Any], categories: dict[str, str], entities: Iterable[Any]) -> list[RiskClue]:
        clues: list[RiskClue] = []
        for entity in entities:
            if str(get_record_field(entity, "entity_type") or "").lower() not in self.CONTACT_TYPES:
                continue
            trace = str(get_record_field(entity, "source_trace_id") or "unknown")
            if trace not in records:
                continue
            value = _contact_clue_display_value(
                _contact_clue_group_key(str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or "")),
                [entity],
            )
            if not value:
                continue
            bridge_trace = self._best_corroborating_trace(
                records,
                categories,
                source_trace=trace,
                source_name=_source_name(records.get(trace), trace),
                source_time=_parse_time(get_record_field(records.get(trace), "publish_time") or get_record_field(records.get(trace), "crawl_time")),
                category=categories.get(trace, "unknown"),
            )
            if not bridge_trace:
                continue
            clues.append(
                self._make_clue(
                    "shared_contact_48h",
                    value,
                    sorted({trace, bridge_trace}),
                    records,
                    categories,
                    [value],
                    "single_identifier_with_authorized_cross_source_corroboration",
                )
            )
        return clues

    def _contextual_account_tool_overlap_clues(self, records: dict[str, Any], categories: dict[str, str], entities: Iterable[Any]) -> list[RiskClue]:
        by_trace: dict[str, list[Any]] = defaultdict(list)
        for entity in entities:
            by_trace[str(get_record_field(entity, "source_trace_id") or "unknown")].append(entity)

        clues: list[RiskClue] = []
        for trace, trace_entities in by_trace.items():
            if trace not in records:
                continue
            accounts = [
                entity
                for entity in trace_entities
                if str(get_record_field(entity, "entity_type") or "").lower() == "account"
            ]
            tools = [
                entity
                for entity in trace_entities
                if str(get_record_field(entity, "entity_type") or "").lower() == "tool_name"
            ]
            text_support = _account_tool_support_terms(records.get(trace))
            if not accounts or (not tools and not text_support):
                continue
            bridge_trace = self._best_corroborating_trace(
                records,
                categories,
                source_trace=trace,
                source_name=_source_name(records.get(trace), trace),
                source_time=_parse_time(get_record_field(records.get(trace), "publish_time") or get_record_field(records.get(trace), "crawl_time")),
                category=categories.get(trace, "unknown"),
            )
            if not bridge_trace:
                continue
            value = _bridge_entity_display_value(accounts[0])
            related_values = [value, *[_bridge_entity_display_value(entity) for entity in tools], *text_support]
            clues.append(
                self._make_clue(
                    "entity_graph_account_tool_overlap",
                    value,
                    sorted({trace, bridge_trace}),
                    records,
                    categories,
                    _ordered_strings(related_values),
                    "single_account_tool_overlap_with_authorized_cross_source_corroboration",
                )
            )
        return clues

    def _contextual_tool_trade_cluster_clues(self, records: dict[str, Any], categories: dict[str, str], entities: Iterable[Any]) -> list[RiskClue]:
        by_trace: dict[str, list[Any]] = defaultdict(list)
        for entity in entities:
            by_trace[str(get_record_field(entity, "source_trace_id") or "unknown")].append(entity)

        clues: list[RiskClue] = []
        for trace, trace_entities in by_trace.items():
            if trace not in records:
                continue
            if any(str(get_record_field(entity, "entity_type") or "").lower() == "account" for entity in trace_entities):
                continue
            contacts = [
                entity
                for entity in trace_entities
                if str(get_record_field(entity, "entity_type") or "").lower() in self.CONTACT_TYPES
            ]
            tools = [
                entity
                for entity in trace_entities
                if str(get_record_field(entity, "entity_type") or "").lower() == "tool_name"
            ]
            text_support = _tool_trade_support_terms(records.get(trace))
            if not tools:
                continue
            bridge_trace = self._best_corroborating_trace(
                records,
                categories,
                source_trace=trace,
                source_name=_source_name(records.get(trace), trace),
                source_time=_parse_time(get_record_field(records.get(trace), "publish_time") or get_record_field(records.get(trace), "crawl_time")),
                category=categories.get(trace, "unknown"),
            )
            if not bridge_trace:
                continue
            if contacts:
                value = _contact_clue_display_value(
                    _contact_clue_group_key(str(get_record_field(contacts[0], "normalized_value") or get_record_field(contacts[0], "entity_value") or "")),
                    [contacts[0]],
                )
                related_values = [value, *[_bridge_entity_display_value(entity) for entity in tools], *text_support]
            else:
                value = _bridge_entity_display_value(tools[0])
                related_values = [value, *text_support]
            if not value:
                continue
            clues.append(
                self._make_clue(
                    "entity_graph_tool_trade_cluster",
                    value,
                    sorted({trace, bridge_trace}),
                    records,
                    categories,
                    _ordered_strings(related_values),
                    "single_tool_trade_cluster_with_authorized_cross_source_corroboration",
                )
            )
        return clues

    def _best_corroborating_trace(
        self,
        records: dict[str, Any],
        categories: dict[str, str],
        *,
        source_trace: str,
        source_name: str,
        source_time: datetime | None,
        category: str,
    ) -> str:
        candidates: list[tuple[int, int, int, str]] = []
        ordered_traces = _corroboration_trace_order(records)
        try:
            source_index = ordered_traces.index(source_trace)
        except ValueError:
            source_index = -1
        for trace, record in records.items():
            if trace == source_trace:
                continue
            if _source_name(record, trace) == source_name:
                continue
            if not _has_authorized_corroboration_text(record):
                continue
            other_time = _parse_time(get_record_field(record, "publish_time") or get_record_field(record, "crawl_time"))
            if source_time is not None and other_time is not None and abs(other_time - source_time) > timedelta(hours=48):
                continue
            order_distance = len(ordered_traces)
            if source_index >= 0 and trace in records:
                trace_index = ordered_traces.index(trace)
                order_distance = (trace_index - source_index) % max(len(ordered_traces), 1)
                if order_distance == 0:
                    order_distance = len(ordered_traces)
            category_penalty = 1
            if category and category != "unknown" and categories.get(trace) == category:
                category_penalty = 0
            time_distance = 10**9
            if other_time is not None and source_time is not None:
                time_distance = int(abs((other_time - source_time).total_seconds()) // 60)
            candidates.append((order_distance, category_penalty, time_distance, trace))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
            return candidates[0][3]
        return ""

    def _template_clues(self, records: dict[str, Any], categories: dict[str, str]) -> list[RiskClue]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for trace, record in records.items():
            text = normalize_text(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or ""))
            signature = canonicalize_for_dedup(_remove_entities(text))[:80]
            if len(signature) >= 8:
                grouped[signature].append(trace)
        clues: list[RiskClue] = []
        for signature, traces in grouped.items():
            trace_set = sorted(set(traces))
            source_names = {_source_name(records.get(trace), trace) for trace in trace_set}
            if (
                len(trace_set) >= 2
                and len(source_names) >= 2
                and any(categories.get(trace) not in {"normal_noise", "正常业务白噪声"} for trace in trace_set)
            ):
                clues.append(
                    self._make_clue(
                        "shared_template_multi_source",
                        signature,
                        trace_set,
                        records,
                        categories,
                        [signature],
                        "same_template_appears_in_at_least_2_sources",
                    )
                )
            if len(set(traces)) >= 3:
                clues.append(self._make_clue("high_frequency_template", signature, trace_set, records, categories, [signature], "same_template_appears_after_dedup_at_least_3_times"))
        return clues

    def _make_clue(self, clue_type: str, key: str, traces: list[str], records: dict[str, Any], categories: dict[str, str], entity_values: list[str], reason: str) -> RiskClue:
        category_counts = Counter(categories.get(trace, "unknown") for trace in traces)
        risk_category = category_counts.most_common(1)[0][0]
        source_names = sorted({str(get_record_field(records.get(trace), "source_name") or get_record_field(records.get(trace), "source_type") or trace) for trace in traces})
        return RiskClue(
            clue_id=f"clue_{uuid4().hex[:12]}",
            clue_type=clue_type,
            key=key,
            risk_category=risk_category,
            evidence_trace_ids=traces,
            source_names=source_names,
            entity_values=entity_values,
            confidence=round(min(0.98, 0.62 + 0.08 * len(traces) + 0.04 * len(source_names)), 4),
            threshold_reason=reason,
        )

    def _within_48h(self, records: list[Any]) -> bool:
        times = [_parse_time(get_record_field(record, "publish_time") or get_record_field(record, "crawl_time")) for record in records]
        times = [item for item in times if item is not None]
        if len(times) <= 1:
            return True
        return max(times) - min(times) <= timedelta(hours=48)


class PlaybookBuilder:
    """Phase III cheating-playbook synthesis from confirmed risk clues."""

    ELEMENT_RULES = {
        "作案目标": ("抖音", "平台", "业务", "账号", "商家"),
        "招募渠道": ("群", "私聊", "上车", "车队", "telegram", "tg"),
        "话术特征": ("模板", "暗号", "黑话", "引流", "返利"),
        "工具资产": ("群控", "脚本", "工具", "协议号", "外挂"),
        "引流路径": ("http", "domain", "落地", "链接", "开户链接"),
        "账号体系": ("接码", "账号", "实名号", "白号", "养号"),
        "结算方式": ("跑分", "代付", "usdt", "银行卡", "返佣"),
    }

    def build(self, clues: Iterable[RiskClue], records: Iterable[Mapping[str, Any] | Any]) -> list[CheatingPlaybook]:
        clues_by_category: dict[str, list[RiskClue]] = defaultdict(list)
        for clue in clues:
            clues_by_category[clue.risk_category].append(clue)
        text_by_trace = {_trace_id(record): normalize_text(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or "")) for record in records}
        playbooks: list[CheatingPlaybook] = []
        for category, category_clues in clues_by_category.items():
            if len(category_clues) < 2:
                continue
            traces = sorted({trace for clue in category_clues for trace in clue.evidence_trace_ids})
            corpus = " ".join(text_by_trace.get(trace, "") for trace in traces).lower()
            elements: dict[str, list[str]] = {}
            for element, keywords in self.ELEMENT_RULES.items():
                hits = sorted({keyword for keyword in keywords if keyword.lower() in corpus})
                if hits:
                    elements[element] = hits
            if len(elements) >= 2:
                playbooks.append(
                    CheatingPlaybook(
                        playbook_id=f"playbook_{uuid4().hex[:12]}",
                        risk_category=category,
                        clue_ids=[clue.clue_id for clue in category_clues],
                        lifecycle_elements=elements,
                        evidence_trace_ids=traces,
                        confidence=round(min(0.96, 0.58 + 0.07 * len(category_clues) + 0.04 * len(elements)), 4),
                        summary=f"基于 {len(category_clues)} 条风险线索聚合出的 {category} 作弊剧本候选，覆盖 {', '.join(elements)}。",
                    )
                )
        return playbooks


class CountermeasurePlanner:
    """Generate review-only candidate defensive strategies."""

    def plan(self, clues: Iterable[RiskClue], playbooks: Iterable[CheatingPlaybook]) -> list[CountermeasureStrategy]:
        strategies: list[CountermeasureStrategy] = []
        for clue in clues:
            strategies.append(
                CountermeasureStrategy(
                    strategy_id=f"strategy_{uuid4().hex[:12]}",
                    target_id=clue.clue_id,
                    target_type="risk_clue",
                    evidence_trace_ids=clue.evidence_trace_ids,
                    recommendation=f"候选监控：围绕 {clue.clue_type}={clue.key} 建立人工复核优先级和灰度观测规则。",
                    expected_false_positive_surface="同名账号/域名被曝光、辟谣或安全研究语境引用时可能误伤，必须结合上下文白名单复核。",
                    gray_release_scope="review_only_dashboard_then_low_impact_monitoring",
                    allowed_actions=["review_queue_prioritization", "monitoring_candidate", "manual_export_after_approval"],
                    forbidden_actions=["auto_ban", "auto_block", "auto_blacklist", "production_strategy_write"],
                )
            )
        for playbook in playbooks:
            strategies.append(
                CountermeasureStrategy(
                    strategy_id=f"strategy_{uuid4().hex[:12]}",
                    target_id=playbook.playbook_id,
                    target_type="cheating_playbook",
                    evidence_trace_ids=playbook.evidence_trace_ids,
                    recommendation=f"候选对抗方案：对 {playbook.risk_category} 剧本覆盖的 {', '.join(playbook.lifecycle_elements)} 建立分层监控、样本复核和灰度词库更新。",
                    expected_false_positive_surface="涉及公开报道、反诈提醒、研究分析语境时只能建档，不得触发处置。",
                    gray_release_scope="shadow_eval_7d_before_any_policy_change",
                    allowed_actions=["shadow_evaluation", "prompt_eval", "manual_policy_review"],
                    forbidden_actions=["auto_enforce", "auto_intercept", "auto_label_schema_write"],
                )
            )
        return strategies


class CountermeasureSummaryBuilder:
    """Build answer-facing summaries from clues/playbooks without auto-enforcement."""

    def build(
        self,
        clues: Iterable[RiskClue | Mapping[str, Any]],
        playbooks: Iterable[CheatingPlaybook | Mapping[str, Any]] = (),
    ) -> list[CountermeasureSummary]:
        summaries: list[CountermeasureSummary] = []
        for item in [*list(clues), *list(playbooks)]:
            data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            target_id = str(data.get("clue_id") or data.get("playbook_id") or uuid4())
            entities = _ordered_strings(data.get("entity_values") or [])
            lifecycle = data.get("lifecycle_elements") if isinstance(data.get("lifecycle_elements"), Mapping) else {}
            keywords = _ordered_strings([*entities, *[keyword for values in lifecycle.values() for keyword in values]])
            summaries.append(
                CountermeasureSummary(
                    summary_id=f"summary_{uuid4().hex[:12]}",
                    target_id=target_id,
                    suspicious_entities=entities,
                    evidence_trace_ids=_ordered_strings(data.get("evidence_trace_ids") or []),
                    risk_focus=_ordered_strings([data.get("risk_category"), data.get("clue_type")]),
                    review_recommendation="进入人工复核队列；仅做灰度监控和样本补充，不触发自动处置。",
                    monitoring_keywords=keywords[:12],
                    confidence=round(float(data.get("confidence") or 0.0), 4),
                )
            )
        return summaries


def _trace_id(record: Mapping[str, Any] | Any) -> str:
    return str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or get_record_field(record, "hash_id") or uuid4())


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _domain(url: str) -> str:
    text = url.strip().lower().replace("hxxp://", "http://").replace("hxxps://", "https://")
    text = text.split("//", 1)[-1]
    text = text.split("/", 1)[0]
    return text.strip(" .，,;；")


def _contact_clue_group_key(value: str) -> str:
    text = normalize_text(str(value or "")).strip(" ,，。;；")
    if not text:
        return ""
    lowered = text.lower()
    for prefix in ("telegram:", "tg:"):
        if lowered.startswith(prefix):
            handle = text.split(":", 1)[1].lstrip("@").strip()
            return handle.lower() if handle else ""
    if lowered.startswith("@"):
        return text[1:].strip().lower()
    return lowered


def _contact_clue_display_value(group_key: str, group: Iterable[Any]) -> str:
    fallback = ""
    for entity in group:
        raw = normalize_text(str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or "")).strip(" ,，。;；")
        if not raw:
            continue
        fallback = fallback or raw
        lowered = raw.lower()
        if lowered.startswith(("telegram:", "tg:")):
            handle = raw.split(":", 1)[1].lstrip("@").strip()
            return f"Telegram:{handle}" if handle else raw
        if lowered.startswith("@"):
            return f"Telegram:{raw[1:].strip()}"
    return fallback or group_key


def _simple_entity_group_key(value: str) -> str:
    return normalize_text(str(value or "")).strip(" ,，。;；").lower()


def _simple_entity_display_value(group_key: str, group: Iterable[Any]) -> str:
    for entity in group:
        raw = normalize_text(str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or "")).strip(" ,，。;；")
        if raw:
            return raw
    return group_key


def _overlap_entity_display_value(entity: Any) -> str:
    entity_type = str(get_record_field(entity, "entity_type") or "").lower()
    value = str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or "")
    if entity_type in {"domain", "url"}:
        return _domain(value)
    return normalize_text(value).strip(" ,，。;；")


def _bridge_entity_display_value(entity: Any) -> str:
    entity_type = str(get_record_field(entity, "entity_type") or "").lower()
    value = str(get_record_field(entity, "normalized_value") or get_record_field(entity, "entity_value") or "")
    if entity_type in {"domain", "url"}:
        return _domain(value)
    return normalize_text(value).strip(" ,，。;；")


def _bridge_entity_group_key(entity: Any) -> str:
    entity_type = str(get_record_field(entity, "entity_type") or "").lower()
    value = _bridge_entity_display_value(entity)
    if entity_type in {"domain", "url"}:
        return _domain(value)
    return _simple_entity_group_key(value)


def _source_name(record: Any, fallback: str) -> str:
    return str(get_record_field(record, "source_name") or get_record_field(record, "source_type") or fallback)


def _has_authorized_corroboration_text(record: Any) -> bool:
    text = normalize_text(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or ""))
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in ("没有复核", "无复核", "没有授权", "无授权", "没有证据链", "无证据链")):
        return False
    authority_hits = any(marker in text for marker in ("授权样本", "授权记录", "授权来源", "授权情报源", "公开授权", "情报源", "人工确认", "人工标注"))
    corroboration_hits = any(
        marker in text
        for marker in (
            "证据链",
            "复核",
            "相互印证",
            "共同出现",
            "同时出现",
            "同现",
            "两源",
            "两个来源",
            "两条公开授权记录",
            "跨源",
            "共享联系人",
            "确认",
            "人工确认",
            "人工标注",
            "可追溯",
            "复用",
            "记录",
            "给出",
            "公开",
            "同窗出现",
        )
    )
    return authority_hits and corroboration_hits


def _corroboration_trace_order(records: Mapping[str, Any]) -> list[str]:
    indexed: list[tuple[datetime, int, str]] = []
    fallback: list[str] = []
    for index, (trace, record) in enumerate(records.items()):
        observed_time = _parse_time(get_record_field(record, "publish_time") or get_record_field(record, "crawl_time"))
        if observed_time is None:
            fallback.append(trace)
        else:
            indexed.append((observed_time, index, trace))
    if not indexed:
        return list(records)
    indexed.sort(key=lambda item: (item[0], item[1], item[2]))
    return [trace for _time, _index, trace in indexed] + fallback


def _is_contextual_settlement_bridge_value(value: str) -> bool:
    return normalize_text(str(value or "")).strip(" ,，。;；").lower() in {"usdt"}


def _account_tool_support_terms(record: Any) -> list[str]:
    text = normalize_text(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or ""))
    terms = []
    if "账号池" in text:
        terms.append("账号池")
    if "卡密" in text:
        terms.append("卡密")
    if "接码" in text:
        terms.append("接码")
    return _ordered_strings(terms)


def _tool_trade_support_terms(record: Any) -> list[str]:
    text = normalize_text(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or ""))
    terms = []
    for marker in ("群发器", "批量登录", "云控", "群控", "脚本", "卡密", "接码"):
        if marker in text:
            terms.append(marker)
    return _ordered_strings(terms)


def _remove_entities(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:tg|telegram|wx|wechat|qq)\s*[:：]\s*[@\w.-]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"@\w[\w.-]*", " ", text)
    for token in ("tg", "telegram", "微信", "vx", "qq", "http", "https"):
        text = text.replace(token, "")
    return text


def _excerpt(text: str, *, max_chars: int = 160) -> str:
    normalized = normalize_text(text)
    return normalized if len(normalized) <= max_chars else f"{normalized[:max_chars - 1]}…"


def _ordered_strings(values: Iterable[Any] | Any) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        values = [values]
    seen: set[str] = set()
    output: list[str] = []
    for raw in values:
        value = normalize_text(str(raw or ""))
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(value)
    return output


__all__ = [
    "CountermeasurePlanner",
    "CountermeasureSummary",
    "CountermeasureSummaryBuilder",
    "CountermeasureStrategy",
    "CheatingPlaybook",
    "EvidenceChain",
    "EvidenceChainRenderer",
    "PlaybookBuilder",
    "RiskClue",
    "RiskClueAggregator",
]
