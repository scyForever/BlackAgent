"""Quality scoring for aggregated risk clues."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from src.collector.base_collector import get_record_field


@dataclass(frozen=True)
class ClueQualityAssessment:
    clue_id: str
    quality_score: float
    quality_level: str
    pass_threshold: bool
    review_required: bool
    cross_source_count: int
    evidence_count: int
    avg_classification_confidence: float
    critical_entity_count: int
    quality_reasons: list[str]
    freshness_score: float = 0.5
    freshness_reasons: list[str] | None = None
    false_positive_risk_score: float = 0.0
    false_positive_risk_reasons: list[str] | None = None

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["freshness_reasons"] = payload.get("freshness_reasons") or []
        payload["false_positive_risk_reasons"] = payload.get("false_positive_risk_reasons") or []
        return payload


class ClueQualityEvaluator:
    """Score clue usability against user-intent quality expectations."""

    CRITICAL_ENTITY_TYPES = {"contact", "account", "url", "domain"}

    def evaluate_many(
        self,
        clues: Iterable[Mapping[str, Any] | Any],
        *,
        classifications: Iterable[Mapping[str, Any] | Any],
        entities: Iterable[Mapping[str, Any] | Any],
        quality_profile: str,
        require_cross_source: bool,
        require_evidence_chain: bool,
    ) -> list[ClueQualityAssessment]:
        conf_by_trace = {
            str(get_record_field(item, "source_trace_id") or get_record_field(item, "trace_id") or ""): float(get_record_field(item, "confidence") or 0.0)
            for item in classifications
        }
        entities_by_trace: dict[str, list[Any]] = {}
        for entity in entities:
            trace_id = str(get_record_field(entity, "source_trace_id") or get_record_field(entity, "trace_id") or "")
            entities_by_trace.setdefault(trace_id, []).append(entity)

        return [
            self.evaluate_one(
                clue,
                conf_by_trace=conf_by_trace,
                entities_by_trace=entities_by_trace,
                quality_profile=quality_profile,
                require_cross_source=require_cross_source,
                require_evidence_chain=require_evidence_chain,
            )
            for clue in clues
        ]

    def evaluate_one(
        self,
        clue: Mapping[str, Any] | Any,
        *,
        conf_by_trace: Mapping[str, float],
        entities_by_trace: Mapping[str, list[Any]],
        quality_profile: str,
        require_cross_source: bool,
        require_evidence_chain: bool,
    ) -> ClueQualityAssessment:
        clue_id = str(get_record_field(clue, "clue_id") or "unknown_clue")
        evidence_trace_ids = [str(item) for item in (get_record_field(clue, "evidence_trace_ids") or [])]
        source_names = [str(item) for item in (get_record_field(clue, "source_names") or [])]
        base_confidence = float(get_record_field(clue, "confidence") or 0.0)
        avg_confidence = 0.0
        if evidence_trace_ids:
            avg_confidence = round(sum(conf_by_trace.get(trace_id, 0.0) for trace_id in evidence_trace_ids) / len(evidence_trace_ids), 4)

        critical_entity_count = 0
        for trace_id in evidence_trace_ids:
            for entity in entities_by_trace.get(trace_id, []):
                entity_type = str(get_record_field(entity, "entity_type") or "").lower()
                if entity_type in self.CRITICAL_ENTITY_TYPES:
                    critical_entity_count += 1

        cross_source_count = len(set(source_names))
        evidence_count = len(set(evidence_trace_ids))
        freshness_score, freshness_reasons = _freshness_score(clue)
        false_positive_risk_score, false_positive_risk_reasons = _false_positive_risk_score(
            cross_source_count=cross_source_count,
            evidence_count=evidence_count,
            avg_confidence=avg_confidence,
            critical_entity_count=critical_entity_count,
            clue_entity_values=[str(item) for item in (get_record_field(clue, "entity_values") or []) if str(item).strip()],
            freshness_score=freshness_score,
        )
        score = (
            base_confidence * 0.45
            + min(evidence_count, 4) / 4.0 * 0.2
            + min(cross_source_count, 3) / 3.0 * 0.2
            + avg_confidence * 0.15
            + freshness_score * 0.08
            - false_positive_risk_score * 0.12
        )
        reasons: list[str] = []
        if cross_source_count >= 2:
            reasons.append("cross_source_confirmed")
        if evidence_count >= 3:
            reasons.append("sufficient_evidence_samples")
        if critical_entity_count > 0:
            reasons.append("critical_entities_present")
        if avg_confidence >= 0.75:
            reasons.append("classification_confidence_stable")
        reasons.extend(freshness_reasons)
        if false_positive_risk_score < 0.4:
            reasons.append("false_positive_risk_low")
        elif false_positive_risk_score >= 0.7:
            reasons.append("false_positive_risk_high")

        if require_cross_source and cross_source_count < 2:
            score -= 0.2
            reasons.append("missing_required_cross_source")
        if require_evidence_chain and evidence_count < 2:
            score -= 0.15
            reasons.append("weak_evidence_chain")
        clue_entity_values = [str(item) for item in (get_record_field(clue, "entity_values") or []) if str(item).strip()]
        if critical_entity_count == 0 and not clue_entity_values:
            score -= 0.15
            reasons.append("missing_critical_entities")
        elif critical_entity_count == 0 and clue_entity_values:
            reasons.append("critical_entity_implied_by_clue_key")

        score = round(max(0.0, min(score, 0.99)), 4)
        threshold = _quality_threshold(quality_profile)
        passed = score >= threshold
        level = "high" if score >= max(threshold, 0.82) else ("medium" if score >= threshold else "low")
        return ClueQualityAssessment(
            clue_id=clue_id,
            quality_score=score,
            quality_level=level,
            pass_threshold=passed,
            review_required=(not passed) or level != "high",
            cross_source_count=cross_source_count,
            evidence_count=evidence_count,
            avg_classification_confidence=avg_confidence,
            critical_entity_count=critical_entity_count,
            freshness_score=freshness_score,
            freshness_reasons=freshness_reasons,
            false_positive_risk_score=false_positive_risk_score,
            false_positive_risk_reasons=false_positive_risk_reasons,
            quality_reasons=reasons,
        )


def build_evidence_reviewability(
    clue: Mapping[str, Any] | Any,
    *,
    assessment: ClueQualityAssessment | Mapping[str, Any] | None = None,
    entities: Iterable[Mapping[str, Any] | Any] = (),
    records: Iterable[Mapping[str, Any] | Any] = (),
) -> dict[str, Any]:
    """Build analyst-review metadata from existing clue/evidence fields."""

    evidence_trace_ids = _ordered_strings(get_record_field(clue, "evidence_trace_ids") or [])
    source_names = _ordered_strings(get_record_field(clue, "source_names") or [])
    source_count = max(len(set(source_names)), _optional_int(get_record_field(clue, "source_count")) or 0)
    evidence_count = len(set(evidence_trace_ids))
    entity_support = _entity_support(clue, evidence_trace_ids=evidence_trace_ids, entities=entities)
    snippets = _evidence_snippets(clue, evidence_trace_ids=evidence_trace_ids, records=records)
    time_range = _evidence_time_range(clue, evidence_trace_ids=evidence_trace_ids, records=records)
    risk_score, risk_reasons = _reviewability_false_positive_risk(
        clue,
        assessment=assessment,
        source_count=source_count,
        evidence_count=evidence_count,
        entity_support_count=len(entity_support),
    )
    risk_level = "high" if risk_score >= 0.7 else ("medium" if risk_score >= 0.4 else "low")
    review_action_reasons: list[str] = []
    if source_count < 2:
        review_action_reasons.append("verify_single_source_support")
    if evidence_count < 2:
        review_action_reasons.append("verify_evidence_chain_depth")
    if not entity_support:
        review_action_reasons.append("verify_entity_support_missing")
    if not snippets:
        review_action_reasons.append("verify_original_snippets_missing")
    if time_range["start"] is None and time_range["end"] is None:
        review_action_reasons.append("verify_observed_time_missing")

    if risk_level == "high" or source_count < 2 or evidence_count < 2 or not entity_support:
        suggested_action = "human_verify_single_source_or_weak_entity_support"
    elif not snippets or time_range["start"] is None or time_range["end"] is None:
        suggested_action = "verify_missing_snippets_or_observed_time"
    else:
        suggested_action = "review_original_snippets_and_confirm_entity_linkage"

    return {
        "source_count": source_count,
        "evidence_count": evidence_count,
        "entity_support_count": len(entity_support),
        "entity_support": entity_support,
        "original_snippets": snippets,
        "time_range": time_range,
        "observed_time": time_range["end"] or time_range["start"],
        "false_positive_risk": {
            "score": risk_score,
            "level": risk_level,
            "reasons": risk_reasons,
        },
        "suggested_review_action": suggested_action,
        "review_action_reasons": review_action_reasons,
    }


def _quality_threshold(profile: str) -> float:
    normalized = str(profile or "").strip().lower()
    if normalized == "high_precision":
        return 0.78
    if normalized == "high_recall":
        return 0.52
    return 0.65


def _freshness_score(clue: Mapping[str, Any] | Any) -> tuple[float, list[str]]:
    reference = _parse_time(
        get_record_field(clue, "quality_reference_time")
        or get_record_field(clue, "reference_time")
        or get_record_field(clue, "evaluated_at")
    ) or datetime.now(timezone.utc)
    seen_at = _parse_time(
        get_record_field(clue, "last_seen")
        or get_record_field(clue, "publish_time")
        or get_record_field(clue, "created_at")
    )
    if seen_at is None:
        return 0.5, ["freshness_unknown"]
    age_hours = max(0.0, (reference - seen_at).total_seconds() / 3600.0)
    if age_hours <= 48:
        return 1.0, ["fresh_evidence_window"]
    if age_hours <= 168:
        return 0.75, ["recent_evidence_window"]
    if age_hours <= 336:
        return 0.5, ["aging_evidence_window"]
    return 0.25, ["stale_evidence_window"]


def _false_positive_risk_score(
    *,
    cross_source_count: int,
    evidence_count: int,
    avg_confidence: float,
    critical_entity_count: int,
    clue_entity_values: list[str],
    freshness_score: float,
) -> tuple[float, list[str]]:
    risk = 0.1
    reasons: list[str] = []
    if cross_source_count < 2:
        risk += 0.25
        reasons.append("single_source_false_positive_risk")
    if evidence_count < 2:
        risk += 0.2
        reasons.append("thin_evidence_false_positive_risk")
    if critical_entity_count == 0 and not clue_entity_values:
        risk += 0.25
        reasons.append("weak_entity_support_false_positive_risk")
    elif critical_entity_count == 0:
        risk += 0.1
        reasons.append("entity_support_implied_not_extracted")
    if avg_confidence < 0.55:
        risk += 0.2
        reasons.append("low_classification_confidence_false_positive_risk")
    if freshness_score <= 0.25:
        risk += 0.1
        reasons.append("stale_context_false_positive_risk")
    risk = round(max(0.0, min(risk, 0.99)), 4)
    if not reasons:
        reasons.append("false_positive_risk_low")
    return risk, reasons


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ordered_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        values = [str(value)]
    else:
        try:
            values = [str(item) for item in value]
        except TypeError:
            values = [str(value)]
    output: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _entity_support(
    clue: Mapping[str, Any] | Any,
    *,
    evidence_trace_ids: list[str],
    entities: Iterable[Mapping[str, Any] | Any],
) -> list[dict[str, Any]]:
    trace_set = set(evidence_trace_ids)
    support: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entity in entities:
        trace_id = str(get_record_field(entity, "source_trace_id") or get_record_field(entity, "trace_id") or "").strip()
        if trace_set and trace_id and trace_id not in trace_set:
            continue
        entity_type = str(get_record_field(entity, "entity_type") or "").strip().lower()
        value = str(
            get_record_field(entity, "normalized_value")
            or get_record_field(entity, "entity_value")
            or get_record_field(entity, "raw_value")
            or ""
        ).strip()
        if not value:
            continue
        key = (entity_type, value, trace_id)
        if key in seen:
            continue
        seen.add(key)
        support.append({"entity_type": entity_type or "unknown", "value": value, "source_trace_id": trace_id or None})
    for value in _ordered_strings(get_record_field(clue, "entity_values") or []):
        key = ("clue_entity_value", value, "")
        if key in seen:
            continue
        seen.add(key)
        support.append({"entity_type": "clue_entity_value", "value": value, "source_trace_id": None})
    return support


def _evidence_snippets(
    clue: Mapping[str, Any] | Any,
    *,
    evidence_trace_ids: list[str],
    records: Iterable[Mapping[str, Any] | Any],
) -> list[str]:
    for field in ("original_snippets", "evidence_snippets", "snippets", "text_snippets"):
        snippets = _ordered_strings(get_record_field(clue, field) or [])
        if snippets:
            return [_snippet(item) for item in snippets[:5]]

    trace_set = set(evidence_trace_ids)
    output: list[str] = []
    seen: set[str] = set()
    for record in records:
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "").strip()
        if trace_set and trace_id not in trace_set:
            continue
        text = str(
            get_record_field(record, "content_text")
            or get_record_field(record, "clean_text")
            or get_record_field(record, "raw_text")
            or get_record_field(record, "text")
            or ""
        ).strip()
        if not text:
            continue
        item = _snippet(text)
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
        if len(output) >= 5:
            break
    return output


def _snippet(text: str, *, limit: int = 240) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _evidence_time_range(
    clue: Mapping[str, Any] | Any,
    *,
    evidence_trace_ids: list[str],
    records: Iterable[Mapping[str, Any] | Any],
) -> dict[str, str | None]:
    direct_start = _first_text_field(clue, ("first_seen", "time_range_start", "start_time"))
    direct_end = _first_text_field(clue, ("last_seen", "observed_time", "time_range_end", "end_time", "publish_time", "created_at"))
    if direct_start or direct_end:
        return {"start": direct_start or direct_end, "end": direct_end or direct_start}

    trace_set = set(evidence_trace_ids)
    observed: list[str] = []
    for record in records:
        trace_id = str(get_record_field(record, "source_trace_id") or get_record_field(record, "trace_id") or "").strip()
        if trace_set and trace_id not in trace_set:
            continue
        value = _first_text_field(record, ("publish_time", "created_at", "crawl_time", "updated_at"))
        if value:
            observed.append(value)
    if not observed:
        return {"start": None, "end": None}
    return {"start": _pick_time_string(observed, earliest=True), "end": _pick_time_string(observed, earliest=False)}


def _first_text_field(item: Mapping[str, Any] | Any, fields: Iterable[str]) -> str | None:
    for field in fields:
        value = get_record_field(item, field)
        text = str(value or "").strip()
        if text:
            return text
    return None


def _pick_time_string(values: Iterable[str], *, earliest: bool) -> str | None:
    candidates = [str(value) for value in values if str(value).strip()]
    if not candidates:
        return None

    def key(value: str) -> tuple[datetime, str]:
        parsed = _parse_time(value)
        if parsed is None:
            fallback = datetime.max.replace(tzinfo=timezone.utc) if earliest else datetime.min.replace(tzinfo=timezone.utc)
            return fallback, value
        return parsed.astimezone(timezone.utc), value

    return min(candidates, key=key) if earliest else max(candidates, key=key)


def _reviewability_false_positive_risk(
    clue: Mapping[str, Any] | Any,
    *,
    assessment: ClueQualityAssessment | Mapping[str, Any] | None,
    source_count: int,
    evidence_count: int,
    entity_support_count: int,
) -> tuple[float, list[str]]:
    if assessment is not None:
        score = get_record_field(assessment, "false_positive_risk_score")
        reasons = _ordered_strings(get_record_field(assessment, "false_positive_risk_reasons") or [])
        if score is not None:
            return round(max(0.0, min(float(score), 0.99)), 4), reasons or ["false_positive_risk_assessed"]
    quality = get_record_field(clue, "quality") if isinstance(get_record_field(clue, "quality"), Mapping) else {}
    if isinstance(quality, Mapping) and quality.get("false_positive_risk_score") is not None:
        return (
            round(max(0.0, min(float(quality.get("false_positive_risk_score") or 0.0), 0.99)), 4),
            _ordered_strings(quality.get("false_positive_risk_reasons") or []) or ["false_positive_risk_assessed"],
        )
    risk = 0.1
    reasons: list[str] = []
    if source_count < 2:
        risk += 0.25
        reasons.append("single_source_false_positive_risk")
    if evidence_count < 2:
        risk += 0.2
        reasons.append("thin_evidence_false_positive_risk")
    if entity_support_count == 0:
        risk += 0.25
        reasons.append("weak_entity_support_false_positive_risk")
    try:
        confidence = float(get_record_field(clue, "confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.55:
        risk += 0.2
        reasons.append("low_confidence_false_positive_risk")
    if not reasons:
        reasons.append("false_positive_risk_low")
    return round(max(0.0, min(risk, 0.99)), 4), reasons


__all__ = ["ClueQualityAssessment", "ClueQualityEvaluator", "build_evidence_reviewability"]
