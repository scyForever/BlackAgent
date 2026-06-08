"""Run BlackAgent batch collection against a source catalog and persist raw data."""

from __future__ import annotations

import argparse
import json
from collections import Counter, OrderedDict, deque
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import PROJECT_ROOT as APP_PROJECT_ROOT, Settings, resolve_project_path
from src.collector.source_config import apply_source_min_quotas, load_source_catalog
from src.collector.source_metadata import source_class_for_record, source_quota_groups_for_record
from src.local_runtime import LocalAgentRuntime
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


DEFAULT_SOURCE_MIN_QUOTAS: dict[str, int] = {
    "vertical_or_technical": 1,
    "public_account_or_article": 1,
    "secondhand_market": 1,
    "crowdsourcing_platform": 1,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect raw intelligence from a batch source catalog.")
    parser.add_argument(
        "--catalog",
        default="config/intel_sources.public.yaml",
        help="Project-relative source catalog YAML path (default: config/intel_sources.public.yaml)",
    )
    parser.add_argument(
        "--db",
        default="data/blackagent_public_sources.db",
        help="SQLite output path, project-relative or absolute (default: data/blackagent_public_sources.db)",
    )
    parser.add_argument("--timeout-seconds", type=float, default=25.0, help="Network timeout per source request")
    parser.add_argument("--max-records", type=int, default=20, help="Max records fetched per source")
    parser.add_argument(
        "--rate-limit-per-minute",
        type=int,
        default=8,
        help="Per-host request budget for batch public search collection (default: 8)",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help="Retry count for retryable HTTP statuses like 429 (default: 3)",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=8.0,
        help="Initial backoff seconds before retrying a throttled source (default: 8)",
    )
    parser.add_argument(
        "--retry-backoff-multiplier",
        type=float,
        default=2.0,
        help="Exponential multiplier for retry backoff (default: 2.0)",
    )
    parser.add_argument(
        "--max-sources",
        type=int,
        default=0,
        help="Optional cap after catalog expansion/filtering (0 = all expanded sources).",
    )
    parser.add_argument(
        "--source-class",
        action="append",
        default=[],
        choices=["im_or_group", "social_or_forum", "vertical_or_technical", "other_authorized"],
        help="Filter expanded sources by source diversity class; repeatable.",
    )
    parser.add_argument(
        "--source-min-quota",
        action="append",
        default=[],
        metavar="GROUP=COUNT",
        help=(
            "Minimum quota for a granular source group before overflow selection; repeatable. "
            "Defaults cover vertical/technical, public-account/article, secondhand, and crowdsourcing sources."
        ),
    )
    parser.add_argument(
        "--disable-source-min-quotas",
        action="store_true",
        help="Disable default granular source minimum quotas when --max-sources is used.",
    )
    parser.add_argument(
        "--summary-out",
        default="",
        help="Optional JSON file for the collection run summary.",
    )
    parser.add_argument("--run-pipeline", action="store_true", help="Also run the Phase II/III pipeline after raw collection")
    parser.add_argument("--fresh", action="store_true", help="Delete the target SQLite file before collecting")
    return parser.parse_args()


def balanced_source_slice(sources: list[Mapping[str, Any]], *, max_sources: int) -> list[dict[str, Any]]:
    """Return a class/source-name balanced prefix from already-filtered sources."""

    if max_sources <= 0 or max_sources >= len(sources):
        return [dict(source) for source in sources]
    grouped: OrderedDict[tuple[str, str], deque[Mapping[str, Any]]] = OrderedDict()
    for source in sources:
        key = (
            source_class_for_record(source),
            str(source.get("source_name") or source.get("name") or "unknown_source"),
        )
        grouped.setdefault(key, deque()).append(source)

    selected: list[dict[str, Any]] = []
    while grouped and len(selected) < max_sources:
        for key in list(grouped.keys()):
            queue = grouped.get(key)
            if not queue:
                grouped.pop(key, None)
                continue
            selected.append(dict(queue.popleft()))
            if not queue:
                grouped.pop(key, None)
            if len(selected) >= max_sources:
                break
    return selected


def source_minimum_quotas_from_args(args: argparse.Namespace) -> dict[str, int]:
    quotas = {} if bool(getattr(args, "disable_source_min_quotas", False)) else dict(DEFAULT_SOURCE_MIN_QUOTAS)
    for raw_item in getattr(args, "source_min_quota", []) or []:
        item = str(raw_item or "").strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"source min quota must use GROUP=COUNT, got {item!r}")
        group, raw_count = item.split("=", 1)
        group = group.strip()
        if not group:
            raise ValueError(f"source min quota group cannot be empty in {item!r}")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"source min quota count must be an integer in {item!r}") from exc
        if count <= 0:
            quotas.pop(group, None)
        else:
            quotas[group] = count
    return dict(quotas)


def selected_sources_from_args(args: argparse.Namespace, catalog_path: Path) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Return explicitly selected expanded sources when source filters are used."""

    requested_classes = {str(item) for item in (args.source_class or []) if str(item).strip()}
    max_sources = max(0, int(args.max_sources or 0))
    if not requested_classes and max_sources <= 0:
        return None, {
            "selection_mode": "catalog_all",
            "source_class_filter": [],
            "max_sources": 0,
            "expanded_source_count": None,
            "selected_source_count": None,
        }

    expanded = load_source_catalog(catalog_path)
    filtered = [
        dict(source)
        for source in expanded
        if not requested_classes or source_class_for_record(source) in requested_classes
    ]
    minimum_quotas = source_minimum_quotas_from_args(args)
    quota_selection = apply_source_min_quotas(
        filtered,
        max_sources=max_sources,
        minimum_quotas=minimum_quotas,
    )
    selected = quota_selection.selected
    selected_class_counts = Counter(source_class_for_record(source) for source in selected)
    selected_group_counts = Counter(
        f"{source_class_for_record(source)}:{source.get('source_name') or source.get('name') or 'unknown_source'}"
        for source in selected
    )
    selected_quota_counts: Counter[str] = Counter()
    for source in selected:
        selected_quota_counts.update(source_quota_groups_for_record(source))
    return selected, {
        "selection_mode": "catalog_expanded_filtered",
        "source_class_filter": sorted(requested_classes),
        "max_sources": max_sources,
        "source_minimum_quotas": minimum_quotas,
        "expanded_source_count": len(expanded),
        "filtered_source_count": len(filtered),
        "selected_source_count": len(selected),
        "selected_source_classes": sorted({source_class_for_record(source) for source in selected}),
        "selected_source_class_counts": dict(sorted(selected_class_counts.items())),
        "selected_source_group_counts": dict(sorted(selected_group_counts.items())),
        "selected_source_quota_counts": dict(sorted(selected_quota_counts.items())),
        "source_quota_warnings": quota_selection.warnings,
        "selected_source_names": [str(source.get("source_name") or "") for source in selected],
    }


def main() -> int:
    args = parse_args()
    catalog_path = resolve_project_path(args.catalog)
    db_path = resolve_project_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and db_path.exists():
        db_path.unlink()

    settings = Settings(
        network={
            "enabled": True,
            "timeout_seconds": args.timeout_seconds,
            "max_records_per_fetch": args.max_records,
            "rate_limit_per_minute": args.rate_limit_per_minute,
            "retry_attempts": args.retry_attempts,
            "retry_backoff_seconds": args.retry_backoff_seconds,
            "retry_backoff_multiplier": args.retry_backoff_multiplier,
        },
        storage={
            "backend": "sql",
            "dsn": f"sqlite:///{db_path.as_posix()}",
            "auto_create_schema": True,
        },
    )

    runtime = LocalAgentRuntime(settings)
    try:
        inline_sources, selection_summary = selected_sources_from_args(args, catalog_path)
        payload = runtime.collect_sources_batch(
            source_config_path=None if inline_sources is not None else str(catalog_path.relative_to(APP_PROJECT_ROOT)),
            sources=inline_sources or (),
            persist_raw=True,
            run_pipeline=args.run_pipeline,
            continue_on_error=True,
        )
    finally:
        runtime.close()

    backend = connect(settings.storage.dsn)
    backend.create_schema()
    raw_rows = backend.list_raw()
    entity_rows = backend.list_entities()
    audit_rows = backend.list_audit()
    backend.close()

    summary = {
        "runtime_status": "ok",
        "catalog_path": str(catalog_path),
        "db_path": str(db_path),
        "source_selection": selection_summary,
        "status": payload.get("status"),
        "source_count": payload.get("source_count"),
        "succeeded_count": payload.get("succeeded_count"),
        "failed_count": payload.get("failed_count"),
        "fetched_count": payload.get("fetched_count"),
        "persisted_count": payload.get("persisted_count"),
        "stored_raw_count": len(raw_rows),
        "stored_entity_count": len(entity_rows),
        "audit_event_count": len(audit_rows),
        "sources": [
            {
                "source_name": item.get("source_name"),
                "fetched_count": item.get("fetched_count"),
                "error": item.get("error"),
                "sample": (item.get("raw_records") or [{}])[0].get("content_text", "")[:240],
                "matched_keywords": (item.get("raw_records") or [{}])[0].get("matched_keywords", []),
            }
            for item in payload.get("results", [])
        ],
    }
    if payload.get("pipeline_result"):
        summary["pipeline_result"] = {
            "risk_clue_count": payload["pipeline_result"].get("risk_clue_count"),
            "playbook_count": payload["pipeline_result"].get("playbook_count"),
            "strategy_count": payload["pipeline_result"].get("strategy_count"),
        }

    if args.summary_out:
        summary_path = resolve_project_path(args.summary_out)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") in {"completed", "partial_failure"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
