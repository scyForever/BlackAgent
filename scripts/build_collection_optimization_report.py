"""Build an auditable collection-optimization report from baseline/new runs.

The script intentionally separates two claims:

* raw-corpus skew in the current collection snapshot, and
* whether an authorized rerun or incremental DB actually improved forum /
  vertical coverage.

It does not fake collection.  If no rerun DB is supplied, the report remains a
baseline-only finding with concrete next commands.
"""

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

from src.collector.source_metadata import source_class_for_record
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build BlackAgent collection skew / rerun evidence report.")
    parser.add_argument("--baseline-db", default="data/collection_phase_delivery.db", help="Existing collection DB.")
    parser.add_argument(
        "--rerun-db",
        default=None,
        help="Optional authorized rerun / incremental DB to compare against baseline.",
    )
    parser.add_argument(
        "--output",
        default="data/collection_optimization_report.json",
        help="Report JSON output path.",
    )
    parser.add_argument(
        "--min-rerun-records",
        type=int,
        default=50,
        help="Minimum rerun raw rows before claiming rerun evidence is meaningful.",
    )
    parser.add_argument(
        "--target-im-share",
        type=float,
        default=0.70,
        help="Max acceptable im_or_group share for the rerun/incremental slice.",
    )
    return parser.parse_args()


def load_rows(db_path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(db_path)
    if not target.exists():
        return []
    backend = connect(f"sqlite:///{target.as_posix()}")
    backend.create_schema()
    rows = backend.list_raw()
    backend.close()
    return rows


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    source_counts = Counter(str(row.get("source_name") or "unknown") for row in rows)
    source_type_counts = Counter(str(row.get("source_type") or "unknown") for row in rows)
    source_class_counts = Counter(source_class_for_record(row) for row in rows)
    for expected_class in ("im_or_group", "social_or_forum", "vertical_or_technical", "other_authorized"):
        source_class_counts.setdefault(expected_class, 0)
    top_source_name, top_source_count = source_counts.most_common(1)[0] if source_counts else ("", 0)
    class_shares = {
        source_class: round(count / total, 4) if total else 0.0
        for source_class, count in sorted(source_class_counts.items())
    }
    return {
        "raw_record_count": total,
        "source_class_counts": [
            {"source_class": source_class, "count": count}
            for source_class, count in source_class_counts.most_common()
        ],
        "source_class_shares": class_shares,
        "source_type_counts": [
            {"source_type": source_type, "count": count}
            for source_type, count in source_type_counts.most_common()
        ],
        "top_sources": [
            {"source_name": source_name, "count": count}
            for source_name, count in source_counts.most_common(20)
        ],
        "top_source_name": top_source_name,
        "top_source_count": top_source_count,
        "top_source_share": round(top_source_count / total, 4) if total else 0.0,
        "im_or_group_share": class_shares.get("im_or_group", 0.0),
        "forum_vertical_count": source_class_counts.get("social_or_forum", 0)
        + source_class_counts.get("vertical_or_technical", 0),
    }


def build_report(
    *,
    baseline_rows: list[dict[str, Any]],
    rerun_rows: list[dict[str, Any]],
    rerun_supplied: bool,
    min_rerun_records: int,
    target_im_share: float,
) -> dict[str, Any]:
    baseline = summarize_rows(baseline_rows)
    rerun = summarize_rows(rerun_rows)
    baseline_im_share = float(baseline.get("im_or_group_share") or 0.0)
    rerun_im_share = float(rerun.get("im_or_group_share") or 0.0)
    rerun_record_count = int(rerun.get("raw_record_count") or 0)
    rerun_meaningful = rerun_supplied and rerun_record_count >= max(1, int(min_rerun_records))
    improved = (
        rerun_meaningful
        and rerun_im_share <= float(target_im_share)
        and rerun_im_share < baseline_im_share
        and int(rerun.get("forum_vertical_count") or 0) > 0
    )
    status = "rerun_improved_source_balance" if improved else "baseline_skew_still_open"
    if rerun_supplied and not rerun_meaningful:
        status = "rerun_insufficient_records"
    return {
        "status": status,
        "run_type": "collection_optimization_report",
        "baseline": baseline,
        "rerun": rerun if rerun_supplied else None,
        "requirements": {
            "min_rerun_records": max(1, int(min_rerun_records)),
            "target_im_or_group_share_max": float(target_im_share),
            "must_include_forum_or_vertical": True,
        },
        "improvement": {
            "rerun_supplied": rerun_supplied,
            "rerun_record_count": rerun_record_count,
            "baseline_im_or_group_share": baseline_im_share,
            "rerun_im_or_group_share": rerun_im_share if rerun_supplied else None,
            "forum_vertical_count": int(rerun.get("forum_vertical_count") or 0) if rerun_supplied else None,
            "can_claim_raw_skew_improved": improved,
        },
        "recommended_rerun_command": (
            "python scripts/collect_public_sources.py --catalog config/intel_sources.collection_optimization.yaml "
            "--db data/collection_phase_incremental_rerun.db --fresh --timeout-seconds 30 "
            "--max-records 20 --rate-limit-per-minute 6 --retry-attempts 2 --max-sources 8 "
            "--source-class social_or_forum --source-class vertical_or_technical "
            "--summary-out data/collection_phase_incremental_rerun_summary.json"
        ),
        "recommended_export_commands": [
            "python scripts/export_collection_phase_stats.py --db data/collection_phase_incremental_rerun.db --json-out data/collection_phase_incremental_rerun_stats.json",
            "python scripts/export_delivery_corpora.py --db data/collection_phase_incremental_rerun.db --raw-jsonl-out data/collection_phase_incremental_rerun_raw.jsonl --manifest-out data/collection_phase_incremental_rerun_manifest.json",
            "python scripts/build_collection_optimization_report.py --baseline-db data/collection_phase_delivery.db --rerun-db data/collection_phase_incremental_rerun.db --output data/collection_optimization_report.json --min-rerun-records 20",
        ],
        "claim_boundary": (
            "This report proves raw source balance only when rerun rows are supplied and meet the thresholds. "
            "Without a rerun DB, it is a baseline skew audit plus a reproducible rerun plan."
        ),
    }


def main() -> int:
    args = parse_args()
    baseline_rows = load_rows(args.baseline_db)
    rerun_supplied = bool(args.rerun_db)
    rerun_rows = load_rows(args.rerun_db) if rerun_supplied else []
    report = build_report(
        baseline_rows=baseline_rows,
        rerun_rows=rerun_rows,
        rerun_supplied=rerun_supplied,
        min_rerun_records=args.min_rerun_records,
        target_im_share=args.target_im_share,
    )
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"rerun_improved_source_balance", "baseline_skew_still_open"} else 1


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
