"""Validate and materialize a human-confirmed held-out classification split.

The seeded held-out builder deliberately starts from deterministic local rules.
This validator is the handoff point for defense-ready evaluation: analysts fill
``human_review`` fields, and only confirmed rows are emitted as manual gold.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


CONFIRMED_STATUSES = {"confirmed", "corrected"}
REQUIRED_HUMAN_FIELDS = (
    "annotator",
    "review_date",
    "conflict_handling",
    "typical_error",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate BlackAgent human-confirmed held-out annotations.")
    parser.add_argument("--input", default="tests/evaluation/heldout_classification.jsonl", help="Seeded or manually edited held-out JSONL.")
    parser.add_argument(
        "--review-csv",
        default=None,
        help=(
            "Optional analyst-filled CSV exported by scripts/export_manual_heldout_review.py. "
            "CSV rows override matching human_review fields before validation."
        ),
    )
    parser.add_argument("--output", default="tests/evaluation/manual_heldout_classification.jsonl", help="Confirmed manual held-out JSONL to write.")
    parser.add_argument("--report", default="data/manual_heldout_report.json", help="Validation report JSON.")
    parser.add_argument("--min-records", type=int, default=100, help="Minimum confirmed rows required for defense-ready status.")
    return parser.parse_args(argv)


def validate_records(records: Iterable[Mapping[str, Any]], *, min_records: int = 50) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    confirmed: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    status_counter: Counter[str] = Counter()
    annotator_counter: Counter[str] = Counter()
    typical_error_counter: Counter[str] = Counter()
    conflict_counter: Counter[str] = Counter()

    for index, raw in enumerate(records, start=1):
        record = dict(raw)
        trace_id = str(record.get("source_trace_id") or record.get("trace_id") or f"line:{index}")
        review = record.get("human_review")
        if not isinstance(review, Mapping):
            issues.append({"trace_id": trace_id, "reason": "missing_human_review"})
            status_counter["missing_human_review"] += 1
            continue

        status = str(review.get("status") or "pending_human_confirmation")
        status_counter[status] += 1
        if status not in CONFIRMED_STATUSES:
            continue

        missing = [field for field in REQUIRED_HUMAN_FIELDS if not str(review.get(field) or "").strip()]
        final_categories = _split_labels(review.get("final_risk_categories"))
        if not final_categories:
            missing.append("final_risk_categories")
        if missing:
            issues.append({"trace_id": trace_id, "reason": "missing_required_human_fields", "fields": missing})
            continue

        materialized = dict(record)
        materialized["expected_risk_categories"] = final_categories
        materialized["expected_secondary_labels"] = _split_labels(review.get("final_secondary_labels"))
        materialized["annotation_source"] = "human_confirmed"
        materialized["dataset_kind"] = "manual_heldout_public_authorized"
        materialized["annotation_note"] = "Human-confirmed held-out label; see human_review for annotator/date/conflict handling."
        confirmed.append(materialized)
        annotator_counter[str(review.get("annotator"))] += 1
        typical_error = str(review.get("typical_error") or "none").strip() or "none"
        typical_error_counter[typical_error] += 1
        conflict_counter[str(review.get("conflict_handling"))] += 1

    report = {
        "status": "completed" if len(confirmed) >= max(1, int(min_records)) else "insufficient_confirmed_records",
        "run_type": "validate_manual_heldout",
        "input_record_count": sum(status_counter.values()),
        "confirmed_record_count": len(confirmed),
        "min_required_records": max(1, int(min_records)),
        "confirmed_record_gap": max(0, max(1, int(min_records)) - len(confirmed)),
        "human_review_status_counts": dict(status_counter),
        "annotator_counts": dict(annotator_counter),
        "conflict_handling_counts": dict(conflict_counter),
        "typical_error_counts": dict(typical_error_counter),
        "issue_count": len(issues),
        "issues": issues[:50],
        "manual_gold_claim": {
            "can_claim_manual_gold": len(confirmed) >= max(1, int(min_records)),
            "claim_status": "human_confirmed_gold_ready"
            if len(confirmed) >= max(1, int(min_records))
            else "review_package_only",
            "required_next_step": "Collect analyst-filled review_csv rows with confirmed/corrected status and required human fields."
            if len(confirmed) < max(1, int(min_records))
            else "Use output JSONL as manual held-out gold with the report attached.",
        },
        "claim_boundary": (
            "Only rows with human_review.status in confirmed/corrected and required annotator/date/conflict fields "
            "are emitted as manual held-out gold."
        ),
    }
    return confirmed, report


def merge_review_csv(records: Iterable[Mapping[str, Any]], review_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Overlay analyst CSV review rows onto seeded held-out records by trace id."""

    review_by_trace = {
        str(row.get("source_trace_id") or row.get("trace_id") or "").strip(): row
        for row in review_rows
        if str(row.get("source_trace_id") or row.get("trace_id") or "").strip()
    }
    merged: list[dict[str, Any]] = []
    for raw in records:
        record = dict(raw)
        trace_id = str(record.get("source_trace_id") or record.get("trace_id") or "").strip()
        review_row = review_by_trace.get(trace_id)
        if not review_row:
            merged.append(record)
            continue
        existing_review = record.get("human_review") if isinstance(record.get("human_review"), Mapping) else {}
        record["human_review"] = {
            **dict(existing_review),
            "status": str(review_row.get("status") or existing_review.get("status") or "").strip(),
            "annotator": str(review_row.get("annotator") or existing_review.get("annotator") or "").strip(),
            "review_date": str(review_row.get("review_date") or existing_review.get("review_date") or "").strip(),
            "final_risk_categories": _split_labels(review_row.get("final_risk_categories")) or _non_empty_list(existing_review.get("final_risk_categories")),
            "final_secondary_labels": _split_labels(review_row.get("final_secondary_labels")) or _non_empty_list(existing_review.get("final_secondary_labels")),
            "conflict_handling": str(review_row.get("conflict_handling") or existing_review.get("conflict_handling") or "").strip(),
            "typical_error": str(review_row.get("typical_error") or existing_review.get("typical_error") or "").strip(),
            "notes": str(review_row.get("notes") or existing_review.get("notes") or "").strip(),
        }
        merged.append(record)
    return merged


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(path)
    records: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                records.append(json.loads(line))
    return records


def load_review_csv(path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as file_obj:
        return [dict(row) for row in csv.DictReader(file_obj)]


def write_jsonl(records: Iterable[Mapping[str, Any]], path: str | Path) -> Path:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file_obj:
        for record in records:
            file_obj.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return target


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_records = load_jsonl(args.input)
    if args.review_csv:
        input_records = merge_review_csv(input_records, load_review_csv(args.review_csv))
    confirmed, report = validate_records(input_records, min_records=args.min_records)
    if args.review_csv:
        report["review_csv"] = str(_project_path(args.review_csv).relative_to(PROJECT_ROOT))
    output = write_jsonl(confirmed, args.output)
    report["output"] = str(output.relative_to(PROJECT_ROOT) if output.is_relative_to(PROJECT_ROOT) else output)
    report_path = _project_path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _non_empty_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates: Iterable[Any] = [value]
    elif isinstance(value, Iterable):
        candidates = value
    else:
        candidates = ()
    return [str(item).strip() for item in candidates if str(item).strip()]


def _split_labels(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_parts = value.replace("；", ";").replace("，", ";").replace(",", ";").split(";")
    elif isinstance(value, Iterable):
        raw_parts = list(value)
    else:
        raw_parts = [value]
    return [str(item).strip() for item in raw_parts if str(item).strip()]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
