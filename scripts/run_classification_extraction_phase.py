"""Run classification/extraction on collection-phase raw records."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.enhancement.source_intake import AuthorizedSourcePolicy, MultimodalTextExtractor
from src.enhancement.text_intelligence import AdaptiveEntropyFilter, AdvancedEntityExtractor, FineGrainedIntentClassifier
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run classification/extraction phase over collection-phase raw records.")
    parser.add_argument("--db", default="data/collection_phase_delivery.db", help="SQLite DB path to read raw_records from")
    parser.add_argument(
        "--summary-out",
        default="data/classification_extraction_phase_summary.json",
        help="Summary JSON output path",
    )
    parser.add_argument(
        "--classifications-jsonl",
        default="data/classification_extraction_phase_classifications.jsonl",
        help="Classification JSONL output path",
    )
    parser.add_argument(
        "--entities-jsonl",
        default="data/classification_extraction_phase_entities.jsonl",
        help="Entity JSONL output path",
    )
    parser.add_argument(
        "--only-labeled",
        action="store_true",
        help="Only run Phase II/III on rows that still carry at least one matched_themes label",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "raw", "cleaned"),
        default="auto",
        help="Input source for classification/extraction: raw_records, cleaned_texts, or auto-detect cleaned_texts first",
    )
    parser.add_argument(
        "--high-risk-only",
        action="store_true",
        help="When consuming cleaned_texts, only keep rows with risk_level HIGH or CRITICAL",
    )
    parser.add_argument(
        "--min-quality-score",
        type=float,
        default=0.0,
        help="Optional minimum quality_score threshold when consuming cleaned_texts",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max raw rows to process (0 = all)")
    return parser.parse_args()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_phase_rows(backend: Any, *, source: str, limit: int = 0, high_risk_only: bool = False, min_quality_score: float = 0.0) -> tuple[list[dict[str, Any]], str, int]:
    raw_rows = backend.list_raw()
    raw_by_trace = {
        str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id")): dict(row)
        for row in raw_rows
    }
    cleaned_rows = backend.list_cleaned()

    use_cleaned = source == "cleaned" or (source == "auto" and bool(cleaned_rows))
    if not use_cleaned:
        rows = raw_rows[: limit] if limit and limit > 0 else raw_rows
        return rows, "raw", len(raw_rows)

    filtered_cleaned: list[dict[str, Any]] = []
    for cleaned in cleaned_rows:
        risk_level = str(cleaned.get("risk_level") or "NONE").upper()
        quality_score = float(cleaned.get("quality_score", 0.0) or 0.0)
        if high_risk_only and risk_level not in {"HIGH", "CRITICAL"}:
            continue
        if quality_score < float(min_quality_score or 0.0):
            continue
        trace_id = str(cleaned.get("source_trace_id") or "")
        raw = raw_by_trace.get(trace_id, {})
        merged = {**raw, **cleaned}
        merged.setdefault("trace_id", trace_id)
        merged.setdefault("source_trace_id", trace_id)
        filtered_cleaned.append(merged)

    rows = filtered_cleaned[: limit] if limit and limit > 0 else filtered_cleaned
    return rows, "cleaned", len(cleaned_rows)


def main() -> int:
    args = parse_args()
    db_path = (PROJECT_ROOT / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db).resolve()
    summary_out = (PROJECT_ROOT / args.summary_out).resolve() if not Path(args.summary_out).is_absolute() else Path(args.summary_out).resolve()
    classifications_out = (
        (PROJECT_ROOT / args.classifications_jsonl).resolve()
        if not Path(args.classifications_jsonl).is_absolute()
        else Path(args.classifications_jsonl).resolve()
    )
    entities_out = (
        (PROJECT_ROOT / args.entities_jsonl).resolve()
        if not Path(args.entities_jsonl).is_absolute()
        else Path(args.entities_jsonl).resolve()
    )

    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    rows, resolved_source, source_total_count = _load_phase_rows(
        backend,
        source=args.source,
        limit=args.limit,
        high_risk_only=bool(args.high_risk_only),
        min_quality_score=float(args.min_quality_score or 0.0),
    )
    raw_snapshot_record_count = len(backend.list_raw())
    cleaned_snapshot_record_count = len(backend.list_cleaned())
    backend.close()

    selected_rows = rows
    if args.only_labeled:
        selected_rows = [row for row in rows if any(str(item).strip() for item in (row.get("matched_themes") or []))]

    trace_to_source = {
        str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id")): str(row.get("source_name") or "unknown")
        for row in selected_rows
    }
    input_theme_counts: Counter[str] = Counter()
    for row in selected_rows:
        for theme in {str(item) for item in (row.get("matched_themes") or []) if str(item).strip()}:
            input_theme_counts[theme] += 1

    source_policy = AuthorizedSourcePolicy()
    multimodal_extractor = MultimodalTextExtractor()
    entropy_filter = AdaptiveEntropyFilter()
    classifier = FineGrainedIntentClassifier()
    entity_extractor = AdvancedEntityExtractor()

    accepted_rows, compliance_decisions = source_policy.filter_records(selected_rows)
    materialized_rows = [multimodal_extractor.materialize(row) for row in accepted_rows]
    kept_rows: list[dict[str, Any]] = []
    entropy_decisions: list[dict[str, Any]] = []
    for row in materialized_rows:
        decision = entropy_filter.evaluate(row)
        dumped = decision.model_dump()
        entropy_decisions.append(dumped)
        if decision.action == "KEEP":
            kept_rows.append(row)

    classifications = [classifier.classify(row).model_dump() for row in kept_rows]
    entities = [entity.model_dump() for row in kept_rows for entity in entity_extractor.extract(row)]

    category_counts = Counter(str(item.get("risk_category") or "unknown") for item in classifications)
    secondary_counts = Counter(str(item.get("secondary_label") or "未细分") for item in classifications)
    conflict_counts = Counter(str(item.get("conflict_status") or "UNKNOWN") for item in classifications)
    review_counts = Counter("review_required" if bool(item.get("review_required")) else "auto_pass" for item in classifications)
    category_source_counts = Counter(
        (str(item.get("risk_category") or "unknown"), trace_to_source.get(str(item.get("source_trace_id") or ""), "unknown"))
        for item in classifications
    )
    entity_type_counts = Counter(str(item.get("entity_type") or "unknown") for item in entities)
    entity_value_counts = Counter(f"{item.get('entity_type')}::{item.get('normalized_value') or item.get('entity_value')}" for item in entities)
    entropy_reason_counts = Counter(str(item.get("reason") or "unknown") for item in entropy_decisions)

    _write_jsonl(classifications_out, classifications)
    _write_jsonl(entities_out, entities)

    summary = {
        "status": "completed",
        "mode": "classification_extraction_phase",
        "db_path": str(db_path),
        "input_source": resolved_source,
        "raw_snapshot_record_count": raw_snapshot_record_count,
        "cleaned_snapshot_record_count": cleaned_snapshot_record_count,
        "source_total_count": source_total_count,
        "raw_record_count": raw_snapshot_record_count,
        "derived_input_total_count": source_total_count,
        "phase_input_count": len(selected_rows),
        "snapshot_alignment": {
            "raw_record_count": raw_snapshot_record_count,
            "cleaned_record_count": cleaned_snapshot_record_count,
            "derived_input_total_count": source_total_count,
            "phase_input_count": len(selected_rows),
            "classification_count": len(classifications),
            "entity_count": len(entities),
            "claim_boundary": (
                "raw_record_count is the collection snapshot size; phase_input_count is the filtered "
                "classification/extraction view after cleaned/high-risk/quality/labeled gates."
            ),
        },
        "only_labeled": bool(args.only_labeled),
        "high_risk_only": bool(args.high_risk_only),
        "min_quality_score": float(args.min_quality_score or 0.0),
        "input_theme_counts": [{"theme": theme, "count": count} for theme, count in input_theme_counts.most_common()],
        "accepted_count": len(accepted_rows),
        "source_rejected_count": len(selected_rows) - len(accepted_rows),
        "entropy_kept_count": len(kept_rows),
        "entropy_dropped_count": len(materialized_rows) - len(kept_rows),
        "classification_count": len(classifications),
        "entity_count": len(entities),
        "review_required_count": review_counts.get("review_required", 0),
        "category_counts": [{"risk_category": name, "count": count} for name, count in category_counts.most_common()],
        "secondary_label_counts": [{"secondary_label": name, "count": count} for name, count in secondary_counts.most_common(20)],
        "conflict_status_counts": [{"conflict_status": name, "count": count} for name, count in conflict_counts.most_common()],
        "entropy_reason_counts": [{"reason": name, "count": count} for name, count in entropy_reason_counts.most_common()],
        "entity_type_counts": [{"entity_type": name, "count": count} for name, count in entity_type_counts.most_common()],
        "top_entities": [{"entity": name, "count": count} for name, count in entity_value_counts.most_common(30)],
        "top_category_sources": [
            {"risk_category": category, "source_name": source_name, "count": count}
            for (category, source_name), count in category_source_counts.most_common(30)
        ],
        "compliance_decisions": [item.model_dump() for item in compliance_decisions[:20]],
        "entropy_decisions_sample": entropy_decisions[:20],
        "classifications_jsonl": str(classifications_out),
        "entities_jsonl": str(entities_out),
    }

    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
