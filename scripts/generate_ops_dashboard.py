"""Aggregate production-readiness metrics into a dashboard-friendly JSON file."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.enhancement.text_intelligence import FineGrainedIntentClassifier


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate BlackAgent ops dashboard JSON.")
    parser.add_argument("--collection-stats", default="data/collection_phase_delivery_stats.json")
    parser.add_argument("--classification-summary", default="data/classification_extraction_phase_summary.json")
    parser.add_argument("--source-smoke", default="data/source_smoke_report.json")
    parser.add_argument("--scale-report", default="data/scale_benchmark_report.json")
    parser.add_argument("--llm-value", default="data/eval/latest_llm_value.json")
    parser.add_argument("--review-records", default=None, help="Optional JSONL to rerun current classifier for fresh review load.")
    parser.add_argument("--review-limit", type=int, default=0, help="Limit fresh review rerun records; 0 means all.")
    parser.add_argument("--output", default="data/ops_dashboard_report.json")
    return parser.parse_args(argv)


def build_dashboard(
    *,
    collection_stats: dict[str, Any] | None = None,
    classification_summary: dict[str, Any] | None = None,
    source_smoke: dict[str, Any] | None = None,
    scale_report: dict[str, Any] | None = None,
    llm_value: dict[str, Any] | None = None,
    review_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    collection_stats = dict(collection_stats or {})
    classification_summary = dict(classification_summary or {})
    source_smoke = dict(source_smoke or {})
    scale_report = dict(scale_report or {})
    llm_value = dict(llm_value or {})
    fresh_review = _fresh_review_load(review_records or []) if review_records is not None else None
    latest_scale = _latest_scale_scenario(scale_report)
    source_quality = _source_quality(source_smoke)
    baseline_review = _baseline_review_load(classification_summary)
    dashboard = {
        "status": "completed",
        "run_type": "ops_dashboard_json",
        "collection": {
            "total_raw_records": int(collection_stats.get("total_raw_records") or classification_summary.get("raw_record_count") or 0),
            "source_count": len(collection_stats.get("source_counts") or []),
            "unlabeled_record_count": int(collection_stats.get("unlabeled_record_count") or 0),
            "duplicate_rate": _duplicate_rate_from_summary(collection_stats),
        },
        "source_quality": source_quality,
        "classification_review_load": {
            "baseline": baseline_review,
            "fresh_current_classifier": fresh_review,
            "improvement_vs_baseline": _review_improvement(baseline_review, fresh_review),
        },
        "latency_and_scale": {
            "sample_size": latest_scale.get("sample_size"),
            "records_per_second": latest_scale.get("records_per_second"),
            "p95_record_latency_ms": latest_scale.get("p95_record_latency_ms"),
            "review_required_count": latest_scale.get("review_required_count"),
        },
        "llm_cost_and_value": {
            "record_enrich_policy": llm_value.get("record_enrich_policy"),
            "should_enable_record_enrich": llm_value.get("should_enable_record_enrich"),
            "gate_reason": llm_value.get("gate_reason"),
            "llm_calls_delta": llm_value.get("llm_calls_delta"),
            "tokens_per_extra_valid_clue": llm_value.get("tokens_per_extra_valid_clue"),
            "latest_scale_llm_calls_per_1000": latest_scale.get("llm_calls_per_1000_records"),
            "latest_scale_estimated_tokens_per_1000": latest_scale.get("estimated_tokens_per_1000_records"),
        },
        "dashboard_keys": [
            "collection.total_raw_records",
            "collection.duplicate_rate",
            "source_quality.failure_rate",
            "classification_review_load.*.review_rate",
            "latency_and_scale.p95_record_latency_ms",
            "llm_cost_and_value.latest_scale_estimated_tokens_per_1000",
        ],
        "claim_boundary": (
            "JSON dashboard for local/offline production-readiness review; it is not a live web monitoring service."
        ),
    }
    return dashboard


def _fresh_review_load(records: list[dict[str, Any]]) -> dict[str, Any]:
    classifier = FineGrainedIntentClassifier()
    reviewed = 0
    category_counter: Counter[str] = Counter()
    secondary_counter: Counter[str] = Counter()
    conflict_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    for raw in records:
        content = str(raw.get("content_text") or raw.get("clean_text") or "").strip()
        if not content:
            continue
        result = classifier.classify({**raw, "content_text": content}).model_dump()
        if bool(result.get("review_required")):
            reviewed += 1
            category_counter[str(result.get("risk_category") or "unknown")] += 1
            secondary_counter[str(result.get("secondary_label") or "待研判")] += 1
            conflict_counter[str(result.get("conflict_status") or "RESOLVED")] += 1
            reason_counter[str(result.get("review_decision_reason") or "unknown")] += 1
    return {
        "record_count": len(records),
        "review_required_count": reviewed,
        "review_rate": round(reviewed / max(len(records), 1), 4),
        "review_load_per_100_records": round(reviewed / max(len(records), 1) * 100.0, 4),
        "by_risk_category": _counter_rows(category_counter),
        "by_secondary_label": _counter_rows(secondary_counter),
        "by_conflict_status": _counter_rows(conflict_counter),
        "by_review_decision_reason": _counter_rows(reason_counter),
    }


def _baseline_review_load(summary: dict[str, Any]) -> dict[str, Any]:
    count = int(summary.get("classification_count") or summary.get("phase_input_count") or 0)
    reviewed = int(summary.get("review_required_count") or 0)
    return {
        "record_count": count,
        "review_required_count": reviewed,
        "review_rate": round(reviewed / max(count, 1), 4),
        "review_load_per_100_records": round(reviewed / max(count, 1) * 100.0, 4),
    }


def _review_improvement(baseline: dict[str, Any], fresh: dict[str, Any] | None) -> dict[str, Any] | None:
    if not fresh:
        return None
    base_rate = float(baseline.get("review_rate") or 0.0)
    fresh_rate = float(fresh.get("review_rate") or 0.0)
    return {
        "baseline_review_rate": base_rate,
        "fresh_review_rate": fresh_rate,
        "absolute_rate_delta": round(fresh_rate - base_rate, 4),
        "relative_reduction": None if base_rate <= 0 else round((base_rate - fresh_rate) / base_rate, 4),
    }


def _source_quality(report: dict[str, Any]) -> dict[str, Any]:
    sources = list(report.get("sources") or [])
    failures = [
        source
        for source in sources
        if source.get("failure_reason") or str(source.get("compliance_status") or "").upper() not in {"", "SCHEDULABLE"}
    ]
    duplicate_values = [
        float(source.get("duplicate_rate"))
        for source in sources
        if isinstance(source.get("duplicate_rate"), (int, float))
    ]
    return {
        "source_count": len(sources),
        "failure_count": len(failures),
        "failure_rate": round(len(failures) / max(len(sources), 1), 4),
        "covered_source_classes": report.get("covered_source_classes") or [],
        "missing_source_classes": report.get("missing_source_classes") or [],
        "average_duplicate_rate": round(sum(duplicate_values) / len(duplicate_values), 4) if duplicate_values else None,
    }


def _latest_scale_scenario(report: dict[str, Any]) -> dict[str, Any]:
    scenarios = [item for item in (report.get("scenarios") or []) if isinstance(item, dict)]
    if not scenarios:
        return {}
    return max(scenarios, key=lambda item: int(item.get("sample_size") or 0))


def _duplicate_rate_from_summary(summary: dict[str, Any]) -> float | None:
    values = [
        float(item.get("duplicate_rate"))
        for item in summary.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("duplicate_rate"), (int, float))
    ]
    return round(sum(values) / len(values), 4) if values else None


def _counter_rows(counter: Counter[str], *, limit: int = 12) -> list[dict[str, Any]]:
    return [{"value": key, "count": value} for key, value in counter.most_common(limit)]


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
    records: list[dict[str, Any]] = []
    if not target.exists():
        return records
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                records.append(json.loads(line))
            if limit and len(records) >= limit:
                break
    return records


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    review_records = load_jsonl(args.review_records, limit=max(0, args.review_limit)) if args.review_records else None
    dashboard = build_dashboard(
        collection_stats=load_json(args.collection_stats),
        classification_summary=load_json(args.classification_summary),
        source_smoke=load_json(args.source_smoke),
        scale_report=load_json(args.scale_report),
        llm_value=load_json(args.llm_value),
        review_records=review_records,
    )
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dashboard, ensure_ascii=False, indent=2))
    return 0


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
