"""Run the intelligent cleaning stage over collection-phase raw records."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cleaner.pipeline import CleanerPipeline
from src.enhancement.source_intake import MultimodalTextExtractor
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


BASE_TEXT_SOURCES = {"content_text", "text", "raw_text"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the intelligent cleaning phase over collection-phase raw records.")
    parser.add_argument("--db", default="data/collection_phase_delivery.db", help="SQLite DB path to read raw_records from")
    parser.add_argument(
        "--summary-out",
        default="data/cleaning_phase_summary.json",
        help="Summary JSON output path",
    )
    parser.add_argument(
        "--cleaned-jsonl",
        default="data/cleaning_phase_cleaned_corpus.jsonl",
        help="Cleaned high-quality corpus JSONL output path",
    )
    parser.add_argument(
        "--high-risk-jsonl",
        default="data/cleaning_phase_high_risk_corpus.jsonl",
        help="High-risk cleaned corpus JSONL output path",
    )
    parser.add_argument(
        "--persist-cleaned",
        action="store_true",
        help="Persist cleaned corpus rows into the cleaned_texts SQL table",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max raw rows to process (0 = all)")
    return parser.parse_args()


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json") if callable(getattr(value, "model_dump")) else value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")


def _trace_id(row: Mapping[str, Any] | Any) -> str:
    if isinstance(row, Mapping):
        data = row
    else:
        data = _dump(row)
    return str(data.get("source_trace_id") or data.get("trace_id") or data.get("hash_id") or "")


def _materialize_rows(rows: list[dict[str, Any]] | list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    extractor = MultimodalTextExtractor()
    materialized_rows: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    materialized_count = 0

    for row in rows:
        raw_row = _dump(row)
        materialized = extractor.materialize(raw_row)
        extra_sources = [
            str(source_name)
            for source_name in (materialized.get("multimodal_text_sources", []) or [])
            if str(source_name) not in BASE_TEXT_SOURCES
        ]
        if extra_sources:
            materialized_count += 1
            materialized["multimodal_text_sources"] = extra_sources
            materialized["multimodal_signal_count"] = len(extra_sources)
            for source_name in extra_sources:
                source_counts[source_name] += 1
            materialized_rows.append(materialized)
            continue
        materialized_rows.append(raw_row)

    summary = {
        "multimodal_materialized_count": materialized_count,
        "multimodal_source_counts": [
            {"source": source_name, "count": count}
            for source_name, count in source_counts.most_common()
        ],
    }
    return materialized_rows, summary


def _copy_cleaning_context(cleaned_row: dict[str, Any], source_row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not source_row:
        return cleaned_row

    for field_name in (
        "source_name",
        "source_type",
        "source_url",
        "legal_basis",
        "matched_keywords",
        "matched_themes",
        "excluded_keywords",
        "excluded_themes",
        "search_query",
        "search_query_url",
        "query_theme",
        "query_term",
        "query_term_stage",
        "query_variant_index",
        "multimodal_text_extracted",
        "multimodal_text_sources",
        "multimodal_signal_count",
        "multimodal_reference_fields",
        "multimodal_reference_count",
        "content_modality",
    ):
        if field_name in source_row:
            cleaned_row[field_name] = source_row.get(field_name)
    return cleaned_row


def run_cleaning_phase(
    rows: list[dict[str, Any]] | list[Any],
    *,
    persist_backend: Any | None = None,
    limit: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    selected_rows = list(rows[:limit] if limit and limit > 0 else rows)
    materialized_rows, multimodal_summary = _materialize_rows(selected_rows)
    row_by_trace = {_trace_id(row): row for row in materialized_rows if _trace_id(row)}
    cleaner = CleanerPipeline()
    batch = cleaner.clean(materialized_rows)

    cleaned_rows = [
        _copy_cleaning_context(_dump(item), row_by_trace.get(str(_dump(item).get("source_trace_id") or "")))
        for item in batch.cleaned
    ]
    dropped_rows = [_dump(item) for item in batch.dropped]
    high_risk_rows = [
        row
        for row in cleaned_rows
        if str(row.get("risk_level") or "NONE").upper() in {"HIGH", "CRITICAL"}
    ]

    if persist_backend is not None and hasattr(persist_backend, "save_cleaned"):
        if hasattr(persist_backend, "clear_cleaned"):
            persist_backend.clear_cleaned()
        for row in cleaned_rows:
            persist_backend.save_cleaned(row, commit=False)
        if hasattr(persist_backend, "_commit"):
            persist_backend._commit()

    drop_reason_counts = Counter(str(row.get("reason") or "unknown") for row in dropped_rows)
    risk_level_counts = Counter(str(row.get("risk_level") or "NONE") for row in cleaned_rows)
    risk_category_counts = Counter(
        category
        for row in cleaned_rows
        for category in row.get("risk_categories", [])
        if str(category).strip()
    )
    risk_marker_counts = Counter(
        marker
        for row in cleaned_rows
        for marker in row.get("risk_markers", [])
        if str(marker).strip()
    )

    quality_total = sum(float(row.get("quality_score", 0.0) or 0.0) for row in cleaned_rows)
    risk_total = sum(float(row.get("risk_score", 0.0) or 0.0) for row in cleaned_rows)

    summary = {
        "status": "completed",
        "mode": "cleaning_phase",
        "input_count": len(selected_rows),
        "cleaned_count": len(cleaned_rows),
        "dropped_count": len(dropped_rows),
        "high_risk_count": len(high_risk_rows),
        "duplicate_drop_count": drop_reason_counts.get("duplicate", 0),
        "dedup_group_count": len(batch.dedup_groups),
        "average_quality_score": round(quality_total / len(cleaned_rows), 4) if cleaned_rows else 0.0,
        "average_risk_score": round(risk_total / len(cleaned_rows), 4) if cleaned_rows else 0.0,
        "drop_reason_counts": [{"reason": name, "count": count} for name, count in drop_reason_counts.most_common()],
        "risk_level_counts": [{"risk_level": name, "count": count} for name, count in risk_level_counts.most_common()],
        "risk_category_counts": [{"risk_category": name, "count": count} for name, count in risk_category_counts.most_common()],
        "top_risk_markers": [{"marker": name, "count": count} for name, count in risk_marker_counts.most_common(20)],
        **multimodal_summary,
    }
    return cleaned_rows, high_risk_rows, summary


def main() -> int:
    args = parse_args()
    db_path = (PROJECT_ROOT / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db).resolve()
    summary_out = (PROJECT_ROOT / args.summary_out).resolve() if not Path(args.summary_out).is_absolute() else Path(args.summary_out).resolve()
    cleaned_out = (PROJECT_ROOT / args.cleaned_jsonl).resolve() if not Path(args.cleaned_jsonl).is_absolute() else Path(args.cleaned_jsonl).resolve()
    high_risk_out = (PROJECT_ROOT / args.high_risk_jsonl).resolve() if not Path(args.high_risk_jsonl).is_absolute() else Path(args.high_risk_jsonl).resolve()

    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    rows = backend.list_raw(limit=args.limit if args.limit and args.limit > 0 else None)
    cleaned_rows, high_risk_rows, summary = run_cleaning_phase(
        rows,
        persist_backend=backend if args.persist_cleaned else None,
    )
    backend.close()

    _write_jsonl(cleaned_out, cleaned_rows)
    _write_jsonl(high_risk_out, high_risk_rows)

    summary.update(
        {
            "db_path": str(db_path),
            "persist_cleaned": bool(args.persist_cleaned),
            "cleaned_jsonl": str(cleaned_out),
            "high_risk_jsonl": str(high_risk_out),
        }
    )
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
