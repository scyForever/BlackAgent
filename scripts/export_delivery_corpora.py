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
from src.collector.source_metadata import build_collection_metadata, source_class_for_record, source_quota_groups_for_record
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
    parser.add_argument(
        "--defense-quota-jsonl-out",
        default="data/collection_phase_defense_quota_balanced_sample.jsonl",
        help="Strict defense quota-balanced raw JSONL sample output path",
    )
    parser.add_argument(
        "--acceptance-pack-jsonl-out",
        default="data/collection_phase_multi_source_acceptance_pack.jsonl",
        help="300-500 record multi-source balanced acceptance pack JSONL output path",
    )
    parser.add_argument(
        "--acceptance-pack-classifications",
        default="",
        help="Optional classification JSONL; when paired with entities, acceptance pack only samples trace IDs present in both.",
    )
    parser.add_argument(
        "--acceptance-pack-entities",
        default="",
        help="Optional entity JSONL; when paired with classifications, acceptance pack only samples trace IDs present in both.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max raw rows to export (0 = all)")
    return parser.parse_args()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = (PROJECT_ROOT / resolved).resolve()
    if not resolved.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


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


def _source_name(row: dict[str, Any]) -> str:
    return str(row.get("source_name") or "unknown")


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
    strict_balance: bool = False,
    min_class_count: int = 20,
    max_class_share: float = 0.45,
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
            "strict_balance": strict_balance,
            "target_source_class_shares": targets,
            "max_source_share": max_source_share,
            "min_class_count": min_class_count,
            "max_class_share": max_class_share,
            "selected_count": 0,
            "selected_trace_id_sample": [],
            "class_counts": [],
            "source_counts": [],
            "warnings": ["no_rows"],
        }

    source_caps: dict[str, int] = {}
    for row in rows:
        source_name = _source_name(row)
        source_caps[source_name] = min(source_caps.get(source_name, total), max(1, int(total * max_source_share)))

    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in targets}
    buckets.setdefault("other_authorized", [])
    for row in rows:
        buckets.setdefault(source_class_for_record(row), []).append(row)

    strict_class_targets: dict[str, int] = {}
    strict_eligible_classes: list[dict[str, Any]] = []
    effective_max_class_share = max_class_share
    warnings: list[str] = []
    if strict_balance:
        available_counts = {source_class: len(available) for source_class, available in buckets.items()}
        strict_class_targets, effective_max_class_share = _strict_class_targets(
            available_counts,
            min_class_count=min_class_count,
            max_class_share=max_class_share,
        )
        strict_eligible_classes = [
            {
                "source_class": source_class,
                "available": available_counts[source_class],
                "target": requested,
            }
            for source_class, requested in strict_class_targets.items()
        ]
        for source_class, available_count in available_counts.items():
            if 0 < available_count < min_class_count:
                warnings.append(f"{source_class}_strict_min_count_unmet:available={available_count};required={min_class_count}")
        if not strict_class_targets:
            warnings.append("strict_no_classes_meet_min_count")
        if effective_max_class_share != max_class_share:
            warnings.append(f"strict_max_class_share_relaxed:{max_class_share}->{effective_max_class_share}")

    class_requests = strict_class_targets if strict_balance else {
        source_class: int(round(total * float(share)))
        for source_class, share in targets.items()
    }

    selected: list[dict[str, Any]] = []
    used_sources: Counter[str] = Counter()
    for source_class, requested in class_requests.items():
        available = buckets.get(source_class, [])
        class_selected = 0
        for row in available:
            if class_selected >= requested:
                break
            source_name = _source_name(row)
            if used_sources[source_name] >= source_caps.get(source_name, total):
                continue
            selected.append(row)
            used_sources[source_name] += 1
            class_selected += 1
        if class_selected < requested:
            available_count = len(available)
            warnings.append(f"{source_class}_quota_underfilled:{class_selected}/{requested};available={available_count}")

    if strict_balance and selected:
        selected, trim_warnings = _trim_strict_class_share(selected, max_class_share=effective_max_class_share)
        warnings.extend(trim_warnings)
        selected, source_trim_warnings = _trim_source_share(
            selected,
            max_source_share=max_source_share,
            min_source_count_for_trim=min_class_count,
        )
        warnings.extend(source_trim_warnings)
        used_sources = Counter(_source_name(row) for row in selected)

    class_counts = Counter(source_class_for_record(row) for row in selected)
    trace_ids = [
        str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id") or "")
        for row in selected
    ]
    summary = {
        "status": "completed",
        "strict_balance": strict_balance,
        "target_source_class_shares": targets,
        "max_source_share": max_source_share,
        "min_class_count": min_class_count,
        "max_class_share": max_class_share,
        "effective_max_class_share": effective_max_class_share,
        "raw_record_count": total,
        "selected_count": len(selected),
        "selected_trace_id_sample": trace_ids[:20],
        "class_counts": [{"source_class": name, "count": count} for name, count in class_counts.most_common()],
        "source_counts": [{"source_name": name, "count": count} for name, count in used_sources.most_common(20)],
        "warnings": warnings,
    }
    if strict_balance:
        summary["strict_eligible_classes"] = strict_eligible_classes
        summary["strict_class_target_counts"] = [
            {"source_class": source_class, "count": count}
            for source_class, count in strict_class_targets.items()
        ]
    if include_trace_ids:
        summary["selected_trace_ids"] = trace_ids
    return summary


ACCEPTANCE_PACK_CATEGORIES = (
    "public_account_or_article",
    "secondhand_market",
    "crowdsourcing_platform",
    "technical_or_forum",
)


def build_acceptance_pack_sample(
    rows: list[dict[str, Any]],
    *,
    min_records: int = 300,
    max_records: int = 500,
    include_trace_ids: bool = False,
    required_trace_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic 300-500 multi-source acceptance pack summary."""

    target_min = max(1, int(min_records))
    target_max = max(target_min, int(max_records))
    per_category_target = max(1, target_min // len(ACCEPTANCE_PACK_CATEGORIES))
    required_ids = {str(item).strip() for item in (required_trace_ids or set()) if str(item).strip()}
    filtered_rows = [
        row
        for row in rows
        if not required_ids or _trace_id(row) in required_ids
    ]
    buckets: dict[str, list[dict[str, Any]]] = {category: [] for category in ACCEPTANCE_PACK_CATEGORIES}
    for row in filtered_rows:
        category = _acceptance_pack_category(row)
        if category:
            buckets[category].append(row)

    available_counts = {category: len(buckets[category]) for category in ACCEPTANCE_PACK_CATEGORIES}
    eligible_count = sum(available_counts.values())
    warnings: list[str] = []
    if eligible_count < target_min:
        warnings.append(f"acceptance_pack_total_below_minimum:available={eligible_count};required={target_min}")
    for category in ACCEPTANCE_PACK_CATEGORIES:
        available_count = available_counts[category]
        if available_count < per_category_target:
            warnings.append(f"{category}_insufficient:available={available_count};required={per_category_target}")

    status = "insufficient_records" if warnings else "completed"
    selected: list[dict[str, Any]] = []
    for category in ACCEPTANCE_PACK_CATEGORIES:
        if status == "completed":
            selected.extend(buckets[category][:per_category_target])
        else:
            selected.extend(buckets[category][: min(len(buckets[category]), target_max - len(selected))])
            if len(selected) >= target_max:
                break

    selected_counts = Counter(_acceptance_pack_category(row) or "uncategorized" for row in selected)
    source_counts = Counter(_source_name(row) for row in selected)
    trace_ids = [_trace_id(row) for row in selected]
    summary = {
        "status": status,
        "pack_version": "multi_source_acceptance_pack_v1",
        "target_record_range": {"min": target_min, "max": target_max},
        "target_record_count": target_min,
        "per_category_target": per_category_target,
        "target_categories": list(ACCEPTANCE_PACK_CATEGORIES),
        "raw_record_count": len(rows),
        "evidence_ready_trace_filter": {
            "enabled": bool(required_ids),
            "required_trace_count": len(required_ids),
            "excluded_without_required_trace": len(rows) - len(filtered_rows),
        },
        "eligible_record_count": eligible_count,
        "selected_count": len(selected),
        "available_category_counts": [
            {"category": category, "count": available_counts[category]}
            for category in ACCEPTANCE_PACK_CATEGORIES
        ],
        "selected_category_counts": [
            {"category": category, "count": selected_counts.get(category, 0)}
            for category in ACCEPTANCE_PACK_CATEGORIES
        ],
        "source_counts": [{"source_name": name, "count": count} for name, count in source_counts.most_common(20)],
        "selected_trace_id_sample": trace_ids[:20],
        "warnings": warnings,
        "claim_boundary": (
            "completed_300_to_500_multi_source_acceptance_pack"
            if status == "completed"
            else "insufficient_records_exported_for_audit_not_300_record_acceptance"
        ),
    }
    if include_trace_ids:
        summary["selected_trace_ids"] = trace_ids
    return summary


def acceptance_pack_required_trace_ids(
    *,
    classifications: list[dict[str, Any]],
    entities: list[dict[str, Any]],
) -> set[str]:
    classification_ids = {_trace_id(row) for row in classifications if _trace_id(row)}
    entity_ids = {_trace_id(row) for row in entities if _trace_id(row)}
    if not classification_ids or not entity_ids:
        return set()
    return classification_ids & entity_ids


def _acceptance_pack_category(row: dict[str, Any]) -> str | None:
    groups = set(source_quota_groups_for_record(row))
    if groups & {"public_account_or_article", "public_account_article"}:
        return "public_account_or_article"
    if "secondhand_market" in groups:
        return "secondhand_market"
    if "crowdsourcing_platform" in groups:
        return "crowdsourcing_platform"
    if "vertical_or_technical" in groups:
        return "technical_or_forum"

    source_text = " ".join(
        str(row.get(field) or "").strip().lower()
        for field in ("source_type", "type", "platform", "source_name", "name", "source_url", "url")
    )
    if any(marker in source_text for marker in ("forum", "tieba", "technical", "techforum", "threat_intel")):
        return "technical_or_forum"
    return None


def _strict_class_targets(
    available_counts: dict[str, int],
    *,
    min_class_count: int,
    max_class_share: float,
) -> tuple[dict[str, int], float]:
    eligible = {
        source_class: count
        for source_class, count in available_counts.items()
        if count >= min_class_count
    }
    if not eligible:
        return {}, max_class_share

    class_count = len(eligible)
    effective_max_class_share = max(float(max_class_share), 1.0 / class_count)
    max_total = sum(eligible.values())
    best_total = 0
    for candidate_total in range(1, max_total + 1):
        if _strict_target_feasible(
            candidate_total,
            eligible,
            min_class_count=min_class_count,
            max_class_share=effective_max_class_share,
        ):
            best_total = candidate_total

    if best_total <= 0:
        return {}, effective_max_class_share

    per_class_cap = max(1, int(best_total * effective_max_class_share))
    class_targets = {
        source_class: min(count, per_class_cap)
        for source_class, count in eligible.items()
    }
    excess = sum(class_targets.values()) - best_total
    while excess > 0:
        reducible = [
            source_class
            for source_class, target in class_targets.items()
            if target > min_class_count
        ]
        if not reducible:
            break
        source_class = max(reducible, key=lambda key: (class_targets[key], key))
        class_targets[source_class] -= 1
        excess -= 1

    return {
        source_class: class_targets[source_class]
        for source_class in available_counts
        if source_class in class_targets
    }, effective_max_class_share


def _strict_target_feasible(
    target_total: int,
    available_counts: dict[str, int],
    *,
    min_class_count: int,
    max_class_share: float,
) -> bool:
    if target_total < len(available_counts) * min_class_count:
        return False
    per_class_cap = max(1, int(target_total * max_class_share))
    if any(min(count, per_class_cap) < min_class_count for count in available_counts.values()):
        return False
    return sum(min(count, per_class_cap) for count in available_counts.values()) >= target_total


def _trim_strict_class_share(
    selected: list[dict[str, Any]],
    *,
    max_class_share: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    kept = list(selected)
    warnings: list[str] = []
    while kept:
        class_counts = Counter(source_class_for_record(row) for row in kept)
        selected_count = len(kept)
        overfilled = [
            (source_class, count)
            for source_class, count in class_counts.items()
            if count / selected_count > max_class_share
        ]
        if not overfilled:
            return kept, warnings

        source_class, count = max(overfilled, key=lambda item: (item[1], item[0]))
        remove_at = next(
            idx
            for idx in range(len(kept) - 1, -1, -1)
            if source_class_for_record(kept[idx]) == source_class
        )
        kept.pop(remove_at)
        warnings.append(f"{source_class}_strict_class_share_trimmed:{count}/{selected_count}")

    return kept, warnings


def _trim_source_share(
    selected: list[dict[str, Any]],
    *,
    max_source_share: float,
    min_source_count_for_trim: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    kept = list(selected)
    warnings: list[str] = []
    if max_source_share <= 0:
        return kept, warnings
    initial_count = len(kept)
    initial_source_counts = Counter(_source_name(row) for row in kept)
    initial_overfilled = [
        (source_name, count)
        for source_name, count in initial_source_counts.items()
        if initial_count and count / initial_count > max_source_share and count > max(0, min_source_count_for_trim)
    ]
    if not initial_overfilled:
        overfilled = [
            (source_name, count)
            for source_name, count in initial_source_counts.items()
            if initial_count and count / initial_count > max_source_share
        ]
        for source_name, count in overfilled:
            warnings.append(f"{source_name}_source_share_cap_infeasible:{count}/{initial_count}")
        return kept, warnings
    max_initial_source_count = max(count for _source_name_value, count in initial_overfilled)
    capped_sources = {
        source_name
        for source_name, count in initial_overfilled
        if count == max_initial_source_count
    }
    if not capped_sources:
        return kept, warnings
    trimmed_original_counts = {source_name: initial_source_counts[source_name] for source_name in capped_sources}
    while kept:
        source_counts = Counter(_source_name(row) for row in kept)
        selected_count = len(kept)
        overfilled = [
            (source_name, count)
            for source_name, count in source_counts.items()
            if source_name in capped_sources
            and count > max(0, min_source_count_for_trim)
            and count / selected_count > max_source_share
        ]
        if not overfilled:
            uncapped_overfilled = [
                (source_name, count)
                for source_name, count in source_counts.items()
                if source_name not in capped_sources and count / selected_count > max_source_share
            ]
            for source_name in sorted(capped_sources):
                original_count = trimmed_original_counts[source_name]
                current_count = source_counts.get(source_name, 0)
                if current_count < original_count:
                    warnings.append(f"{source_name}_strict_source_share_trimmed:{original_count}->{current_count}/{selected_count}")
            for source_name, count in uncapped_overfilled:
                warnings.append(f"{source_name}_source_share_cap_infeasible:{count}/{selected_count}")
            return kept, warnings

        source_name, count = max(overfilled, key=lambda item: (item[1], item[0]))
        remove_at = next(
            idx
            for idx in range(len(kept) - 1, -1, -1)
            if _source_name(kept[idx]) == source_name
        )
        kept.pop(remove_at)

    return kept, warnings


def main() -> int:
    args = parse_args()
    db_path = (PROJECT_ROOT / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db).resolve()
    raw_out = (PROJECT_ROOT / args.raw_jsonl_out).resolve() if not Path(args.raw_jsonl_out).is_absolute() else Path(args.raw_jsonl_out).resolve()
    manifest_out = (PROJECT_ROOT / args.manifest_out).resolve() if not Path(args.manifest_out).is_absolute() else Path(args.manifest_out).resolve()
    quota_out = (PROJECT_ROOT / args.quota_jsonl_out).resolve() if not Path(args.quota_jsonl_out).is_absolute() else Path(args.quota_jsonl_out).resolve()
    defense_quota_out = (
        (PROJECT_ROOT / args.defense_quota_jsonl_out).resolve()
        if not Path(args.defense_quota_jsonl_out).is_absolute()
        else Path(args.defense_quota_jsonl_out).resolve()
    )
    acceptance_pack_out = (
        (PROJECT_ROOT / args.acceptance_pack_jsonl_out).resolve()
        if not Path(args.acceptance_pack_jsonl_out).is_absolute()
        else Path(args.acceptance_pack_jsonl_out).resolve()
    )
    acceptance_classifications = _load_jsonl(args.acceptance_pack_classifications) if args.acceptance_pack_classifications else []
    acceptance_entities = _load_jsonl(args.acceptance_pack_entities) if args.acceptance_pack_entities else []
    acceptance_required_trace_ids = acceptance_pack_required_trace_ids(
        classifications=acceptance_classifications,
        entities=acceptance_entities,
    )

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

    defense_quota_sample = build_quota_sample(exported_rows, strict_balance=True, include_trace_ids=True)
    defense_selected_ids = set(defense_quota_sample.pop("selected_trace_ids", []) or [])
    defense_quota_rows = [
        row
        for row in exported_rows
        if str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id") or "") in defense_selected_ids
    ]
    _write_jsonl(defense_quota_out, defense_quota_rows)

    acceptance_pack = build_acceptance_pack_sample(
        exported_rows,
        include_trace_ids=True,
        required_trace_ids=acceptance_required_trace_ids or None,
    )
    acceptance_pack["evidence_ready_inputs"] = {
        "classifications_jsonl": str((PROJECT_ROOT / args.acceptance_pack_classifications).resolve())
        if args.acceptance_pack_classifications and not Path(args.acceptance_pack_classifications).is_absolute()
        else str(Path(args.acceptance_pack_classifications).resolve()) if args.acceptance_pack_classifications else "",
        "entities_jsonl": str((PROJECT_ROOT / args.acceptance_pack_entities).resolve())
        if args.acceptance_pack_entities and not Path(args.acceptance_pack_entities).is_absolute()
        else str(Path(args.acceptance_pack_entities).resolve()) if args.acceptance_pack_entities else "",
        "classification_trace_count": len({_trace_id(row) for row in acceptance_classifications if _trace_id(row)}),
        "entity_trace_count": len({_trace_id(row) for row in acceptance_entities if _trace_id(row)}),
        "required_trace_count": len(acceptance_required_trace_ids),
        "claim_boundary": (
            "acceptance pack sampled only from traces with both classification and entity evidence"
            if acceptance_required_trace_ids
            else "acceptance pack did not receive both classification and entity evidence inputs"
        ),
    }
    acceptance_selected_ids = set(acceptance_pack.pop("selected_trace_ids", []) or [])
    acceptance_pack_rows = [
        row
        for row in exported_rows
        if str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id") or "") in acceptance_selected_ids
    ]
    _write_jsonl(acceptance_pack_out, acceptance_pack_rows)

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
        "defense_quota_balanced_jsonl": str(defense_quota_out),
        "acceptance_pack_jsonl": str(acceptance_pack_out),
        "query_term_stage_counts": [{"stage": name, "count": count} for name, count in stage_counts.most_common()],
        "special_signal_counts": [{"signal": name, "count": count} for name, count in signal_counts.most_common()],
        "source_class_counts": [{"source_class": name, "count": count} for name, count in source_class_counts.most_common()],
        "source_access_type_counts": [{"source_access_type": name, "count": count} for name, count in source_access_counts.most_common()],
        "top_sources": [{"source_name": name, "count": count} for name, count in source_counts.most_common(20)],
        "quota_balanced_sample": quota_sample,
        "defense_quota_balanced_sample": defense_quota_sample,
        "multi_source_acceptance_pack": acceptance_pack,
        "sample_special_signal_rows": samples,
    }
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
