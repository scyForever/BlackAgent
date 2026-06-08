"""Risk clue, playbook, and candidate countermeasure generation."""

from __future__ import annotations

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
        clues: list[RiskClue] = []
        clues.extend(self._contact_clues(record_by_trace, category_by_trace, entities))
        clues.extend(self._url_clues(record_by_trace, category_by_trace, entities))
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

    def _template_clues(self, records: dict[str, Any], categories: dict[str, str]) -> list[RiskClue]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for trace, record in records.items():
            text = normalize_text(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or ""))
            signature = canonicalize_for_dedup(_remove_entities(text))[:80]
            if len(signature) >= 8:
                grouped[signature].append(trace)
        clues: list[RiskClue] = []
        for signature, traces in grouped.items():
            if len(set(traces)) >= 3:
                clues.append(self._make_clue("high_frequency_template", signature, sorted(set(traces)), records, categories, [signature], "same_template_appears_after_dedup_at_least_3_times"))
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


def _remove_entities(text: str) -> str:
    text = normalize_text(text)
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
