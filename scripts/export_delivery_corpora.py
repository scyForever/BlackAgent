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
from src.collector.source_metadata import build_collection_metadata, source_class_for_record
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
    parser.add_argument(
        "--quota-jsonl-out",
        default="data/collection_phase_quota_balanced_sample.jsonl",
        help="Quota-balanced raw JSONL sample output path",
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
    metadata = build_collection_metadata(annotated, content_text=original_text, now_iso=str(annotated.get("crawl_time") or ""))
    for key, value in metadata.items():
        annotated.setdefault(key, value)
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
        annotated["multimodal_reference_fields"] = materialized.get("multimodal_reference_fields", []) or []
        annotated["multimodal_reference_count"] = materialized.get("multimodal_reference_count", 0) or 0
        annotated["content_modality"] = materialized.get("content_modality")
        if normalize_text(str(materialized.get("content_text") or "")) != normalized_text:
            annotated["multimodal_augmented_content_text"] = materialized.get("content_text") or ""
    elif materialized.get("multimodal_reference_count"):
        annotated["multimodal_reference_fields"] = materialized.get("multimodal_reference_fields", []) or []
        annotated["multimodal_reference_count"] = materialized.get("multimodal_reference_count", 0) or 0
        annotated["content_modality"] = materialized.get("content_modality")

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


def build_quota_sample(
    rows: list[dict[str, Any]],
    *,
    source_class_targets: dict[str, float] | None = None,
    max_source_share: float = 0.30,
    include_trace_ids: bool = False,
) -> dict[str, Any]:
    """Build a deterministic quota-balanced sample manifest from raw rows.

    This does not mutate the source DB; it records an auditable subset that can
    be fed into later stages when the defense needs balanced IM/forum/vertical
    evidence rather than a Telegram-dominated raw snapshot.
    """

    targets = source_class_targets or {
        "im_or_group": 0.30,
        "social_or_forum": 0.30,
        "vertical_or_technical": 0.30,
        "other_authorized": 0.10,
    }
    total = len(rows)
    if total <= 0:
        return {
            "status": "empty",
            "target_source_class_shares": targets,
            "max_source_share": max_source_share,
            "selected_count": 0,
            "selected_trace_id_sample": [],
            "class_counts": [],
            "source_counts": [],
            "warnings": ["no_rows"],
        }

    source_caps: dict[str, int] = {}
    for row in rows:
        source_name = str(row.get("source_name") or "unknown")
        source_caps[source_name] = min(source_caps.get(source_name, total), max(1, int(total * max_source_share)))

    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in targets}
    buckets.setdefault("other_authorized", [])
    for row in rows:
        buckets.setdefault(source_class_for_record(row), []).append(row)

    selected: list[dict[str, Any]] = []
    used_sources: Counter[str] = Counter()
    warnings: list[str] = []
    for source_class, share in targets.items():
        available = buckets.get(source_class, [])
        requested = int(round(total * float(share)))
        class_selected = 0
        for row in available:
            if class_selected >= requested:
                break
            source_name = str(row.get("source_name") or "unknown")
            if used_sources[source_name] >= source_caps.get(source_name, total):
                continue
            selected.append(row)
            used_sources[source_name] += 1
            class_selected += 1
        if class_selected < requested:
            available_count = len(available)
            warnings.append(f"{source_class}_quota_underfilled:{class_selected}/{requested};available={available_count}")

    class_counts = Counter(source_class_for_record(row) for row in selected)
    trace_ids = [
        str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id") or "")
        for row in selected
    ]
    summary = {
        "status": "completed",
        "target_source_class_shares": targets,
        "max_source_share": max_source_share,
        "raw_record_count": total,
        "selected_count": len(selected),
        "selected_trace_id_sample": trace_ids[:20],
        "class_counts": [{"source_class": name, "count": count} for name, count in class_counts.most_common()],
        "source_counts": [{"source_name": name, "count": count} for name, count in used_sources.most_common(20)],
        "warnings": warnings,
    }
    if include_trace_ids:
        summary["selected_trace_ids"] = trace_ids
    return summary


def main() -> int:
    args = parse_args()
    db_path = (PROJECT_ROOT / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db).resolve()
    raw_out = (PROJECT_ROOT / args.raw_jsonl_out).resolve() if not Path(args.raw_jsonl_out).is_absolute() else Path(args.raw_jsonl_out).resolve()
    manifest_out = (PROJECT_ROOT / args.manifest_out).resolve() if not Path(args.manifest_out).is_absolute() else Path(args.manifest_out).resolve()
    quota_out = (PROJECT_ROOT / args.quota_jsonl_out).resolve() if not Path(args.quota_jsonl_out).is_absolute() else Path(args.quota_jsonl_out).resolve()

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
    source_class_counts: Counter[str] = Counter()
    source_access_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    for row in exported_rows:
        source_counts[str(row.get("source_name") or "unknown")] += 1
        source_class_counts[source_class_for_record(row)] += 1
        source_access_counts[str(row.get("source_access_type") or "unknown")] += 1
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

    quota_sample = build_quota_sample(exported_rows, include_trace_ids=True)
    selected_ids = set(quota_sample.pop("selected_trace_ids", []) or [])
    quota_rows = [
        row
        for row in exported_rows
        if str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id") or "") in selected_ids
    ]
    _write_jsonl(quota_out, quota_rows)

    manifest = {
        "status": "completed",
        "db_path": str(db_path),
        "raw_record_count": len(exported_rows),
        "cleaned_record_count": len(cleaned_rows),
        "snapshot_alignment": {
            "raw_record_count": len(exported_rows),
            "cleaned_record_count": len(cleaned_rows),
            "claim_boundary": (
                "raw_record_count is the authoritative collection snapshot; "
                "cleaned/classification summaries may be filtered derived views and must report their source snapshot separately."
            ),
        },
        "raw_jsonl": str(raw_out),
        "quota_balanced_jsonl": str(quota_out),
        "query_term_stage_counts": [{"stage": name, "count": count} for name, count in stage_counts.most_common()],
        "special_signal_counts": [{"signal": name, "count": count} for name, count in signal_counts.most_common()],
        "source_class_counts": [{"source_class": name, "count": count} for name, count in source_class_counts.most_common()],
        "source_access_type_counts": [{"source_access_type": name, "count": count} for name, count in source_access_counts.most_common()],
        "top_sources": [{"source_name": name, "count": count} for name, count in source_counts.most_common(20)],
        "quota_balanced_sample": quota_sample,
        "sample_special_signal_rows": samples,
    }
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
