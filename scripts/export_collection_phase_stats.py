"""Export theme/source counts for collection-phase raw_records."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.collector.relevance import (
    DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS,
    decide_text_relevance,
    load_theme_synonym_registry,
)
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export source/theme counts for collection-phase delivery.")
    parser.add_argument("--db", default="data/collection_phase_delivery.db", help="SQLite DB path")
    parser.add_argument("--json-out", default="data/collection_phase_delivery_stats.json", help="Summary JSON output path")
    parser.add_argument("--theme-csv", default="data/collection_phase_theme_counts.csv", help="Theme CSV output path")
    parser.add_argument("--source-csv", default="data/collection_phase_source_counts.csv", help="Source CSV output path")
    parser.add_argument("--cross-csv", default="data/collection_phase_theme_source_counts.csv", help="Theme/source cross CSV output path")
    parser.add_argument(
        "--refresh-relevance",
        action="store_true",
        help="Re-evaluate matched keywords/themes from content_text using the current relevance policy before exporting stats",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="When used with --refresh-relevance, persist refreshed matched keyword/theme fields back into raw_records payloads",
    )
    return parser.parse_args()


def write_csv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.writer(file_obj)
        writer.writerow(headers)
        writer.writerows(rows)


def refresh_relevance(rows: list[dict[str, Any]], *, write_back_backend: Any | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_themes = tuple(load_theme_synonym_registry().keys())
    changed_rows = 0
    crowd_before = 0
    crowd_after = 0
    removed_crowd_sources: Counter[str] = Counter()
    refreshed_rows: list[dict[str, Any]] = []

    for row in rows:
        original_themes = tuple(str(item) for item in (row.get("matched_themes") or []) if str(item).strip())
        if "众包任务" in original_themes:
            crowd_before += 1

        decision = decide_text_relevance(
            row.get("content_text"),
            include_themes=all_themes,
            exclude_keywords=DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS,
            min_keyword_hits=1,
        )
        refreshed = dict(row)
        refreshed["matched_keywords"] = list(decision.matched_keywords)
        refreshed["excluded_keywords"] = list(decision.excluded_keywords)
        refreshed["matched_themes"] = list(decision.matched_themes)
        refreshed["excluded_themes"] = list(decision.excluded_themes)
        refreshed["keyword_hit_count"] = decision.hit_count
        refreshed["relevance_version"] = decision.policy_version

        refreshed_themes = tuple(str(item) for item in refreshed["matched_themes"] if str(item).strip())
        if "众包任务" in refreshed_themes:
            crowd_after += 1
        elif "众包任务" in original_themes:
            removed_crowd_sources[str(row.get("source_name") or "unknown")] += 1

        original_fingerprint = (
            tuple(str(item) for item in (row.get("matched_keywords") or [])),
            original_themes,
            tuple(str(item) for item in (row.get("excluded_keywords") or [])),
            tuple(str(item) for item in (row.get("excluded_themes") or [])),
            int(row.get("keyword_hit_count") or 0),
            str(row.get("relevance_version") or ""),
        )
        refreshed_fingerprint = (
            tuple(refreshed["matched_keywords"]),
            refreshed_themes,
            tuple(refreshed["excluded_keywords"]),
            tuple(refreshed["excluded_themes"]),
            int(refreshed["keyword_hit_count"]),
            str(refreshed["relevance_version"]),
        )
        if original_fingerprint != refreshed_fingerprint:
            changed_rows += 1
            if write_back_backend is not None:
                write_back_backend.save_raw(refreshed)

        refreshed_rows.append(refreshed)

    refresh_summary = {
        "changed_row_count": changed_rows,
        "crowd_theme_before": crowd_before,
        "crowd_theme_after": crowd_after,
        "crowd_theme_removed_row_count": max(0, crowd_before - crowd_after),
        "removed_crowd_source_counts": [
            {"source_name": source_name, "count": count}
            for source_name, count in removed_crowd_sources.most_common()
        ],
    }
    return refreshed_rows, refresh_summary


def main() -> int:
    args = parse_args()
    db_path = (PROJECT_ROOT / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db).resolve()
    json_out = (PROJECT_ROOT / args.json_out).resolve() if not Path(args.json_out).is_absolute() else Path(args.json_out).resolve()
    theme_csv = (PROJECT_ROOT / args.theme_csv).resolve() if not Path(args.theme_csv).is_absolute() else Path(args.theme_csv).resolve()
    source_csv = (PROJECT_ROOT / args.source_csv).resolve() if not Path(args.source_csv).is_absolute() else Path(args.source_csv).resolve()
    cross_csv = (PROJECT_ROOT / args.cross_csv).resolve() if not Path(args.cross_csv).is_absolute() else Path(args.cross_csv).resolve()

    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    rows = backend.list_raw()
    refresh_summary: dict[str, Any] | None = None
    if args.refresh_relevance:
        write_backend = backend if args.write_back else None
        rows, refresh_summary = refresh_relevance(rows, write_back_backend=write_backend)
    backend.close()

    source_counts: Counter[str] = Counter()
    theme_counts: Counter[str] = Counter()
    cross_counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    unlabeled_count = 0

    for row in rows:
        source_name = str(row.get("source_name") or "unknown")
        source_counts[source_name] += 1
        themes = [str(item) for item in (row.get("matched_themes") or []) if str(item).strip()]
        if not themes:
            unlabeled_count += 1
        for theme in sorted(set(themes)):
            theme_counts[theme] += 1
            cross_counts[(theme, source_name)] += 1

    summary = {
        "db_path": str(db_path),
        "total_raw_records": len(rows),
        "unlabeled_record_count": unlabeled_count,
        "theme_counting_rule": "multi_label_membership_count",
        "relevance_refreshed": bool(args.refresh_relevance),
        "relevance_write_back": bool(args.refresh_relevance and args.write_back),
        "theme_counts": [{"theme": theme, "count": count} for theme, count in theme_counts.most_common()],
        "source_counts": [{"source_name": name, "count": count} for name, count in source_counts.most_common()],
        "theme_source_counts": [
            {"theme": theme, "source_name": source_name, "count": count}
            for (theme, source_name), count in sorted(cross_counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
        ],
    }
    if refresh_summary is not None:
        summary["relevance_refresh_summary"] = refresh_summary

    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(theme_csv, ["theme", "count"], [[item["theme"], item["count"]] for item in summary["theme_counts"]])
    write_csv(source_csv, ["source_name", "count"], [[item["source_name"], item["count"]] for item in summary["source_counts"]])
    write_csv(
        cross_csv,
        ["theme", "source_name", "count"],
        [[item["theme"], item["source_name"], item["count"]] for item in summary["theme_source_counts"]],
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
