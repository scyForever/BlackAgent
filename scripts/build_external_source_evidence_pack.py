"""Build a small balanced external/public source-evidence pack.

This pack is intentionally separate from the large historical delivery export:
it audits whether existing public-compliant collection artifacts can provide a
small balanced set across IM/group, public-account/article, social/forum, and
vertical/technical sources with reviewable provenance.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.collector.source_metadata import source_class_for_record, source_quota_groups_for_record
from src.enhancement.text_intelligence import AdvancedEntityExtractor


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


REQUIRED_GROUPS = (
    "im_or_group",
    "public_account_or_article",
    "social_or_forum",
    "vertical_or_technical",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a balanced external/public source evidence pack.")
    parser.add_argument(
        "--input-jsonl",
        action="append",
        default=[],
        help="Input public-compliant raw JSONL. Repeatable.",
    )
    parser.add_argument("--per-group", type=int, default=20, help="Target rows per required source group.")
    parser.add_argument("--output", default="data/external_balanced_source_evidence_pack.jsonl")
    parser.add_argument("--report", default="data/external_balanced_source_evidence_pack_report.json")
    parser.add_argument("--snapshot-dir", default="data/external_source_evidence_snapshots")
    return parser.parse_args(argv)


def build_pack(rows: Iterable[Mapping[str, Any]], *, per_group: int = 20) -> dict[str, Any]:
    target = max(1, int(per_group))
    candidates: dict[str, list[dict[str, Any]]] = {group: [] for group in REQUIRED_GROUPS}
    buckets: dict[str, list[dict[str, Any]]] = {group: [] for group in REQUIRED_GROUPS}
    available_counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for row in rows:
        data = dict(row)
        group = source_evidence_group(data)
        if group not in buckets:
            skipped["unsupported_group"] += 1
            continue
        available_counts[group] += 1
        candidates[group].append(data)

    extractor = AdvancedEntityExtractor()
    for group in REQUIRED_GROUPS:
        for data in _round_robin_by_source(candidates[group]):
            if len(buckets[group]) >= target:
                break
            evidence = materialize_evidence_row(data, group=group, extractor=extractor)
            if not evidence:
                skipped[f"{group}_missing_required_fields"] += 1
                continue
            buckets[group].append(evidence)

    selected: list[dict[str, Any]] = []
    for group in REQUIRED_GROUPS:
        selected.extend(buckets[group][:target])

    selected_counts = Counter(row["source_evidence_group"] for row in selected)
    warnings: list[str] = []
    for group in REQUIRED_GROUPS:
        if len(buckets[group]) < target:
            warnings.append(f"{group}_insufficient:available={len(buckets[group])};required={target}")
    missing_required_fields = sum(1 for row in selected if not _has_required_evidence_fields(row))
    if missing_required_fields:
        warnings.append(f"selected_rows_missing_required_fields:{missing_required_fields}")

    report = {
        "status": "completed" if not warnings else "insufficient_records",
        "pack_version": "external_source_evidence_pack_v1",
        "target_groups": list(REQUIRED_GROUPS),
        "per_group_target": target,
        "selected_count": len(selected),
        "available_group_counts": {group: available_counts.get(group, 0) for group in REQUIRED_GROUPS},
        "eligible_group_counts": {group: len(buckets[group]) for group in REQUIRED_GROUPS},
        "selected_group_counts": {group: selected_counts.get(group, 0) for group in REQUIRED_GROUPS},
        "source_counts": [
            {"source_name": source_name, "count": count}
            for source_name, count in Counter(row["source_name"] for row in selected).most_common(20)
        ],
        "missing_required_fields": missing_required_fields,
        "skipped_counts": dict(sorted(skipped.items())),
        "warnings": warnings,
        "claim_boundary": (
            "small_balanced_external_public_source_evidence_pack"
            if not warnings
            else "insufficient_external_public_source_evidence_pack_not_balanced"
        ),
    }
    return {"rows": selected, "report": report}


def source_evidence_group(row: Mapping[str, Any]) -> str | None:
    groups = set(source_quota_groups_for_record(row))
    if groups & {"public_account_or_article", "public_account_article"}:
        return "public_account_or_article"
    source_class = source_class_for_record(row)
    if source_class == "im_or_group":
        return "im_or_group"
    source_text = " ".join(
        str(row.get(field) or "").lower()
        for field in ("source_name", "source_type", "platform", "source_url", "url")
    )
    source_name = str(row.get("source_name") or "").lower()
    source_type = str(row.get("source_type") or row.get("type") or "").lower()
    platform = str(row.get("platform") or "").lower()
    source_host = (urlparse(str(row.get("source_url") or row.get("url") or "")).hostname or "").lower()
    if (
        "public_article" in source_name
        or source_type in {"article", "public_account", "html_article", "rss"}
        or platform in {"wechat_public", "public_account", "html_article", "article"}
        or source_host == "mp.weixin.qq.com"
    ):
        return "public_account_or_article"
    if source_class == "vertical_or_technical":
        return "vertical_or_technical"
    if any(marker in source_text for marker in ("technical", "tech", "stackoverflow", "github", "v2ex", "reddit", "vertical")):
        return "vertical_or_technical"
    if source_class == "social_or_forum":
        return "social_or_forum"
    if any(marker in source_text for marker in ("telegram", "t.me", " tg", "tg_")):
        return "im_or_group"
    if any(marker in source_text for marker in ("forum", "tieba", "social", "x.com", "twitter", "douyin", "xiaohongshu")):
        return "social_or_forum"
    return None


def materialize_evidence_row(
    row: Mapping[str, Any],
    *,
    group: str,
    extractor: AdvancedEntityExtractor | None = None,
) -> dict[str, Any] | None:
    if not _is_external_public_row(row):
        return None
    source_url = str(row.get("source_url") or row.get("url") or "").strip()
    crawl_time = str(row.get("crawl_time") or row.get("publish_time") or row.get("last_seen_at") or "").strip()
    raw_payload_uri = str(row.get("raw_payload_uri") or "").strip()
    text = str(row.get("content_text") or row.get("raw_text") or row.get("text") or "").strip()
    if not (source_url and crawl_time and raw_payload_uri and text):
        return None
    trace_id = _trace_id(row)
    original_capture_snapshot_uri = str(row.get("capture_snapshot_uri") or "").strip()
    capture_snapshot_uri = f"local_snapshot://external_source_evidence/{_safe_filename(trace_id)}.json"
    entity_snippets = _entity_source_snippets({**dict(row), "trace_id": trace_id, "content_text": text}, text, extractor=extractor)
    if not entity_snippets:
        return None
    return {
        "trace_id": trace_id,
        "source_trace_id": trace_id,
        "source_evidence_group": group,
        "source_name": str(row.get("source_name") or "unknown_source"),
        "source_type": str(row.get("source_type") or row.get("type") or ""),
        "platform": str(row.get("platform") or ""),
        "source_url": source_url,
        "crawl_time": crawl_time,
        "raw_payload_uri": raw_payload_uri,
        "capture_snapshot_uri": capture_snapshot_uri,
        "original_capture_snapshot_uri": original_capture_snapshot_uri,
        "cleaning_reason": _cleaning_reason(row),
        "raw_snippet": text[:500],
        "entity_source_snippets": entity_snippets,
        "legal_basis": str(row.get("legal_basis") or ""),
        "source_access_type": str(row.get("source_access_type") or ""),
        "content_hash": row.get("content_hash") or row.get("hash_id"),
        "claim_boundary": "public_compliant_external_row_with_local_snapshot_reference",
        "_snapshot_payload": {
            "trace_id": trace_id,
            "source_name": row.get("source_name"),
            "source_url": source_url,
            "crawl_time": crawl_time,
            "raw_payload_uri": raw_payload_uri,
            "original_capture_snapshot_uri": original_capture_snapshot_uri,
            "content_text": text,
        },
    }


def load_input_rows(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        target = _project_path(path)
        if not target.exists():
            continue
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_pack(rows: list[dict[str, Any]], output_path: str | Path, *, snapshot_dir: str | Path) -> Path:
    output = _project_path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshots = _project_path(snapshot_dir)
    snapshots.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            snapshot_payload = row.pop("_snapshot_payload", None)
            if snapshot_payload is not None and str(row.get("capture_snapshot_uri") or "").startswith("local_snapshot://"):
                snapshot_path = snapshots / f"{_safe_filename(str(row.get('trace_id') or 'unknown'))}.json"
                snapshot_path.write_text(json.dumps(snapshot_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                row["capture_snapshot_uri"] = str(snapshot_path)
            file_obj.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return output


def write_report(report: Mapping[str, Any], report_path: str | Path, *, output_path: Path) -> Path:
    target = _project_path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**dict(report), "output": str(output_path)}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _round_robin_by_source(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source_name = str(row.get("source_name") or "unknown_source")
        grouped.setdefault(source_name, []).append(row)
    ordered_sources = list(grouped)
    ordered: list[dict[str, Any]] = []
    while True:
        added = False
        for source_name in ordered_sources:
            bucket = grouped[source_name]
            if not bucket:
                continue
            ordered.append(bucket.pop(0))
            added = True
        if not added:
            return ordered


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_paths = args.input_jsonl or [
        "data/collection_phase_raw_dataset.jsonl",
        "data/acceptance_direct_final3_raw_dataset.jsonl",
    ]
    pack = build_pack(load_input_rows(input_paths), per_group=args.per_group)
    output = write_pack(pack["rows"], args.output, snapshot_dir=args.snapshot_dir)
    report = {
        **pack["report"],
        "input_jsonl": [str(_project_path(path)) for path in input_paths],
        "snapshot_dir": str(_project_path(args.snapshot_dir)),
    }
    write_report(report, args.report, output_path=output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _is_external_public_row(row: Mapping[str, Any]) -> bool:
    url = str(row.get("source_url") or row.get("url") or "").strip()
    host = (urlparse(url).hostname or "").lower()
    if not url.startswith(("http://", "https://")):
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return False
    if "loopback" in str(row.get("source_name") or "").lower():
        return False
    legal_basis = str(row.get("legal_basis") or "").upper()
    return legal_basis in {"", "PUBLIC_COMPLIANT_DATA", "AUTHORIZED_PARTNER", "INTERNAL_AUTHORIZED_SOURCE"}


def _entity_source_snippets(
    record: Mapping[str, Any],
    text: str,
    *,
    extractor: AdvancedEntityExtractor | None = None,
) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    extractor = extractor or AdvancedEntityExtractor()
    for entity in extractor.extract(record)[:5]:
        data = entity.model_dump() if hasattr(entity, "model_dump") else dict(entity)
        start = _safe_int(data.get("start_offset"))
        end = _safe_int(data.get("end_offset"))
        if start is None or end is None or start < 0 or end <= start or start >= len(text):
            snippet = text[:240]
        else:
            snippet = text[max(0, start - 40) : min(len(text), end + 40)]
        snippets.append(
            {
                "entity_type": data.get("entity_type"),
                "raw_value": data.get("entity_value"),
                "normalized_value": data.get("normalized_value"),
                "source_snippet": snippet,
            }
        )
    return snippets


def _cleaning_reason(row: Mapping[str, Any]) -> str:
    if row.get("cleaning_reason"):
        return str(row["cleaning_reason"])
    quality = row.get("collection_quality") if isinstance(row.get("collection_quality"), Mapping) else {}
    if quality.get("noise_reason"):
        return f"retained_for_evidence_with_collection_noise_reason:{quality['noise_reason']}"
    return "retained_for_external_source_evidence_pack"


def _has_required_evidence_fields(row: Mapping[str, Any]) -> bool:
    return all(
        row.get(key)
        for key in ("source_url", "crawl_time", "raw_payload_uri", "capture_snapshot_uri", "cleaning_reason", "entity_source_snippets")
    )


def _trace_id(row: Mapping[str, Any]) -> str:
    return str(row.get("trace_id") or row.get("source_trace_id") or row.get("hash_id") or row.get("content_hash") or "unknown")


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned[:120] or "unknown"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
