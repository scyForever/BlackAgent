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
    parser.add_argument("--collection-stats", default="data/collection_phase_delivery_manifest.json")
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
    collection_coverage = _collection_coverage(collection_stats)
    cleaning_stats = _cleaning_stats(cleaning_summary)
    classification_stats = _classification_stats(classification_summary, classification_rows)
    entity_stats = _entity_stats(entity_rows, classification_summary)
    clue_stats = _clue_stats(e2e_evidence)
    clue_samples = _clue_samples(e2e_evidence)
    evaluation_metrics = _evaluation_metrics(eval_report)
    test_results = _run_test_commands(test_commands or [], run_tests=run_tests)
    referenced_run = _referenced_run_artifact(e2e_evidence)

    report = {
        "status": "completed",
        "run_type": "defense_acceptance_summary",
        "collection_coverage": collection_coverage,
        "cleaning_stats": cleaning_stats,
        "classification_stats": classification_stats,
        "entity_stats": entity_stats,
        "clue_stats": clue_stats,
        "clue_samples": clue_samples,
        "evaluation_metrics": evaluation_metrics,
        "test_results": test_results,
        "end_to_end_demo": _end_to_end_demo(
            e2e_evidence=e2e_evidence,
            collection_coverage=collection_coverage,
            cleaning_stats=cleaning_stats,
            classification_stats=classification_stats,
            entity_stats=entity_stats,
            clue_stats=clue_stats,
            clue_samples=clue_samples,
            evaluation_metrics=evaluation_metrics,
            test_results=test_results,
            referenced_run=referenced_run,
        ),
        "acceptance_keys": [
            "collection_coverage.source_class_counts",
            "cleaning_stats.cleaned_count",
            "classification_stats.category_counts",
            "entity_stats.entity_type_counts",
            "clue_samples",
            "end_to_end_demo",
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
    total_raw_records = int(summary.get("total_raw_records") or summary.get("raw_record_count") or skew.get("total_raw_records") or 0)
    im_or_group_count = int(counts.get("im_or_group") or 0)
    im_or_group_share = (
        float(skew.get("im_or_group_share"))
        if skew.get("im_or_group_share") is not None
        else round(im_or_group_count / total_raw_records, 4)
        if total_raw_records
        else 0.0
    )
    defense_sample = summary.get("defense_quota_balanced_sample") if isinstance(summary.get("defense_quota_balanced_sample"), dict) else {}
    return {
        "total_raw_records": total_raw_records,
        "source_class_counts": counts,
        "im_or_group_share": im_or_group_share,
        "warnings": [str(item) for item in (skew.get("warnings") or defense_sample.get("warnings") or [])],
        "defense_balanced_sample": _balanced_sample_coverage(defense_sample),
    }


def _balanced_sample_coverage(sample: dict[str, Any]) -> dict[str, Any]:
    if not sample:
        return {
            "selected_count": 0,
            "source_class_counts": {},
            "strict_balance": False,
            "warnings": [],
        }
    return {
        "selected_count": int(sample.get("selected_count") or 0),
        "source_class_counts": _rows_to_count_map(sample.get("class_counts") or [], "source_class"),
        "strict_balance": bool(sample.get("strict_balance")),
        "warnings": [str(item) for item in (sample.get("warnings") or [])],
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
        reviewability = clue.get("evidence_reviewability") if isinstance(clue.get("evidence_reviewability"), dict) else {}
        samples.append(
            {
                "clue_id": clue.get("clue_id"),
                "clue_type": clue.get("clue_type"),
                "risk_category": clue.get("risk_category"),
                "evidence_trace_count": int(clue.get("evidence_trace_count") or len(clue.get("evidence_trace_ids") or [])),
                "source_names": [str(item) for item in (clue.get("source_names") or [])],
                "quality_level": clue.get("quality_level"),
                "review_required": bool(clue.get("quality_review_required") or clue.get("llm_review_required")),
                "suggested_review_action": clue.get("suggested_review_action") or reviewability.get("suggested_review_action"),
                "review_action_reasons": [str(item) for item in (reviewability.get("review_action_reasons") or [])],
            }
        )
    return samples


def _end_to_end_demo(
    *,
    e2e_evidence: dict[str, Any],
    collection_coverage: dict[str, Any],
    cleaning_stats: dict[str, Any],
    classification_stats: dict[str, Any],
    entity_stats: dict[str, Any],
    clue_stats: dict[str, Any],
    clue_samples: list[dict[str, Any]],
    evaluation_metrics: dict[str, Any],
    test_results: list[dict[str, Any]],
    referenced_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    counts = e2e_evidence.get("counts") if isinstance(e2e_evidence.get("counts"), dict) else {}
    execution_summary = (
        e2e_evidence.get("execution_summary")
        if isinstance(e2e_evidence.get("execution_summary"), dict)
        else (referenced_run or {}).get("execution_summary")
        if isinstance((referenced_run or {}).get("execution_summary"), dict)
        else {}
    )
    collection_runs = [item for item in e2e_evidence.get("collection_runs") or [] if isinstance(item, dict)]
    return {
        "query": str(e2e_evidence.get("query") or "取当天诈骗引流相关线索"),
        "evidence_scope": _demo_evidence_scope(e2e_evidence, collection_runs),
        "source_selection": _demo_source_selection(e2e_evidence, collection_runs, collection_coverage),
        "collection": {
            "input_count": int(counts.get("input_count") or e2e_evidence.get("input_count") or 0),
            "fetched_count": int(
                counts.get("fetched_count")
                or e2e_evidence.get("fetched_count")
                or sum(int(item.get("fetched_count") or 0) for item in collection_runs)
            ),
            "selected_source_count": int(
                counts.get("selected_source_count")
                or e2e_evidence.get("selected_source_count")
                or len(_unique_values(item.get("source_name") for item in collection_runs))
            ),
            "collection_runs": _demo_collection_runs(collection_runs),
            "coverage": collection_coverage,
        },
        "cleaning": {
            **cleaning_stats,
            "accepted_count": int(counts.get("accepted_count") or cleaning_stats.get("cleaned_count") or 0),
        },
        "classification": classification_stats,
        "entities": {
            **entity_stats,
            "e2e_entity_count": int(counts.get("entity_count") or entity_stats.get("entity_count") or 0),
        },
        "clues": {
            **clue_stats,
            "samples": clue_samples,
            "evidence_chain": _demo_evidence_chain(e2e_evidence),
        },
        "cost_latency": _demo_cost_latency(e2e_evidence, execution_summary, referenced_run=referenced_run or {}),
        "verification": {
            "evaluation_metrics": evaluation_metrics,
            "test_results": test_results,
        },
        "claim_boundary": (
            "End-to-end demo evidence is assembled from local artifacts and explicit test commands; "
            "live collection is claimed only when the evidence artifact itself records a live run."
        ),
    }


def _demo_source_selection(
    evidence: dict[str, Any],
    collection_runs: list[dict[str, Any]],
    collection_coverage: dict[str, Any],
) -> dict[str, Any]:
    plan = evidence.get("investigation_plan") if isinstance(evidence.get("investigation_plan"), dict) else {}
    strategy = evidence.get("source_selection_strategy")
    if not isinstance(strategy, dict):
        strategy = plan.get("source_selection_strategy") if isinstance(plan.get("source_selection_strategy"), dict) else {}
    selected_names = _string_list(evidence.get("selected_source_names")) or _string_list(plan.get("selected_source_names"))
    if not selected_names:
        selected_names = _unique_values(item.get("source_name") for item in collection_runs)
    selected_classes = (
        _string_list(evidence.get("selected_source_classes"))
        or _string_list(evidence.get("collection_source_classes_executed"))
        or _unique_values(item.get("source_class") for item in collection_runs)
        or list((collection_coverage.get("source_class_counts") or {}).keys())
    )
    return {
        "selected_source_names": selected_names,
        "selected_source_classes": selected_classes,
        "strategy": strategy,
        "source_catalog": evidence.get("source_catalog"),
        "command": evidence.get("command"),
    }


def _demo_evidence_scope(evidence: dict[str, Any], collection_runs: list[dict[str, Any]]) -> dict[str, Any]:
    status = str(evidence.get("status") or "").strip()
    has_single_e2e_artifact = bool(status or collection_runs or evidence.get("agent_final_output"))
    mode = (
        "single_e2e_artifact_with_supporting_aggregates"
        if has_single_e2e_artifact
        else "supporting_aggregates_only"
    )
    return {
        "mode": mode,
        "e2e_status": status or None,
        "uses_supporting_aggregates": True,
        "supporting_aggregate_sections": [
            "collection_coverage",
            "cleaning_stats",
            "classification_stats",
            "entity_stats",
            "evaluation_metrics",
            "test_results",
        ],
        "claim_boundary": (
            "Demo steps come from one explicit e2e evidence artifact when present; "
            "coverage, evaluation, and verification fields are supporting aggregate artifacts."
        ),
    }


def _demo_collection_runs(collection_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in collection_runs:
        rows.append(
            {
                "source_name": item.get("source_name"),
                "source_class": item.get("source_class"),
                "collection_layer": item.get("collection_layer"),
                "fetched_count": int(item.get("fetched_count") or 0),
                "status": item.get("status"),
            }
        )
    return rows


def _demo_evidence_chain(evidence: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    clues = [item for item in evidence.get("agent_final_output") or [] if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for clue in clues[:limit]:
        raw_chain = clue.get("evidence_chain") if isinstance(clue.get("evidence_chain"), list) else []
        reviewability = clue.get("evidence_reviewability") if isinstance(clue.get("evidence_reviewability"), dict) else {}
        evidence_trace_ids = _string_list(clue.get("evidence_trace_ids")) or _unique_values(
            item.get("source_trace_id") for item in raw_chain if isinstance(item, dict)
        )
        rows.append(
            {
                "clue_id": clue.get("clue_id"),
                "clue_type": clue.get("clue_type"),
                "risk_category": clue.get("risk_category"),
                "evidence_trace_ids": evidence_trace_ids,
                "source_names": _string_list(clue.get("source_names")),
                "evidence_trace_count": int(clue.get("evidence_trace_count") or len(evidence_trace_ids)),
                "evidence_chain": [dict(item) for item in raw_chain if isinstance(item, dict)],
                "suggested_review_action": clue.get("suggested_review_action") or reviewability.get("suggested_review_action"),
                "review_action_reasons": [str(item) for item in (reviewability.get("review_action_reasons") or [])],
            }
        )
    return rows


def _demo_cost_latency(
    evidence: dict[str, Any],
    execution_summary: dict[str, Any],
    *,
    referenced_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    referenced_run = referenced_run or {}
    referenced_summary = (
        referenced_run.get("execution_summary")
        if isinstance(referenced_run.get("execution_summary"), dict)
        else {}
    )
    telemetry = (
        execution_summary.get("telemetry")
        if isinstance(execution_summary.get("telemetry"), dict)
        else referenced_summary.get("telemetry")
        if isinstance(referenced_summary.get("telemetry"), dict)
        else {}
    )
    budget_controller = telemetry.get("budget_controller") if isinstance(telemetry.get("budget_controller"), dict) else {}
    budget = (
        execution_summary.get("budget")
        if isinstance(execution_summary.get("budget"), dict)
        else referenced_summary.get("budget")
        if isinstance(referenced_summary.get("budget"), dict)
        else budget_controller.get("budget")
        if isinstance(budget_controller.get("budget"), dict)
        else {}
    )
    llm_cost = (
        execution_summary.get("llm_cost")
        if isinstance(execution_summary.get("llm_cost"), dict)
        else referenced_summary.get("llm_cost")
        if isinstance(referenced_summary.get("llm_cost"), dict)
        else budget_controller.get("llm_cost")
        if isinstance(budget_controller.get("llm_cost"), dict)
        else evidence.get("llm_cost")
        if isinstance(evidence.get("llm_cost"), dict)
        else {}
    )
    return {
        "elapsed_seconds": _optional_float(
            execution_summary.get("elapsed_seconds")
            or telemetry.get("elapsed_seconds")
            or budget_controller.get("elapsed_seconds")
            or evidence.get("elapsed_seconds")
            or referenced_run.get("elapsed_seconds")
            or referenced_summary.get("elapsed_seconds")
        ),
        "elapsed_ms": _optional_float(
            execution_summary.get("elapsed_ms")
            or telemetry.get("elapsed_ms")
            or budget_controller.get("elapsed_ms")
            or evidence.get("elapsed_ms")
            or referenced_run.get("elapsed_ms")
            or referenced_summary.get("elapsed_ms")
        ),
        "budget": budget,
        "llm_cost": llm_cost,
        "elapsed_budget_exhausted": bool(
            execution_summary.get("elapsed_budget_exhausted")
            or telemetry.get("elapsed_budget_exhausted")
            or budget_controller.get("elapsed_budget_exhausted")
            or evidence.get("elapsed_budget_exhausted")
            or referenced_summary.get("elapsed_budget_exhausted")
        ),
    }


def _referenced_run_artifact(evidence: dict[str, Any]) -> dict[str, Any]:
    run_path = evidence.get("run_artifact")
    if not str(run_path or "").strip():
        return {}
    return load_json(str(run_path))


def _evaluation_metrics(report: dict[str, Any]) -> dict[str, Any]:
    clue = report.get("clue") if isinstance(report.get("clue"), dict) else {}
    classification = report.get("classification") if isinstance(report.get("classification"), dict) else {}
    object_eval = clue.get("object_clue_eval") if isinstance(clue.get("object_clue_eval"), dict) else {}
    object_overall = object_eval.get("overall") if isinstance(object_eval.get("overall"), dict) else {}
    return {
        "primary_classification_f1": report.get("primary_classification_f1"),
        "secondary_classification_f1": report.get("secondary_classification_f1"),
        "hierarchical_classification_f1": report.get("hierarchical_classification_f1"),
        "classification_prediction_semantics": classification.get("prediction_semantics") or {},
        "false_positive_rate": report.get("false_positive_rate"),
        "classification_review_rate": report.get("classification_review_rate"),
        "entity_f1": report.get("entity_f1"),
        "clue_precision": report.get("clue_precision"),
        "clue_recall": report.get("clue_recall"),
        "clue_f1": report.get("clue_f1"),
        "expected_clue_count": clue.get("expected_clue_count"),
        "actual_clue_count": clue.get("actual_clue_count"),
        "duplicate_clue_rate": clue.get("duplicate_clue_rate"),
        "object_clue_f1": object_overall.get("f1"),
        "evidence_chain_precision": object_eval.get("evidence_chain_precision"),
        "evidence_chain_recall": object_eval.get("evidence_chain_recall"),
        "evidence_reviewability_rate": object_eval.get("evidence_reviewability_rate"),
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _unique_values(values: Iterable[Any]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rows.append(normalized)
    return rows


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
