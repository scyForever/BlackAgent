"""Export raw-delivery JSONL plus a manifest for special black/gray signals."""

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

from src.cleaner.text_filter import BLACKGRAY_LITERAL_VARIANT_REPLACEMENTS, normalize_intel_text, normalize_text
from src.collector.relevance import get_theme_search_variants, load_theme_synonym_registry
from src.enhancement.source_intake import MultimodalTextExtractor
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


BASE_TEXT_SOURCES = {"content_text", "text", "raw_text"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export collection/cleaning delivery corpora.")
    parser.add_argument("--db", default="data/collection_phase_delivery.db", help="SQLite DB path")
    parser.add_argument(
        "--raw-jsonl-out",
        default="data/collection_phase_raw_dataset.jsonl",
        help="Raw dataset JSONL output path",
    )
    parser.add_argument(
        "--manifest-out",
        default="data/collection_phase_delivery_manifest.json",
        help="Manifest JSON output path",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max raw rows to export (0 = all)")
    return parser.parse_args()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")


def _query_stage_lookup() -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for theme in load_theme_synonym_registry():
        for item in get_theme_search_variants(theme, limit=64):
            lookup[(normalize_text(theme).lower(), normalize_text(str(item["term"])).lower())] = str(item["stage"])
    return lookup


def _infer_query_term_stage(row: dict[str, Any], lookup: dict[tuple[str, str], str]) -> tuple[str | None, bool]:
    current = normalize_text(str(row.get("query_term_stage") or ""))
    if current in {"core", "variant"}:
        return current, False

    query_term = normalize_text(str(row.get("query_term") or ""))
    if not query_term:
        return None, False

    query_theme = normalize_text(str(row.get("query_theme") or ""))
    if query_theme:
        inferred = lookup.get((query_theme.lower(), query_term.lower()))
        if inferred:
            return inferred, True
    return "core", True


def _trace_id(row: dict[str, Any]) -> str:
    return str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id") or "")


def _annotate_row(
    row: dict[str, Any],
    *,
    stage_lookup: dict[tuple[str, str], str],
    multimodal_extractor: MultimodalTextExtractor,
) -> dict[str, Any]:
    annotated = dict(row)
    original_text = str(row.get("content_text") or "")
    normalized_text = normalize_text(original_text)
    intel_normalized = normalize_intel_text(original_text)
    contains_variant_signal = bool(original_text) and intel_normalized != normalized_text
    contains_emoji_signal = any(raw in original_text for raw, _target in BLACKGRAY_LITERAL_VARIANT_REPLACEMENTS)

    query_term_stage, stage_inferred = _infer_query_term_stage(row, stage_lookup)
    if query_term_stage:
        annotated["query_term_stage"] = query_term_stage
    if stage_inferred:
        annotated["query_term_stage_inferred"] = True

    materialized = multimodal_extractor.materialize(row)
    extra_sources = [
        str(source_name)
        for source_name in (materialized.get("multimodal_text_sources", []) or [])
        if str(source_name) not in BASE_TEXT_SOURCES
    ]
    multimodal_signal_count = len(extra_sources)
    if multimodal_signal_count > 0:
        annotated["multimodal_text_extracted"] = True
        annotated["multimodal_signal_count"] = multimodal_signal_count
        annotated["multimodal_text_sources"] = extra_sources
        if normalize_text(str(materialized.get("content_text") or "")) != normalized_text:
            annotated["multimodal_augmented_content_text"] = materialized.get("content_text") or ""

    special_signal_types: list[str] = []
    if contains_variant_signal:
        special_signal_types.append("variant_or_homophone_normalized")
    if contains_emoji_signal:
        special_signal_types.append("emoji_marker")
    if multimodal_signal_count > 0:
        special_signal_types.append("multimodal_text")
    if special_signal_types:
        annotated["special_signal_types"] = special_signal_types

    return annotated


def main() -> int:
    args = parse_args()
    db_path = (PROJECT_ROOT / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db).resolve()
    raw_out = (PROJECT_ROOT / args.raw_jsonl_out).resolve() if not Path(args.raw_jsonl_out).is_absolute() else Path(args.raw_jsonl_out).resolve()
    manifest_out = (PROJECT_ROOT / args.manifest_out).resolve() if not Path(args.manifest_out).is_absolute() else Path(args.manifest_out).resolve()

    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    raw_rows = backend.list_raw(limit=args.limit if args.limit and args.limit > 0 else None)
    cleaned_rows = backend.list_cleaned()
    backend.close()

    stage_lookup = _query_stage_lookup()
    multimodal_extractor = MultimodalTextExtractor()
    exported_rows = [
        _annotate_row(row, stage_lookup=stage_lookup, multimodal_extractor=multimodal_extractor)
        for row in raw_rows
    ]

    _write_jsonl(raw_out, exported_rows)

    source_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    for row in exported_rows:
        source_counts[str(row.get("source_name") or "unknown")] += 1
        stage = str(row.get("query_term_stage") or "").strip()
        if stage:
            stage_counts[stage] += 1
        for signal_name in row.get("special_signal_types", []) or []:
            signal_counts[str(signal_name)] += 1
        if row.get("special_signal_types") and len(samples) < 8:
            samples.append(
                {
                    "trace_id": _trace_id(row),
                    "source_name": row.get("source_name"),
                    "query_term": row.get("query_term"),
                    "query_term_stage": row.get("query_term_stage"),
                    "special_signal_types": row.get("special_signal_types"),
                    "multimodal_signal_count": row.get("multimodal_signal_count", 0),
                    "content_preview": str(row.get("content_text") or "")[:240],
                }
            )

    for expected_stage in ("core", "variant"):
        stage_counts.setdefault(expected_stage, 0)
    for expected_signal in ("variant_or_homophone_normalized", "emoji_marker", "multimodal_text"):
        signal_counts.setdefault(expected_signal, 0)

    manifest = {
        "status": "completed",
        "db_path": str(db_path),
        "raw_record_count": len(exported_rows),
        "cleaned_record_count": len(cleaned_rows),
        "raw_jsonl": str(raw_out),
        "query_term_stage_counts": [{"stage": name, "count": count} for name, count in stage_counts.most_common()],
        "special_signal_counts": [{"signal": name, "count": count} for name, count in signal_counts.most_common()],
        "top_sources": [{"source_name": name, "count": count} for name, count in source_counts.most_common(20)],
        "sample_special_signal_rows": samples,
    }
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
