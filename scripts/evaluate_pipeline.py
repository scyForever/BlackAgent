"""Evaluate deterministic pipeline quality/cost/latency on JSONL gold data."""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline import IntelligencePipeline
from src.domain import RunPolicyContext
from src.agent.budget_controller import BudgetController, RuntimeBudget
from src.backend import LLMGateway, LLMGatewayConfig
from src.evaluation.llm_ablation import LLMValueGate, write_latest_llm_value_report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate BlackAgent pipeline on gold JSONL records.")
    parser.add_argument("--gold", required=True, help="JSONL with content_text plus expected_risk_categories/expected_entities.")
    parser.add_argument("--entities-gold", default=None, help="Optional JSONL focused on entity extraction.")
    parser.add_argument("--clues-gold", default=None, help="Optional JSONL focused on clue aggregation.")
    parser.add_argument("--hard-negative", default=None, help="Optional JSONL where no risk prediction is expected.")
    parser.add_argument("--profile", default="high_recall", choices=["fast", "balanced", "high_recall"], help="Routing profile to evaluate.")
    parser.add_argument(
        "--classification-granularity",
        default="auto",
        choices=["auto", "primary_only", "hierarchical"],
        help=(
            "Classification metric granularity. auto scores hierarchical only when secondary gold labels exist; "
            "primary_only reports secondary/hierarchical as not_applicable."
        ),
    )
    parser.add_argument(
        "--llm-mode",
        default="off",
        choices=["off", "mock", "real"],
        help="LLM enrichment mode: off, deterministic mock, or real OpenAI-compatible gateway from env/config.",
    )
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="Run fast/off, high_recall/off, and high_recall/mock and report LLM marginal value.",
    )
    parser.add_argument(
        "--ablation-include-real",
        action="store_true",
        help="Also run high_recall/real in ablation; requires a configured real LLM gateway.",
    )
    parser.add_argument(
        "--write-latest-llm-value",
        default=None,
        help="When --ablation is used, also write runtime-facing latest LLM value JSON to this path.",
    )
    parser.add_argument("--with-budget", action="store_true", help="Attach BudgetController to the evaluation pipeline.")
    parser.add_argument("--min-classification-f1", type=float, default=None, help="Fail if classification F1 is below this threshold.")
    parser.add_argument("--min-primary-classification-f1", type=float, default=None, help="Fail if primary classification F1 is below this threshold.")
    parser.add_argument("--min-secondary-classification-f1", type=float, default=None, help="Fail if secondary classification F1 is below this threshold.")
    parser.add_argument("--min-hierarchical-classification-f1", type=float, default=None, help="Fail if hierarchical primary+secondary F1 is below this threshold.")
    parser.add_argument("--min-entity-f1", type=float, default=None, help="Fail if entity F1 is below this threshold.")
    parser.add_argument("--max-hard-negative-fpr", type=float, default=None, help="Fail if hard-negative false-positive rate is above this threshold.")
    parser.add_argument("--max-llm-calls-per-1000", type=float, default=None, help="Fail if profile LLM calls per 1000 records exceed this threshold.")
    parser.add_argument("--max-clue-overgeneration-ratio", type=float, default=None, help="Fail if actual clue count greatly exceeds expected count.")
    parser.add_argument("--max-review-load-per-100-records", type=float, default=None, help="Fail if actionable clue review load is above this threshold.")
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


def evaluate(
    records: list[dict[str, Any]],
    *,
    entity_records: list[dict[str, Any]] | None = None,
    clue_records: list[dict[str, Any]] | None = None,
    hard_negative_records: list[dict[str, Any]] | None = None,
    profile: str = "high_recall",
    llm_mode: str = "off",
    with_budget: bool = False,
    classification_granularity: str = "auto",
) -> dict[str, Any]:
    started = time.perf_counter()
    policy = RunPolicyContext.from_profile_config(routing_profile=profile, profile_config=_profile_config(profile), budget=_budget_profile(profile))
    gateway = _gateway_for_mode(llm_mode)
    budget_controller = BudgetController(RuntimeBudget.from_mapping(policy.budget or _budget_profile(profile))) if (with_budget or gateway is not None) else None
    pipeline = IntelligencePipeline(
        policy=policy,
        llm_gateway=gateway,
        budget_controller=budget_controller,
        load_runtime_llm_value=False,
    )
    classification_records = list(records)
    entity_eval_records = list(entity_records or records)
    hard_negative_records = list(hard_negative_records or [])
    clue_eval_records = list(clue_records or [])

    classification_result = pipeline.run(
        [*classification_records, *hard_negative_records],
        context={"quality_profile": profile, "require_evidence_chain": False, "policy": policy.model_dump()},
    )
    entity_result = pipeline.run(entity_eval_records, context={"quality_profile": profile, "require_evidence_chain": False, "policy": policy.model_dump()})
    clue_result = pipeline.run(clue_eval_records, context={"quality_profile": profile, "require_evidence_chain": False, "policy": policy.model_dump()}) if clue_eval_records else None
    elapsed_ms = (time.perf_counter() - started) * 1000

    classification_metrics = evaluate_classification(
        [*classification_records, *hard_negative_records],
        classification_result.classified,
        granularity=classification_granularity,
    )
    entity_metrics = evaluate_entities(entity_eval_records, entity_result.entities)
    clue_metrics = evaluate_clues(clue_eval_records, clue_result.clues if clue_result is not None else [])
    llm_budget = budget_controller.snapshot() if budget_controller is not None else {}
    llm_calls = int((llm_budget or {}).get("llm_calls") or classification_result.execution_summary.get("llm_enrich_trace_count") or 0)
    llm_calls_per_1000 = round(llm_calls / max(len(classification_records) + len(hard_negative_records), 1) * 1000, 4)
    valid_clues = max(1, clue_metrics["overall"]["tp"])
    pipeline_summary = classification_result.execution_summary.model_dump()
    report = {
        "status": "completed",
        "profile": profile,
        "record_count": len(classification_records),
        "classification_record_count": len(classification_records),
        "entity_record_count": len(entity_eval_records),
        "clue_record_count": len(clue_eval_records),
        "hard_negative_record_count": len(hard_negative_records),
        "classification": classification_metrics,
        "entity": entity_metrics,
        "clue": clue_metrics,
        "classification_precision": classification_metrics["primary"]["precision"],
        "classification_recall": classification_metrics["primary"]["recall"],
        "classification_f1": classification_metrics["primary"]["f1"],
        "primary_classification_f1": classification_metrics["primary"]["f1"],
        "secondary_classification_f1": classification_metrics["secondary"]["f1"],
        "hierarchical_classification_f1": classification_metrics["hierarchical"]["f1"],
        "classification_granularity": classification_metrics["granularity"],
        "secondary_classification_status": classification_metrics["secondary"]["status"],
        "hierarchical_classification_status": classification_metrics["hierarchical"]["status"],
        "secondary_label_policy": (
            "formal_metric"
            if classification_metrics["secondary_gold"]["ready"]
            else "assistive_field_not_formal_metric"
        ),
        "entity_precision": entity_metrics["overall"]["precision"],
        "entity_recall": entity_metrics["overall"]["recall"],
        "entity_f1": entity_metrics["overall"]["f1"],
        "clue_precision": clue_metrics["overall"]["precision"],
        "clue_recall": clue_metrics["overall"]["recall"],
        "clue_f1": clue_metrics["overall"]["f1"],
        "high_risk_recall": classification_metrics["primary"]["recall"],
        "false_positive_rate": classification_metrics["false_positive_rate"],
        "hard_negative": classification_metrics["hard_negative"],
        "llm_calls_per_1000_records": llm_calls_per_1000,
        "estimated_tokens_per_valid_clue": round(float((llm_budget or {}).get("estimated_tokens") or classification_result.execution_summary.get("estimated_tokens") or 0.0) / valid_clues, 4),
        "llm_mode": llm_mode,
        "budget_controller": llm_budget,
        "p50_latency_ms": round(elapsed_ms, 2),
        "p95_latency_ms": round(elapsed_ms, 2),
        "pipeline_summary": pipeline_summary,
        "rule_version": pipeline_summary.get("rule_version"),
        "profile_comparison_dimensions": {
            "llm_calls": llm_calls,
            "llm_calls_per_1000_records": llm_calls_per_1000,
            "classification_recall": classification_metrics["overall"]["recall"],
            "primary_classification_recall": classification_metrics["primary"]["recall"],
            "false_positive_rate": classification_metrics["false_positive_rate"],
            "p95_latency_ms": round(elapsed_ms, 2),
            "estimated_tokens": int((llm_budget or {}).get("estimated_tokens") or 0),
        },
    }
    return report


def evaluate_difficult_sets(
    set_paths: Iterable[str | Path] | None = None,
    *,
    profile: str = "high_recall",
    llm_mode: str = "off",
    with_budget: bool = False,
) -> dict[str, Any]:
    """Evaluate named hard subsets so LLM value can be measured by scenario."""

    paths = list(set_paths or _default_difficult_set_paths())
    subsets: dict[str, Any] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            subsets[path.stem] = {"status": "missing", "path": str(path)}
            continue
        records = load_jsonl(path)
        subsets[path.stem] = evaluate(
            records,
            profile=profile,
            llm_mode=llm_mode,
            with_budget=with_budget,
            classification_granularity="auto",
        )
    return {
        "status": "completed",
        "profile": profile,
        "llm_mode": llm_mode,
        "subset_count": len(subsets),
        "subsets": subsets,
    }


def quality_gate_failures(report: Mapping[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    if args.min_classification_f1 is not None and float(report["classification_f1"]) < args.min_classification_f1:
        failures.append(f"classification_f1_below_threshold:{report['classification_f1']}<{args.min_classification_f1}")
    if getattr(args, "min_primary_classification_f1", None) is not None and float(report["primary_classification_f1"]) < args.min_primary_classification_f1:
        failures.append(f"primary_classification_f1_below_threshold:{report['primary_classification_f1']}<{args.min_primary_classification_f1}")
    if getattr(args, "min_secondary_classification_f1", None) is not None:
        if report.get("secondary_classification_f1") is None:
            failures.append("secondary_classification_f1_not_applicable:secondary_gold_required")
        elif float(report["secondary_classification_f1"]) < args.min_secondary_classification_f1:
            failures.append(f"secondary_classification_f1_below_threshold:{report['secondary_classification_f1']}<{args.min_secondary_classification_f1}")
    if getattr(args, "min_hierarchical_classification_f1", None) is not None:
        if report.get("hierarchical_classification_f1") is None:
            failures.append("hierarchical_classification_f1_not_applicable:secondary_gold_required")
        elif float(report["hierarchical_classification_f1"]) < args.min_hierarchical_classification_f1:
            failures.append(f"hierarchical_classification_f1_below_threshold:{report['hierarchical_classification_f1']}<{args.min_hierarchical_classification_f1}")
    if args.min_entity_f1 is not None and float(report["entity_f1"]) < args.min_entity_f1:
        failures.append(f"entity_f1_below_threshold:{report['entity_f1']}<{args.min_entity_f1}")
    if args.max_hard_negative_fpr is not None and float(report["false_positive_rate"]) > args.max_hard_negative_fpr:
        failures.append(f"hard_negative_fpr_above_threshold:{report['false_positive_rate']}>{args.max_hard_negative_fpr}")
    if args.max_llm_calls_per_1000 is not None and float(report["llm_calls_per_1000_records"]) > args.max_llm_calls_per_1000:
        failures.append(f"llm_calls_per_1000_above_threshold:{report['llm_calls_per_1000_records']}>{args.max_llm_calls_per_1000}")
    if getattr(args, "max_clue_overgeneration_ratio", None) is not None and float(report["clue"]["clue_overgeneration_ratio"]) > args.max_clue_overgeneration_ratio:
        failures.append(f"clue_overgeneration_ratio_above_threshold:{report['clue']['clue_overgeneration_ratio']}>{args.max_clue_overgeneration_ratio}")
    if getattr(args, "max_review_load_per_100_records", None) is not None and float(report["clue"]["review_load_per_100_records"]) > args.max_review_load_per_100_records:
        failures.append(f"review_load_per_100_records_above_threshold:{report['clue']['review_load_per_100_records']}>{args.max_review_load_per_100_records}")
    return failures


def evaluate_classification(
    records: list[dict[str, Any]],
    actual_items: Iterable[Mapping[str, Any]],
    *,
    granularity: str = "auto",
) -> dict[str, Any]:
    primary_tp = primary_fp = primary_fn = 0
    secondary_tp = secondary_fp = secondary_fn = 0
    hierarchical_tp = hierarchical_fp = hierarchical_fn = 0
    actual_by_trace = {
        str(item.get("source_trace_id") or ""): item
        for item in actual_items
    }
    hard_tn = hard_fp = 0
    annotated_secondary_records = sum(1 for record in records if _has_secondary_gold_annotation(record))
    expected_secondary_gold_count = sum(len(expected_secondary_labels(record)) for record in records)
    requested_granularity = str(granularity or "auto").strip().lower()
    if requested_granularity == "auto":
        resolved_granularity = "hierarchical" if expected_secondary_gold_count > 0 else "primary_only"
    elif requested_granularity in {"primary", "primary_only"}:
        resolved_granularity = "primary_only"
    else:
        resolved_granularity = "hierarchical"
    hierarchical_gold_ready = resolved_granularity == "hierarchical" and expected_secondary_gold_count > 0
    for record in records:
        trace_id = _trace_id(record)
        expected_primary = predicted_categories(record)
        expected_secondary = expected_secondary_labels(record)
        actual = actual_by_trace.get(trace_id, {})
        actual_primary = predicted_categories(actual)
        actual_secondary = actual_secondary_labels(actual)
        primary_tp += len(expected_primary & actual_primary)
        primary_fp += len(actual_primary - expected_primary)
        primary_fn += len(expected_primary - actual_primary)
        if hierarchical_gold_ready:
            secondary_tp += len(expected_secondary & actual_secondary)
            secondary_fp += len(actual_secondary - expected_secondary)
            secondary_fn += len(expected_secondary - actual_secondary)
            expected_pairs = _classification_pairs(expected_primary, expected_secondary)
            actual_pairs = _classification_pairs(actual_primary, actual_secondary)
            hierarchical_tp += len(expected_pairs & actual_pairs)
            hierarchical_fp += len(actual_pairs - expected_pairs)
            hierarchical_fn += len(expected_pairs - actual_pairs)
        if not expected_primary:
            if actual_primary:
                hard_fp += 1
            else:
                hard_tn += 1
    primary = prf(primary_tp, primary_fp, primary_fn)
    if hierarchical_gold_ready:
        secondary = {
            **prf(secondary_tp, secondary_fp, secondary_fn),
            "status": "completed",
            "metric_note": "secondary_gold_present",
        }
        hierarchical = {
            **prf(hierarchical_tp, hierarchical_fp, hierarchical_fn),
            "status": "completed",
            "metric_note": "primary_secondary_pair_f1",
        }
        overall = {**hierarchical, "metric_note": "hierarchical_primary_secondary_f1"}
    else:
        secondary_status = "missing_secondary_gold" if resolved_granularity == "hierarchical" else "not_applicable"
        secondary = {
            "precision": None,
            "recall": None,
            "f1": None,
            "status": secondary_status,
            "metric_note": "secondary_gold_labels_required_for_secondary_metrics",
        }
        hierarchical = {
            "precision": None,
            "recall": None,
            "f1": None,
            "status": secondary_status,
            "metric_note": "hierarchical_gold_labels_required_for_hierarchical_metrics",
        }
        overall = {**primary, "metric_note": "primary_only_f1"}
    return {
        "overall": overall,
        "primary": {**primary, "tp": primary_tp, "fp": primary_fp, "fn": primary_fn},
        "secondary": {**secondary, "tp": secondary_tp, "fp": secondary_fp, "fn": secondary_fn},
        "hierarchical": {**hierarchical, "tp": hierarchical_tp, "fp": hierarchical_fp, "fn": hierarchical_fn},
        "granularity": resolved_granularity,
        "secondary_gold": {
            "annotated_record_count": annotated_secondary_records,
            "expected_label_count": expected_secondary_gold_count,
            "ready": hierarchical_gold_ready,
        },
        "false_positive_rate": round(hard_fp / max(hard_fp + hard_tn, 1), 4),
        "hard_negative": {"tn": hard_tn, "fp": hard_fp},
    }


def evaluate_entities(records: list[dict[str, Any]], actual_entities: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    entity_tp = entity_fp = entity_fn = 0
    entities_by_trace: dict[str, set[tuple[str, str]]] = {}
    for entity in actual_entities:
        trace_id = str(entity.get("source_trace_id") or "")
        normalized = normalize_entity(entity)
        if trace_id and normalized:
            entities_by_trace.setdefault(trace_id, set()).add(normalized)

    for record in records:
        trace_id = _trace_id(record)
        expected_entities = expected_entity_set(record)
        actual_entities = entities_by_trace.get(trace_id, set())
        tp, fp, fn = entity_confusion(expected_entities, actual_entities)
        entity_tp += tp
        entity_fp += fp
        entity_fn += fn
    overall = prf(entity_tp, entity_fp, entity_fn)
    return {"overall": {**overall, "tp": entity_tp, "fp": entity_fp, "fn": entity_fn}}


def evaluate_clues(records: list[dict[str, Any]], actual_clues: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    expected_count_values = [int(record.get("expected_clue_count") or 0) for record in records if record.get("expected_clue_count") is not None]
    expected_count = max(expected_count_values) if expected_count_values else sum(len(record.get("expected_clues") or []) for record in records)
    expected_types = {str(item).strip() for record in records for item in (record.get("expected_clue_types") or []) if str(item).strip()}
    actual = [dict(item) for item in actual_clues]
    actual_types = {str(item.get("clue_type") or "").strip() for item in actual if str(item.get("clue_type") or "").strip()}
    if expected_types:
        type_tp = len(expected_types & actual_types)
        type_fp = len(actual_types - expected_types)
        type_fn = len(expected_types - actual_types)
        count_fp = max(0, len(actual) - max(expected_count, type_tp))
        count_fn = max(0, expected_count - len(actual))
        tp = min(type_tp, expected_count or type_tp)
        fp = type_fp + count_fp
        fn = type_fn + count_fn
    else:
        tp = min(len(actual), expected_count)
        fp = max(0, len(actual) - expected_count)
        fn = max(0, expected_count - len(actual))
    overall = prf(tp, fp, fn)
    overgeneration_ratio = round(len(actual) / max(expected_count, 1), 4)
    duplicate_rate = _duplicate_clue_rate(actual)
    return {
        "overall": {**overall, "tp": tp, "fp": fp, "fn": fn},
        "expected_clue_count": expected_count,
        "actual_clue_count": len(actual),
        "clue_overgeneration_ratio": overgeneration_ratio,
        "valid_clue_precision_by_count": round(min(expected_count, len(actual)) / max(len(actual), 1), 4),
        "review_load_per_100_records": round(len(actual) / max(len(records), 1) * 100, 4),
        "duplicate_clue_rate": duplicate_rate,
        "actual_clue_types": sorted(actual_types),
    }


def predicted_categories(classification: Mapping[str, Any]) -> set[str]:
    risk = _normalize_label(classification.get("risk_category") or classification.get("expected_primary_risk"))
    expected = classification.get("expected_risk_categories")
    categories = {_normalize_label(item) for item in expected} if isinstance(expected, list) else set()
    categories.update({_normalize_label(item) for item in classification.get("expected_primary_risks", [])} if isinstance(classification.get("expected_primary_risks"), list) else set())
    if risk:
        categories.add(risk)
    return {item for item in categories if item not in {"unknown", "normal_noise", "正常业务白噪声", "待研判", "无风险", "none"}}


def actual_secondary_labels(classification: Mapping[str, Any]) -> set[str]:
    labels = set()
    secondary = _normalize_label(classification.get("secondary_label"))
    if secondary:
        labels.add(secondary)
    return {item for item in labels if item not in {"待研判", "未细分", "unknown", "none", "null"}}


def expected_secondary_labels(classification: Mapping[str, Any]) -> set[str]:
    labels = set()
    secondary = _normalize_label(classification.get("expected_secondary_risk"))
    if secondary:
        labels.add(secondary)
    raw = classification.get("expected_secondary_risks") or classification.get("expected_secondary_labels")
    if isinstance(raw, list):
        labels.update(_normalize_label(item) for item in raw)
    return {item for item in labels if item not in {"待研判", "未细分", "unknown", "none", "null"}}


def predicted_secondary_labels(classification: Mapping[str, Any]) -> set[str]:
    """Backward-compatible secondary-label reader.

    Evaluation now separates actual predictions from expected gold labels so
    model secondary output is not counted as false positive when the gold file
    intentionally has no secondary annotations.
    """

    return actual_secondary_labels(classification) or expected_secondary_labels(classification)


def _has_secondary_gold_annotation(record: Mapping[str, Any]) -> bool:
    if record.get("expected_secondary_risk") not in (None, ""):
        return True
    for field in ("expected_secondary_risks", "expected_secondary_labels"):
        value = record.get(field)
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True
    return False


def expected_entity_set(record: Mapping[str, Any]) -> set[tuple[str, str]]:
    values = record.get("expected_entities") or []
    output: set[tuple[str, str]] = set()
    for item in values:
        if isinstance(item, Mapping):
            normalized = normalize_entity(item)
        else:
            normalized = ("*", _normalize_entity_value(item))
        if normalized and normalized[1]:
            output.add(normalized)
    return output


def normalize_entity(entity: Mapping[str, Any]) -> tuple[str, str]:
    entity_type = str(entity.get("entity_type") or entity.get("type") or "*").strip().lower() or "*"
    value = _normalize_entity_value(entity.get("normalized_value") or entity.get("entity_value") or entity.get("value"))
    return (entity_type, value) if value else ("", "")


def entity_matches(expected: tuple[str, str], actual: tuple[str, str]) -> bool:
    expected_type, expected_value = expected
    actual_type, actual_value = actual
    if expected_type not in {"", "*"} and expected_type != actual_type:
        return False
    if expected_value == actual_value:
        return True
    if expected_value and actual_value and (expected_value in actual_value or actual_value in expected_value):
        return True
    return False


def entity_confusion(expected_entities: set[tuple[str, str]], actual_entities: set[tuple[str, str]]) -> tuple[int, int, int]:
    unmatched_actual = set(actual_entities)
    tp = 0
    for expected in expected_entities:
        match = next((actual for actual in unmatched_actual if entity_matches(expected, actual)), None)
        if match is not None:
            tp += 1
            unmatched_actual.remove(match)
    fp = len(unmatched_actual)
    fn = max(0, len(expected_entities) - tp)
    return tp, fp, fn


def _normalize_label(value: Any) -> str:
    return str(value or "").strip()


def _normalize_entity_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text.replace("telegram:", "tg:").replace("https://", "").replace("http://", "").strip(" /")


def _trace_id(record: Mapping[str, Any]) -> str:
    return str(record.get("source_trace_id") or record.get("trace_id") or record.get("hash_id") or "")


def _default_difficult_set_paths() -> list[Path]:
    return [
        PROJECT_ROOT / "tests/evaluation/hard_slang_ambiguous.jsonl",
        PROJECT_ROOT / "tests/evaluation/context_conflict.jsonl",
        PROJECT_ROOT / "tests/evaluation/low_evidence_high_risk.jsonl",
        PROJECT_ROOT / "tests/evaluation/cross_source_entity_graph.jsonl",
        PROJECT_ROOT / "tests/evaluation/llm_required_cases.jsonl",
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    loaded = {
        "records": load_jsonl(args.gold),
        "entity_records": load_jsonl(args.entities_gold) if args.entities_gold else None,
        "clue_records": load_jsonl(args.clues_gold) if args.clues_gold else None,
        "hard_negative_records": load_jsonl(args.hard_negative) if args.hard_negative else None,
    }
    report = (
        evaluate_ablation(**loaded, with_budget=True, include_real=args.ablation_include_real)
        if args.ablation
        else evaluate(
            loaded["records"],
            entity_records=loaded["entity_records"],
            clue_records=loaded["clue_records"],
            hard_negative_records=loaded["hard_negative_records"],
            profile=args.profile,
            llm_mode=args.llm_mode,
            with_budget=args.with_budget,
            classification_granularity=args.classification_granularity,
        )
    )
    if args.ablation and args.write_latest_llm_value:
        report["latest_llm_value"] = write_latest_llm_value_report(
            report,
            output_path=args.write_latest_llm_value,
            profile="high_recall",
        )
    failures = quality_gate_failures(report, args)
    if failures:
        report["status"] = "failed_quality_gate"
        report["quality_gate_failures"] = failures
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _profile_config(profile: str) -> dict[str, Any]:
    defaults = {
        "fast": {
            "enable_llm_intent_parse": False,
            "enable_query_rewrite": False,
            "enable_live_collection": False,
            "enable_llm_record_enrich": False,
            "enable_llm_clue_refine": True,
        },
        "balanced": {
            "enable_llm_intent_parse": True,
            "enable_query_rewrite": True,
            "enable_live_collection": True,
            "enable_llm_record_enrich": True,
            "enable_llm_clue_refine": True,
        },
        "high_recall": {
            "enable_llm_intent_parse": True,
            "enable_query_rewrite": True,
            "enable_live_collection": True,
            "enable_llm_record_enrich": True,
            "enable_llm_clue_refine": True,
        },
    }
    return defaults.get(profile, defaults["balanced"])


def evaluate_ablation(
    records: list[dict[str, Any]],
    *,
    entity_records: list[dict[str, Any]] | None = None,
    clue_records: list[dict[str, Any]] | None = None,
    hard_negative_records: list[dict[str, Any]] | None = None,
    with_budget: bool = True,
    include_real: bool = False,
) -> dict[str, Any]:
    scenarios = {
        "fast_off": evaluate(
            deepcopy(records),
            entity_records=deepcopy(entity_records),
            clue_records=deepcopy(clue_records),
            hard_negative_records=deepcopy(hard_negative_records),
            profile="fast",
            llm_mode="off",
            with_budget=with_budget,
        ),
        "high_recall_off": evaluate(
            deepcopy(records),
            entity_records=deepcopy(entity_records),
            clue_records=deepcopy(clue_records),
            hard_negative_records=deepcopy(hard_negative_records),
            profile="high_recall",
            llm_mode="off",
            with_budget=with_budget,
        ),
        "high_recall_mock": evaluate(
            deepcopy(records),
            entity_records=deepcopy(entity_records),
            clue_records=deepcopy(clue_records),
            hard_negative_records=deepcopy(hard_negative_records),
            profile="high_recall",
            llm_mode="mock",
            with_budget=with_budget,
        ),
    }
    base = scenarios["high_recall_off"]
    llm = scenarios["high_recall_mock"]
    comparison = _llm_value_delta(base, llm)
    if include_real:
        scenarios["high_recall_real"] = evaluate(
            deepcopy(records),
            entity_records=deepcopy(entity_records),
            clue_records=deepcopy(clue_records),
            hard_negative_records=deepcopy(hard_negative_records),
            profile="high_recall",
            llm_mode="real",
            with_budget=with_budget,
        )
        comparison["real"] = _llm_value_delta(base, scenarios["high_recall_real"])
    value_gate = LLMValueGate()
    return {
        "status": "completed",
        "mode": "llm_ablation",
        "scenarios": scenarios,
        "llm_value": comparison,
        "llm_value_gate": {
            "should_enable_record_enrich": value_gate.should_enable_record_enrich("high_recall", comparison),
            "reason": comparison["gate_reason"],
        },
    }


def _gateway_for_mode(llm_mode: str) -> LLMGateway | None:
    if llm_mode == "mock":
        return LLMGateway(LLMGatewayConfig(dry_run=True, mock=True))
    if llm_mode == "real":
        return LLMGateway(LLMGatewayConfig.from_env())
    return None


def _budget_profile(profile: str) -> dict[str, Any]:
    return {
        "fast": {
            "max_candidate_clues": 4,
            "max_llm_calls": 2,
            "max_llm_tokens": 4000,
            "max_llm_classify_records": 2,
            "max_llm_refine_clues": 1,
        },
        "balanced": {
            "max_candidate_clues": 6,
            "max_llm_calls": 20,
            "max_llm_tokens": 20000,
            "max_llm_classify_records": 20,
            "max_llm_refine_clues": 10,
        },
        "high_recall": {
            "max_candidate_clues": 6,
            "max_llm_calls": 40,
            "max_llm_tokens": 40000,
            "max_llm_classify_records": 40,
            "max_llm_refine_clues": 20,
        },
    }.get(profile, {})


def _llm_value_delta(base: Mapping[str, Any], llm: Mapping[str, Any]) -> dict[str, Any]:
    classification_delta = round(float(llm["primary_classification_f1"]) - float(base["primary_classification_f1"]), 4)
    entity_delta = round(float(llm["entity_f1"]) - float(base["entity_f1"]), 4)
    hard_negative_delta = round(float(llm["false_positive_rate"]) - float(base["false_positive_rate"]), 4)
    clue_precision_delta = round(float(llm["clue_f1"]) - float(base["clue_f1"]), 4)
    clue_recall_delta = round(float(llm["clue_recall"]) - float(base["clue_recall"]), 4)
    llm_calls_delta = round(float(llm["llm_calls_per_1000_records"]) - float(base["llm_calls_per_1000_records"]), 4)
    token_delta = float(llm.get("profile_comparison_dimensions", {}).get("estimated_tokens") or 0) - float(base.get("profile_comparison_dimensions", {}).get("estimated_tokens") or 0)
    f1_gain = max(classification_delta, entity_delta, clue_precision_delta, 0.0)
    extra_valid_clues = max(float(llm.get("clue", {}).get("overall", {}).get("tp") or 0) - float(base.get("clue", {}).get("overall", {}).get("tp") or 0), 0.0)
    if f1_gain <= 0.0 and extra_valid_clues <= 0.0:
        gate_reason = "llm_added_cost_without_measured_quality_gain"
    else:
        gate_reason = "llm_measured_positive_marginal_gain"
    return {
        "classification_f1_delta": classification_delta,
        "entity_f1_delta": entity_delta,
        "hard_negative_fpr_delta": hard_negative_delta,
        "clue_precision_delta": clue_precision_delta,
        "clue_recall_delta": clue_recall_delta,
        "llm_calls_delta": llm_calls_delta,
        "tokens_per_f1_gain": None if f1_gain <= 0 else round(token_delta / f1_gain, 4),
        "tokens_per_extra_valid_clue": None if extra_valid_clues <= 0 else round(token_delta / extra_valid_clues, 4),
        "gate_reason": gate_reason,
    }


def _classification_pairs(primary: set[str], secondary: set[str]) -> set[tuple[str, str]]:
    if not primary:
        return set()
    if not secondary:
        return {(item, "*") for item in primary}
    return {(item, label) for item in primary for label in secondary}


def _duplicate_clue_rate(actual: list[dict[str, Any]]) -> float:
    if not actual:
        return 0.0
    keys = [
        (
            str(item.get("clue_type") or ""),
            str(item.get("key") or ""),
            str(item.get("risk_category") or ""),
        )
        for item in actual
    ]
    return round(1.0 - (len(set(keys)) / len(keys)), 4)


if __name__ == "__main__":
    raise SystemExit(main())
