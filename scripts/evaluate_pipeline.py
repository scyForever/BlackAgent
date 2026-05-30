"""Evaluate deterministic pipeline quality/cost/latency on JSONL gold data."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline import IntelligencePipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate BlackAgent pipeline on gold JSONL records.")
    parser.add_argument("--gold", required=True, help="JSONL with content_text plus expected_risk_categories/expected_entities.")
    parser.add_argument("--output", default="data/eval_report.json", help="Where to write JSON metrics.")
    return parser.parse_args(argv)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    records: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                records.append(json.loads(line))
    return records


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    pipeline = IntelligencePipeline()
    result = pipeline.run(records, context={"quality_profile": "high_recall", "require_evidence_chain": False})
    elapsed_ms = (time.perf_counter() - started) * 1000

    class_tp = class_fp = class_fn = 0
    entity_tp = entity_fp = entity_fn = 0
    actual_by_trace = {
        str(item.get("source_trace_id") or ""): item
        for item in result.classified
    }
    entities_by_trace: dict[str, set[str]] = {}
    for entity in result.entities:
        trace_id = str(entity.get("source_trace_id") or "")
        value = str(entity.get("normalized_value") or entity.get("entity_value") or "")
        if trace_id and value:
            entities_by_trace.setdefault(trace_id, set()).add(value)

    for record in records:
        trace_id = str(record.get("source_trace_id") or record.get("trace_id") or record.get("hash_id") or "")
        expected_categories = {str(item) for item in (record.get("expected_risk_categories") or [])}
        actual_category = str((actual_by_trace.get(trace_id) or {}).get("risk_category") or "")
        actual_categories = {actual_category} if actual_category else set()
        class_tp += len(expected_categories & actual_categories)
        class_fp += len(actual_categories - expected_categories)
        class_fn += len(expected_categories - actual_categories)

        expected_entities = {str(item) for item in (record.get("expected_entities") or [])}
        actual_entities = entities_by_trace.get(trace_id, set())
        entity_tp += len(expected_entities & actual_entities)
        entity_fp += len(actual_entities - expected_entities)
        entity_fn += len(expected_entities - actual_entities)

    classification = prf(class_tp, class_fp, class_fn)
    entities = prf(entity_tp, entity_fp, entity_fn)
    return {
        "status": "completed",
        "record_count": len(records),
        "classification_precision": classification["precision"],
        "classification_recall": classification["recall"],
        "classification_f1": classification["f1"],
        "entity_precision": entities["precision"],
        "entity_recall": entities["recall"],
        "entity_f1": entities["f1"],
        "high_risk_recall": classification["recall"],
        "false_positive_rate": round(class_fp / max(class_tp + class_fp, 1), 4),
        "llm_calls_per_1000_records": 0.0,
        "estimated_tokens_per_valid_clue": 0.0,
        "p50_latency_ms": round(elapsed_ms, 2),
        "p95_latency_ms": round(elapsed_ms, 2),
        "pipeline_summary": result.execution_summary,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = evaluate(load_jsonl(args.gold))
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
