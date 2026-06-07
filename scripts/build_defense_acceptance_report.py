"""Build a one-shot defense acceptance summary from existing artifacts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.classifier.nlp_rule_matcher import review_bucket_for_classification

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build BlackAgent defense acceptance summary JSON.")
    parser.add_argument("--collection-stats", default="data/collection_phase_delivery_stats.json")
    parser.add_argument("--cleaning-summary", default="data/cleaning_phase_summary.json")
    parser.add_argument("--classification-summary", default="data/classification_extraction_phase_summary.json")
    parser.add_argument("--classifications-jsonl", default="data/classification_extraction_phase_classifications.jsonl")
    parser.add_argument("--entities-jsonl", default="data/classification_extraction_phase_entities.jsonl")
    parser.add_argument("--e2e-evidence", default="data/acceptance_real_e2e_evidence.json")
    parser.add_argument("--eval-report", default="data/eval_heldout_report.json")
    parser.add_argument("--output", default="data/defense_acceptance_report.json")
    parser.add_argument(
        "--test-command",
        action="append",
        default=[],
        help="Explicit verification command to run and include in the report. Can be repeated.",
    )
    parser.add_argument(
        "--run-tests",
        action="store_true",
        help="Run --test-command entries. Without this flag commands are recorded as not_run.",
    )
    return parser.parse_args(argv)


def load_json(path: str | Path) -> dict[str, Any]:
    target = _project_path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_jsonl(path: str | Path, *, limit: int = 0) -> list[dict[str, Any]]:
    target = _project_path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                rows.append(json.loads(line))
            if limit and len(rows) >= limit:
                break
    return rows


def build_report(
    *,
    collection_stats: dict[str, Any] | None = None,
    cleaning_summary: dict[str, Any] | None = None,
    classification_summary: dict[str, Any] | None = None,
    classifications: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
    e2e_evidence: dict[str, Any] | None = None,
    eval_report: dict[str, Any] | None = None,
    test_commands: Iterable[str] | None = None,
    run_tests: bool = False,
) -> dict[str, Any]:
    collection_stats = dict(collection_stats or {})
    cleaning_summary = dict(cleaning_summary or {})
    classification_summary = dict(classification_summary or {})
    classification_rows = list(classifications or [])
    entity_rows = list(entities or [])
    e2e_evidence = dict(e2e_evidence or {})
    eval_report = dict(eval_report or {})

    report = {
        "status": "completed",
        "run_type": "defense_acceptance_summary",
        "collection_coverage": _collection_coverage(collection_stats),
        "cleaning_stats": _cleaning_stats(cleaning_summary),
        "classification_stats": _classification_stats(classification_summary, classification_rows),
        "entity_stats": _entity_stats(entity_rows, classification_summary),
        "clue_stats": _clue_stats(e2e_evidence),
        "clue_samples": _clue_samples(e2e_evidence),
        "evaluation_metrics": _evaluation_metrics(eval_report),
        "test_results": _run_test_commands(test_commands or [], run_tests=run_tests),
        "acceptance_keys": [
            "collection_coverage.source_class_counts",
            "cleaning_stats.cleaned_count",
            "classification_stats.category_counts",
            "entity_stats.entity_type_counts",
            "clue_samples",
            "evaluation_metrics.primary_classification_f1",
            "test_results",
        ],
        "claim_boundary": (
            "This report aggregates local artifacts and explicit verification commands. It does not "
            "claim fresh live collection unless the referenced artifacts were produced by a live run."
        ),
    }
    return report


def write_report(report: dict[str, Any], path: str | Path) -> None:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _collection_coverage(summary: dict[str, Any]) -> dict[str, Any]:
    skew = summary.get("source_skew") if isinstance(summary.get("source_skew"), dict) else {}
    source_rows = skew.get("source_class_counts") or summary.get("source_class_counts") or []
    counts = _rows_to_count_map(source_rows, "source_class")
    return {
        "total_raw_records": int(summary.get("total_raw_records") or skew.get("total_raw_records") or 0),
        "source_class_counts": counts,
        "im_or_group_share": float(skew.get("im_or_group_share") or 0.0),
        "warnings": [str(item) for item in (skew.get("warnings") or [])],
    }


def _cleaning_stats(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_count": int(summary.get("input_count") or 0),
        "cleaned_count": int(summary.get("cleaned_count") or 0),
        "dropped_count": int(summary.get("dropped_count") or 0),
        "high_risk_count": int(summary.get("high_risk_count") or 0),
        "drop_reason_counts": _rows_to_count_map(summary.get("drop_reason_counts") or [], "reason"),
    }


def _classification_stats(summary: dict[str, Any], classifications: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    category_counts = _rows_to_count_map(summary.get("category_counts") or [], "risk_category")
    secondary_counts = _rows_to_count_map(summary.get("secondary_label_counts") or [], "secondary_label")
    review_required = int(summary.get("review_required_count") or 0)
    unknown_count = int(category_counts.get("unknown") or 0)
    low_relevance = int(category_counts.get("正常业务白噪声") or 0) + int(secondary_counts.get("低相关") or 0)
    explicit_risk = sum(
        count
        for label, count in category_counts.items()
        if label not in {"unknown", "正常业务白噪声", "待研判", ""}
    )
    estimate_split = {
        "explicit_risk": explicit_risk,
        "low_relevance": low_relevance,
        "human_review_required": review_required,
    }
    record_buckets = _record_review_buckets(classifications or [])
    has_record_buckets = bool(classifications)
    return {
        "classification_count": int(summary.get("classification_count") or summary.get("phase_input_count") or 0),
        "category_counts": category_counts,
        "secondary_label_counts": secondary_counts,
        "unknown_count": unknown_count,
        "pending_secondary_count": int(secondary_counts.get("待研判") or 0),
        "unspecified_secondary_count": int(secondary_counts.get("未细分") or 0),
        "review_required_count": review_required,
        "review_split": record_buckets if has_record_buckets else estimate_split,
        "review_split_source": "classification_rows" if has_record_buckets else "summary_estimate",
        "summary_estimate_review_split": estimate_split,
        "record_review_buckets": record_buckets,
        "record_review_bucket_total": len(classifications or []),
    }


def _entity_stats(rows: list[dict[str, Any]], classification_summary: dict[str, Any]) -> dict[str, Any]:
    type_counts = Counter(str(row.get("entity_type") or "unknown") for row in rows)
    trace_ids = {
        str(row.get("source_trace_id") or row.get("trace_id") or "")
        for row in rows
        if str(row.get("source_trace_id") or row.get("trace_id") or "").strip()
    }
    return {
        "entity_count": len(rows) or int(classification_summary.get("entity_count") or 0),
        "entity_type_counts": dict(sorted(type_counts.items())),
        "unique_trace_count": len(trace_ids),
    }


def _clue_stats(evidence: dict[str, Any]) -> dict[str, Any]:
    counts = evidence.get("counts") if isinstance(evidence.get("counts"), dict) else {}
    clues = [item for item in evidence.get("agent_final_output") or [] if isinstance(item, dict)]
    return {
        "status": evidence.get("status"),
        "risk_clue_count": int(counts.get("risk_clue_count") or len(clues)),
        "high_quality_count": int(counts.get("high_quality_count") or len(clues)),
        "sample_count": len(clues),
    }


def _clue_samples(evidence: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    clues = [item for item in evidence.get("agent_final_output") or [] if isinstance(item, dict)]
    samples: list[dict[str, Any]] = []
    for clue in clues[:limit]:
        samples.append(
            {
                "clue_id": clue.get("clue_id"),
                "clue_type": clue.get("clue_type"),
                "risk_category": clue.get("risk_category"),
                "evidence_trace_count": int(clue.get("evidence_trace_count") or len(clue.get("evidence_trace_ids") or [])),
                "source_names": [str(item) for item in (clue.get("source_names") or [])],
                "quality_level": clue.get("quality_level"),
                "review_required": bool(clue.get("quality_review_required") or clue.get("llm_review_required")),
            }
        )
    return samples


def _evaluation_metrics(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "primary_classification_f1": report.get("primary_classification_f1"),
        "secondary_classification_f1": report.get("secondary_classification_f1"),
        "hierarchical_classification_f1": report.get("hierarchical_classification_f1"),
        "false_positive_rate": report.get("false_positive_rate"),
        "classification_review_rate": report.get("classification_review_rate"),
        "entity_f1": report.get("entity_f1"),
        "dataset": report.get("dataset") or {},
    }


def _record_review_buckets(classifications: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in classifications:
        bucket = review_bucket_for_classification(
            risk_category=str(item.get("risk_category") or "").strip(),
            review_required=bool(item.get("review_required")),
            secondary_label=str(item.get("secondary_label") or "").strip(),
            conflict_status=str(item.get("conflict_status") or "").strip(),
        )
        counter[bucket] += 1
    return {
        "explicit_risk": counter.get("explicit_risk", 0),
        "low_relevance": counter.get("low_relevance", 0),
        "human_review_required": counter.get("human_review_required", 0),
    }


def _run_test_commands(commands: Iterable[str], *, run_tests: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        command = str(command).strip()
        if not command:
            continue
        if not run_tests:
            results.append({"command": command, "status": "not_run", "returncode": None})
            continue
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            shell=True,
            text=True,
            capture_output=True,
            timeout=600,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        results.append(
            {
                "command": command,
                "status": "passed" if completed.returncode == 0 else "failed",
                "returncode": completed.returncode,
                "elapsed_ms": elapsed_ms,
                "stdout_excerpt": completed.stdout[-2000:],
                "stderr_excerpt": completed.stderr[-2000:],
            }
        )
    return results


def _rows_to_count_map(rows: Any, name_key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get(name_key) or row.get("value") or row.get("name") or "").strip()
        if name:
            counts[name] = int(row.get("count") or 0)
    return counts


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        collection_stats=load_json(args.collection_stats),
        cleaning_summary=load_json(args.cleaning_summary),
        classification_summary=load_json(args.classification_summary),
        classifications=load_jsonl(args.classifications_jsonl),
        entities=load_jsonl(args.entities_jsonl),
        e2e_evidence=load_json(args.e2e_evidence),
        eval_report=load_json(args.eval_report),
        test_commands=args.test_command,
        run_tests=bool(args.run_tests),
    )
    write_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed_tests = [
        result
        for result in report.get("test_results", [])
        if result.get("returncode") not in (None, 0)
    ]
    return 1 if failed_tests else 0


if __name__ == "__main__":
    raise SystemExit(main())
