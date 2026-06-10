"""Candidate clue retrieval for online investigation requests."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.enhancement.clue_quality import build_evidence_reviewability


class ClueRetriever:
    """Rank candidate clues using cheap lexical and metadata overlap."""

    def retrieve(
        self,
        clues: Iterable[Mapping[str, Any] | Any],
        *,
        query: str,
        intent: Mapping[str, Any],
        limit: int = 50,
        time_range_hours: int | None = None,
        allowed_source_types: Iterable[str] = (),
        allowed_risk_types: Iterable[str] = (),
        min_quality_score: float | None = None,
        entity_graph: Any | None = None,
    ) -> list[dict[str, Any]]:
        materialized_clues = [*(_normalize(item) for item in clues), *_graph_clues(entity_graph)]
        query_tokens = _tokens(query)
        intent_risk_types = {str(item).lower() for item in (intent.get("risk_types") or [])}
        filter_risk_types = {str(item).lower() for item in (allowed_risk_types or [])}
        source_prefs = {str(item).lower() for item in (intent.get("source_preferences") or [])}
        source_type_filters = {_normalize_source_type(item) for item in allowed_source_types if _normalize_source_type(item)}
        require_cross_source = bool(intent.get("require_cross_source", False))
        now = _utc_now()

        scored: list[tuple[float, dict[str, Any]]] = []
        for clue in materialized_clues:
            if min_quality_score is not None and float(clue.get("quality_score") or 0.0) < float(min_quality_score):
                continue
            if filter_risk_types and not any(risk in str(clue.get("risk_category") or "").lower() for risk in filter_risk_types):
                continue
            if time_range_hours is not None and not _within_hours(clue, now=now, hours=time_range_hours):
                if time_range_hours <= 24 or not _soft_match(query_tokens, clue):
                    continue
                clue["retrieval_stale"] = True
            if source_type_filters and not _match_source_types(clue, source_type_filters):
                continue
            score = 0.0
            risk_category = str(clue.get("risk_category") or "").lower()
            clue_text = " ".join(
                [
                    str(clue.get("clue_type") or ""),
                    str(clue.get("key") or ""),
                    " ".join(str(item) for item in (clue.get("entity_values") or [])),
                    " ".join(str(item) for item in (clue.get("source_names") or [])),
                ]
            ).lower()
            overlap = len(query_tokens.intersection(_tokens(clue_text)))
            score += min(overlap, 6) * 0.12
            if any(risk in risk_category for risk in intent_risk_types):
                score += 0.35
            source_names_text = " ".join(str(item).lower() for item in (clue.get("source_names") or []))
            if any(pref in source_names_text for pref in source_prefs):
                score += 0.15
            cross_source_count = len(set(str(item) for item in (clue.get("source_names") or [])))
            if require_cross_source and cross_source_count >= 2:
                score += 0.2
            if clue.get("entity_graph_backend"):
                score += 0.18
                clue.setdefault("retrieval_source", "entity_graph")
            score += min(float(clue.get("quality_score") or 0.0), 1.0) * 0.1
            score += min(float(clue.get("confidence") or 0.0), 1.0) * 0.08
            clue["retrieval_score"] = round(score, 4)
            clue.setdefault("evidence_reviewability", build_evidence_reviewability(clue))
            scored.append((score, clue))

        scored.sort(key=lambda item: (item[0], float(item[1].get("quality_score") or 0.0), float(item[1].get("confidence") or 0.0)), reverse=True)
        positive = [item for score, item in scored if score > 0]
        if positive:
            return positive[:limit]
        return [item for _, item in scored[: min(limit, 10)]]


def _graph_clues(entity_graph: Any | None) -> list[dict[str, Any]]:
    if entity_graph is None:
        return []
    if isinstance(entity_graph, Mapping):
        generated = entity_graph.get("clues") or []
    elif hasattr(entity_graph, "generate_clues"):
        generated = entity_graph.generate_clues()
    elif hasattr(entity_graph, "snapshot"):
        generated = _clues_from_graph_snapshot(entity_graph.snapshot())
    else:
        generated = []
    return [_normalize(item) for item in generated]


def _clues_from_graph_snapshot(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    observations = [dict(item) for item in snapshot.get("observations") or [] if isinstance(item, Mapping)]
    entities = [dict(item) for item in snapshot.get("entities") or [] if isinstance(item, Mapping)]
    observations_by_entity: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        entity_id = str(observation.get("entity_id") or "")
        if entity_id:
            observations_by_entity.setdefault(entity_id, []).append(observation)
    clues: list[dict[str, Any]] = []
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "")
        entity_observations = observations_by_entity.get(entity_id, [])
        sources = sorted({str(item.get("source_name") or item.get("source_type") or item.get("trace_id") or "") for item in entity_observations if str(item.get("source_name") or item.get("source_type") or item.get("trace_id") or "").strip()})
        traces = sorted({str(item.get("trace_id") or "") for item in entity_observations if str(item.get("trace_id") or "").strip()})
        if len(sources) < 2 or len(traces) < 2:
            continue
        clue = {
            "clue_id": f"graph_snapshot:{entity_id}",
            "clue_type": "graph_shared_entity_cross_source",
            "key": entity_id,
            "risk_category": "unknown",
            "source_names": sources,
            "evidence_trace_ids": traces,
            "entity_values": [str(entity.get("masked_display_value") or entity_id)],
            "confidence": 0.74,
            "quality_score": 0.7,
            "entity_asset_id": entity_id,
            "entity_observation_refs": [str(item.get("observation_id") or "") for item in entity_observations if item.get("observation_id")],
            "entity_graph_backend": "entity_graph_snapshot",
        }
        clue["evidence_reviewability"] = build_evidence_reviewability(
            clue,
            entities=[
                {
                    "source_trace_id": item.get("trace_id"),
                    "entity_type": entity.get("entity_type"),
                    "normalized_value": entity.get("masked_display_value") or entity_id,
                }
                for item in entity_observations
            ],
        )
        clues.append(clue)
    return clues


def _normalize(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


def _tokens(text: str) -> set[str]:
    normalized = "".join(ch.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" else " " for ch in str(text or ""))
    chunks = {chunk for chunk in normalized.split() if chunk}
    chinese_chars = {ch for ch in normalized if "\u4e00" <= ch <= "\u9fff"}
    return chunks.union(chinese_chars)


def _normalize_source_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"telegram", "tg", "im", "chat"}:
        return "im"
    if text in {"forum", "论坛", "贴吧"}:
        return "forum"
    if text in {"threat_intel", "feed", "intel"}:
        return "threat_intel"
    return text


def _match_source_types(clue: Mapping[str, Any], allowed: set[str]) -> bool:
    clue_types = {_normalize_source_type(item) for item in (clue.get("source_types") or []) if _normalize_source_type(item)}
    if clue_types:
        return bool(clue_types.intersection(allowed))
    source_names_text = " ".join(str(item).lower() for item in (clue.get("source_names") or []))
    if "tg" in source_names_text or "telegram" in source_names_text:
        return "im" in allowed
    if "forum" in source_names_text:
        return "forum" in allowed
    if "feed" in source_names_text or "intel" in source_names_text:
        return "threat_intel" in allowed
    return False


def _soft_match(query_tokens: set[str], clue: Mapping[str, Any]) -> bool:
    clue_text = " ".join(
        [
            str(clue.get("risk_category") or ""),
            str(clue.get("key") or ""),
            " ".join(str(item) for item in (clue.get("entity_values") or [])),
            " ".join(str(item) for item in (clue.get("source_names") or [])),
        ]
    )
    return bool(query_tokens.intersection(_tokens(clue_text)))


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _within_hours(clue: Mapping[str, Any], *, now, hours: int) -> bool:
    from datetime import datetime, timedelta, timezone

    for field in ("last_seen", "updated_at", "created_at"):
        value = clue.get(field)
        if not value:
            continue
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return now - parsed <= timedelta(hours=hours)
        except ValueError:
            continue
    return True
