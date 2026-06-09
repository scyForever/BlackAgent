"""Build a reviewable evidence pack from a multi-source acceptance JSONL.

The pack joins raw collection rows with available classification, entity, and
candidate-clue artifacts. It does not claim missing downstream evidence; rows
without linked clues are marked for follow-up review.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from src.cleaner.text_filter import (
    calculate_noise_score,
    calculate_quality_score,
    canonicalize_for_dedup,
    detect_risk_signal_profile,
    normalize_text,
    shannon_entropy,
    stable_dedup_group_id,
)
from src.collector.source_metadata import source_class_for_record, source_quota_groups_for_record


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INLINE_CLEANING_VERSION = "evidence_pack_inline_cleaner_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build reviewable acceptance evidence pack JSONL.")
    parser.add_argument("--acceptance-pack", default="data/collection_phase_multi_source_acceptance_pack.jsonl")
    parser.add_argument("--cleaned", default="", help="Optional cleaning-phase JSONL used to fill clean_text by trace_id.")
    parser.add_argument("--classifications", default="data/classification_extraction_phase_classifications.jsonl")
    parser.add_argument("--entities", default="data/classification_extraction_phase_entities.jsonl")
    parser.add_argument("--clues", default="", help="Optional candidate-clue JSONL; falls back to no linked clues.")
    parser.add_argument("--hydrated", default="", help="Optional hydrated target-page JSONL used to replace snippet-only source evidence.")
    parser.add_argument("--cleaning-drops", default="", help="Optional cleaning drop JSONL keyed by trace_id/source_trace_id.")
    parser.add_argument("--dropped", default="", help="Optional dropped-row JSONL keyed by trace_id/source_trace_id.")
    parser.add_argument("--output", default="data/collection_phase_multi_source_evidence_pack.jsonl")
    parser.add_argument("--report-out", default="data/collection_phase_multi_source_evidence_pack_report.json")
    return parser.parse_args()


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    resolved = _resolve(path)
    if not resolved.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(rows: Iterable[dict[str, Any]], path: str | Path) -> Path:
    resolved = _resolve(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")
    return resolved


def build_evidence_pack(
    acceptance_rows: list[dict[str, Any]],
    *,
    cleaned: list[dict[str, Any]] | None = None,
    classifications: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    clues: list[dict[str, Any]] | None = None,
    hydrated: list[dict[str, Any]] | None = None,
    cleaning_drops: list[dict[str, Any]] | None = None,
    dropped: list[dict[str, Any]] | None = None,
    output_path: str | Path,
    report_path: str | Path,
) -> dict[str, Any]:
    cleaned_by_trace = _latest_by_trace(cleaned or [])
    cleaning_drop_by_trace = _latest_by_trace([*_dict_rows(cleaning_drops or []), *_dict_rows(dropped or [])])
    classification_by_trace = _latest_by_trace(classifications)
    entities_by_trace = _group_by_trace(entities)
    clues_by_trace = _clues_by_evidence_trace(clues or [])
    hydrated_by_trace, hydrated_by_url = _hydrated_indexes(hydrated or [])

    evidence_rows: list[dict[str, Any]] = []
    completeness_counter: Counter[str] = Counter()
    review_status_counter: Counter[str] = Counter()
    cleaning_source_counter: Counter[str] = Counter()
    source_evidence_counter: Counter[str] = Counter()
    source_evidence_by_category: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in acceptance_rows:
        trace_id = _trace_id(row)
        hydrated_row = hydrated_by_trace.get(trace_id) or hydrated_by_url.get(str(row.get("source_url") or ""))
        source_evidence_row = _source_evidence_row(row, hydrated_row)
        cleaned_row = cleaned_by_trace.get(trace_id) or {}
        inline_cleaned_row = _inline_cleaned_row(trace_id, row) if not cleaned_row.get("clean_text") else {}
        classification = classification_by_trace.get(trace_id) or {}
        row_entities = entities_by_trace.get(trace_id) or []
        row_clues = clues_by_trace.get(trace_id) or []
        raw_snippet = _raw_snippet(row)
        clean_text = str(
            cleaned_row.get("clean_text")
            or inline_cleaned_row.get("clean_text")
            or row.get("clean_text")
            or row.get("normalized_text")
            or row.get("content_text")
            or ""
        )
        review_status = "linked_to_cross_source_clue" if row_clues else "no_cross_source_clue_yet"
        cross_source_clue_chain = [_clue_card(item) for item in row_clues]
        clue_chain = cross_source_clue_chain or [
            _single_record_review_chain(
                trace_id=trace_id,
                row=row,
                classification=classification,
                entities=row_entities,
            )
        ]
        completeness = {
            "has_raw_snippet": bool(raw_snippet),
            "has_clean_text": bool(clean_text),
            "has_cleaning_phase_text": bool(cleaned_row.get("clean_text")),
            "has_inline_cleaning_text": bool(inline_cleaned_row.get("clean_text")),
            "has_auditable_clean_text": bool(cleaned_row.get("clean_text") or inline_cleaned_row.get("clean_text")),
            "has_classification": bool(classification),
            "has_entities": bool(row_entities),
            "has_clue_chain": bool(clue_chain),
            "has_cross_source_clue": bool(row_clues),
        }
        for key, value in completeness.items():
            if value:
                completeness_counter[key] += 1
        review_status_counter[review_status] += 1
        cleaning_card = _cleaning_card(cleaned_row or inline_cleaned_row)
        cleaning_source_counter[cleaning_card.get("source") or "unknown"] += 1
        source_evidence = _source_evidence_card(source_evidence_row, cleaning_drop_by_trace.get(trace_id))
        _count_source_evidence(source_evidence, counter=source_evidence_counter)
        _count_source_evidence(source_evidence, counter=source_evidence_by_category[_acceptance_category(source_evidence_row)])
        evidence_rows.append(
            {
                "trace_id": trace_id,
                "source_name": row.get("source_name"),
                "source_type": row.get("source_type"),
                "source_url": row.get("source_url"),
                "source_access_type": row.get("source_access_type"),
                "acceptance_category": _acceptance_category(row),
                "raw_snippet": raw_snippet,
                "source_evidence": source_evidence,
                "clean_text": clean_text,
                "cleaning": cleaning_card,
                "classification": _classification_card(classification),
                "entities": [_entity_card(item, source_row=row, source_evidence=source_evidence) for item in row_entities],
                "clue_chain": clue_chain,
                "review_chain": {
                    "status": review_status,
                    "steps": [
                        "raw_collection",
                        "clean_text_available" if clean_text else "clean_text_missing",
                        "classification_joined" if classification else "classification_missing",
                        "entities_joined" if row_entities else "entities_missing",
                        "clue_chain_joined" if row_clues else "clue_chain_missing",
                    ],
                },
                "evidence_completeness": completeness,
            }
        )

    output = write_jsonl(evidence_rows, output_path)
    report = {
        "status": "completed" if evidence_rows else "empty",
        "record_count": len(evidence_rows),
        "output": str(output),
        "completeness_counts": dict(completeness_counter),
        "source_evidence_counts": dict(source_evidence_counter),
        "source_evidence_counts_by_category": {
            category: dict(counter)
            for category, counter in sorted(source_evidence_by_category.items())
        },
        "review_status_counts": dict(review_status_counter),
        "cleaning_source_counts": dict(cleaning_source_counter),
        "claim_boundary": "evidence_pack_joins_available_artifacts_missing_clues_are_not_claimed",
    }
    resolved_report = _resolve(report_path)
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    resolved_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _latest_by_trace(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_trace: dict[str, dict[str, Any]] = {}
    for row in rows:
        trace_id = _trace_id(row)
        if trace_id:
            by_trace[trace_id] = row
    return by_trace


def _hydrated_indexes(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_original_trace: dict[str, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    for row in _dict_rows(rows):
        original_trace_id = str(row.get("hydrated_from_trace_id") or "").strip()
        if original_trace_id:
            by_original_trace[original_trace_id] = row
        source_url = str(row.get("source_url") or "").strip()
        if source_url:
            by_url[source_url] = row
    return by_original_trace, by_url


def _source_evidence_row(row: dict[str, Any], hydrated_row: dict[str, Any] | None) -> dict[str, Any]:
    if not hydrated_row:
        return dict(row)
    merged = dict(row)
    raw_snippet = _raw_snippet(row)
    for key in (
        "content_text",
        "raw_text",
        "crawl_time",
        "publish_time",
        "capture_snapshot_uri",
        "raw_payload_uri",
        "ocr_text",
        "ocr_confidence",
        "content_modality",
        "image_path",
        "screenshot_path",
        "attachments",
    ):
        if hydrated_row.get(key) is not None and str(hydrated_row.get(key)) != "":
            merged[key] = hydrated_row.get(key)
    merged["raw_snippet"] = raw_snippet
    merged["source_url"] = row.get("source_url") or hydrated_row.get("source_url")
    if hydrated_row.get("trace_id"):
        merged["hydrated_trace_id"] = hydrated_row.get("trace_id")
    if hydrated_row.get("hydrated_from_trace_id"):
        merged["hydrated_from_trace_id"] = hydrated_row.get("hydrated_from_trace_id")
    return merged


def _group_by_trace(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        trace_id = _trace_id(row)
        if trace_id:
            grouped[trace_id].append(row)
    return dict(grouped)


def _dict_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append(dict(row))
        elif hasattr(row, "model_dump"):
            normalized.append(dict(row.model_dump()))
        elif is_dataclass(row):
            normalized.append(dict(asdict(row)))
    return normalized


def _clues_by_evidence_trace(clues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for clue in clues:
        for trace_id in clue.get("evidence_trace_ids") or []:
            normalized = str(trace_id).strip()
            if normalized:
                grouped[normalized].append(clue)
    return dict(grouped)


def _trace_id(row: dict[str, Any]) -> str:
    return str(row.get("source_trace_id") or row.get("trace_id") or row.get("hash_id") or "").strip()


def _raw_snippet(row: dict[str, Any]) -> str:
    if row.get("raw_snippet") is not None:
        return str(row.get("raw_snippet"))
    return str(row.get("content_text") or row.get("raw_text") or "")[:500]


def _source_evidence_card(row: dict[str, Any], cleaning_drop: dict[str, Any] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {}
    raw_text = _first_text(row, ("content_text", "raw_text"))
    if raw_text is not None:
        card["raw_text"] = raw_text

    raw_snippet = _raw_snippet(row)
    if raw_snippet:
        card["raw_snippet"] = raw_snippet

    for key in (
        "crawl_time",
        "publish_time",
        "source_url",
        "capture_snapshot_uri",
        "raw_payload_uri",
        "hydrated_trace_id",
        "hydrated_from_trace_id",
    ):
        if key in row and row.get(key) is not None:
            card[key] = row.get(key)

    ocr = {
        key: row.get(key)
        for key in ("ocr_text", "ocr_confidence", "content_modality")
        if key in row and row.get(key) is not None
    }
    if ocr:
        card["ocr"] = ocr

    media = {
        key: row.get(key)
        for key in ("image_path", "screenshot_path", "attachments")
        if key in row and row.get(key) is not None
    }
    if media:
        card["media"] = media

    if isinstance(row.get("image_evidence"), list):
        card["image_evidence"] = [dict(item) for item in row["image_evidence"] if isinstance(item, dict)]

    if cleaning_drop:
        card["cleaning_drop"] = dict(cleaning_drop)
    return card


def _count_source_evidence(card: dict[str, Any], *, counter: Counter[str]) -> None:
    if card:
        counter["has_source_evidence"] += 1
    raw_text = str(card.get("raw_text") or "")
    raw_snippet = str(card.get("raw_snippet") or "")
    if raw_text:
        counter["has_raw_text"] += 1
    if raw_snippet:
        counter["has_raw_snippet"] += 1
    if raw_text and raw_snippet and raw_text.strip() != raw_snippet.strip():
        counter["raw_text_differs_from_raw_snippet"] += 1
    if card.get("hydrated_trace_id") or card.get("hydrated_from_trace_id"):
        counter["has_hydrated_body"] += 1
    for key in ("source_url", "crawl_time", "publish_time", "capture_snapshot_uri", "raw_payload_uri"):
        if card.get(key):
            counter[f"has_{key}"] += 1
    if card.get("ocr"):
        counter["has_ocr"] += 1
    if card.get("media"):
        counter["has_media"] += 1
    if card.get("image_evidence"):
        counter["has_image_evidence"] += 1
    if card.get("cleaning_drop"):
        counter["has_cleaning_drop"] += 1


def _first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _classification_card(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "risk_category": row.get("risk_category"),
        "secondary_label": row.get("secondary_label") or row.get("final_secondary_label"),
        "confidence": row.get("confidence"),
        "review_required": row.get("review_required"),
        "evidence": row.get("evidence") or [],
        "conflict_status": row.get("conflict_status"),
    }


def _entity_card(
    row: dict[str, Any],
    *,
    source_row: dict[str, Any] | None = None,
    source_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_row = source_row or {}
    source_evidence = source_evidence or {}
    card = {
        "entity_type": row.get("entity_type"),
        "normalized_value": row.get("normalized_value") or row.get("entity_value"),
        "confidence": row.get("confidence"),
        "context_relevance": row.get("context_relevance"),
    }
    snippet = _entity_source_snippet(row, source_row=source_row, source_evidence=source_evidence)
    if snippet:
        card["source_snippet"] = snippet
    for key in ("source_url", "crawl_time", "raw_payload_uri", "capture_snapshot_uri"):
        card[key] = row.get(key) or source_evidence.get(key) or source_row.get(key)
    return card


def _entity_source_snippet(
    row: dict[str, Any],
    *,
    source_row: dict[str, Any],
    source_evidence: dict[str, Any],
) -> str:
    explicit = str(row.get("source_snippet") or row.get("context") or "").strip()
    if explicit:
        return explicit
    text = str(
        source_evidence.get("raw_snippet")
        or source_evidence.get("raw_text")
        or source_row.get("raw_snippet")
        or source_row.get("content_text")
        or source_row.get("raw_text")
        or ""
    )
    if not text:
        return ""
    start = _optional_int(row.get("start") or row.get("start_offset"))
    end = _optional_int(row.get("end") or row.get("end_offset"))
    if start is None or end is None or start < 0 or end <= start or start >= len(text):
        return text[:240]
    left = max(0, start - 40)
    right = min(len(text), end + 40)
    return text[left:right]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clue_card(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "clue_id": row.get("clue_id"),
        "clue_type": row.get("clue_type"),
        "risk_category": row.get("risk_category"),
        "key": row.get("key"),
        "evidence_trace_ids": [str(item) for item in (row.get("evidence_trace_ids") or [])],
        "source_names": [str(item) for item in (row.get("source_names") or [])],
        "confidence": row.get("confidence"),
        "quality_score": row.get("quality_score"),
    }


def _single_record_review_chain(
    *,
    trace_id: str,
    row: dict[str, Any],
    classification: dict[str, Any],
    entities: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "no_cross_source_clue_yet",
        "clue_type": "single_record_review_chain",
        "risk_category": classification.get("risk_category"),
        "key": _single_record_key(row, classification, entities),
        "evidence_trace_ids": [trace_id] if trace_id else [],
        "source_names": [str(row.get("source_name"))] if row.get("source_name") else [],
        "confidence": classification.get("confidence"),
        "quality_score": None,
        "claim_boundary": "single_record_evidence_chain_not_cross_source_clue",
    }


def _single_record_key(
    row: dict[str, Any],
    classification: dict[str, Any],
    entities: list[dict[str, Any]],
) -> str:
    entity_values = [
        str(item.get("normalized_value") or item.get("entity_value") or "").strip()
        for item in entities
        if str(item.get("normalized_value") or item.get("entity_value") or "").strip()
    ]
    if entity_values:
        return entity_values[0]
    return str(classification.get("risk_category") or row.get("query_term") or row.get("source_name") or "unlinked_record")


def _cleaning_card(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {"source": "raw_fallback", "quality_score": None, "risk_level": None}
    card = {
        "source": "cleaning_phase",
        "quality_score": row.get("quality_score"),
        "risk_score": row.get("risk_score"),
        "risk_level": row.get("risk_level"),
        "risk_categories": row.get("risk_categories") or [],
        "risk_markers": row.get("risk_markers") or [],
    }
    source = str(row.get("_cleaning_source") or "").strip()
    if source:
        card["source"] = source
    for key in ("cleaning_version", "noise_score", "dedup_group_id", "text_entropy", "claim_boundary"):
        if key in row:
            card[key] = row.get(key)
    return card


def _inline_cleaned_row(trace_id: str, row: dict[str, Any]) -> dict[str, Any]:
    raw_text = str(row.get("clean_text") or row.get("normalized_text") or row.get("content_text") or row.get("raw_text") or "")
    clean_text = normalize_text(raw_text)[:4000]
    if not clean_text:
        return {}

    profile = detect_risk_signal_profile(clean_text, extra_terms=_inline_extra_terms(row))
    noise_score = calculate_noise_score(clean_text)
    entropy = shannon_entropy(clean_text)
    canonical = canonicalize_for_dedup(clean_text)
    quality_score = calculate_quality_score(
        clean_text,
        noise_score=noise_score,
        risk_score=profile.risk_score,
        entropy=entropy,
    )
    return {
        "source_trace_id": trace_id,
        "clean_text": clean_text,
        "_cleaning_source": "evidence_pack_inline_cleaning",
        "cleaning_version": INLINE_CLEANING_VERSION,
        "noise_score": noise_score,
        "dedup_group_id": stable_dedup_group_id(canonical or clean_text),
        "quality_score": quality_score,
        "risk_score": profile.risk_score,
        "risk_level": profile.risk_level,
        "risk_categories": list(profile.risk_categories),
        "risk_markers": list(profile.risk_markers),
        "text_entropy": entropy,
        "claim_boundary": "cleaned_inline_for_acceptance_evidence_not_persisted_cleaning_phase",
    }


def _inline_extra_terms(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("query_term", "risk_category", "secondary_label", "source_class"):
        value = str(row.get(key) or "").strip()
        if value:
            values.append(value)
    return values


def _acceptance_category(row: dict[str, Any]) -> str:
    for key in ("acceptance_category", "source_quota_group", "source_class"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    quota_groups = set(source_quota_groups_for_record(row))
    if "public_account_or_article" in quota_groups:
        return "public_account_or_article"
    source_class = source_class_for_record(row)
    if source_class and source_class != "other_authorized":
        return source_class
    return ""


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()


def main() -> int:
    args = parse_args()
    report = build_evidence_pack(
        load_jsonl(args.acceptance_pack),
        cleaned=load_jsonl(args.cleaned) if args.cleaned else [],
        classifications=load_jsonl(args.classifications),
        entities=load_jsonl(args.entities),
        clues=load_jsonl(args.clues) if args.clues else [],
        hydrated=load_jsonl(args.hydrated) if args.hydrated else [],
        cleaning_drops=load_jsonl(args.cleaning_drops) if args.cleaning_drops else [],
        dropped=load_jsonl(args.dropped) if args.dropped else [],
        output_path=args.output,
        report_path=args.report_out,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
