"""Evaluate deterministic pipeline quality/cost/latency on JSONL gold data."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


def configure_stdout_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


configure_stdout_utf8()


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline import IntelligencePipeline
from src.domain import RunPolicyContext
from src.agent.budget_controller import BudgetController, RuntimeBudget
from src.backend import LLMGateway, LLMGatewayConfig
from src.classifier.nlp_rule_matcher import review_bucket_for_classification
from src.evaluation.llm_ablation import LLMValueGate, write_latest_llm_value_report


NON_RISK_SECONDARY_LABELS = {"低相关", "防御语境", "研究讨论", "正常业务白噪声", "待研判"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate BlackAgent pipeline on gold JSONL records.")
    parser.add_argument("--gold", required=True, help="JSONL with content_text plus expected_risk_categories/expected_entities.")
    parser.add_argument("--entities-gold", default=None, help="Optional JSONL focused on entity extraction.")
    parser.add_argument("--clues-gold", default=None, help="Optional JSONL focused on clue aggregation.")
    parser.add_argument("--hard-negative", default=None, help="Optional JSONL where no risk prediction is expected.")
    parser.add_argument("--dataset-name", default=None, help="Human-readable dataset name written to the report.")
    parser.add_argument(
        "--dataset-kind",
        default=None,
        help="Dataset split/kind such as synthetic_gold, heldout_public_authorized, or smoke.",
    )
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
        "--profile-curve",
        action="store_true",
        help="Run fast, balanced, and high_recall and report quality/cost/latency tradeoff curves.",
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
    parser.add_argument("--min-clue-recall", type=float, default=None, help="Fail if clue recall is below this threshold.")
    parser.add_argument("--min-object-clue-recall", type=float, default=None, help="Fail if object-level clue recall is below this threshold.")
    parser.add_argument("--max-clue-overgeneration-ratio", type=float, default=None, help="Fail if actual clue count greatly exceeds expected count.")
    parser.add_argument("--max-review-load-per-100-records", type=float, default=None, help="Fail if actionable clue review load is above this threshold.")
    parser.add_argument("--max-classification-review-rate", type=float, default=None, help="Fail if classification review_required rate is above this threshold.")
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
    dataset_name: str | None = None,
    dataset_kind: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    classification_records = list(records)
    entity_eval_records = list(entity_records or records)
    hard_negative_records = list(hard_negative_records or [])
    clue_eval_records = list(clue_records or [])
    profile_config = _profile_config(profile)
    if _contains_graph_clue_gold([*classification_records, *entity_eval_records, *clue_eval_records]):
        profile_config["enable_graph_clue_generation"] = True
    policy = RunPolicyContext.from_profile_config(routing_profile=profile, profile_config=profile_config, budget=_budget_profile(profile))
    gateway = _gateway_for_mode(llm_mode)
    budget_controller = BudgetController(RuntimeBudget.from_mapping(policy.budget or _budget_profile(profile))) if (with_budget or gateway is not None) else None
    pipeline = IntelligencePipeline(
        policy=policy,
        llm_gateway=gateway,
        budget_controller=budget_controller,
        load_runtime_llm_value=False,
    )

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
        "dataset": dataset_profile(
            [*classification_records, *hard_negative_records],
            explicit_name=dataset_name,
            explicit_kind=dataset_kind,
        ),
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
        "classification_review_load": classification_metrics["review_load"],
        "classification_review_rate": classification_metrics["review_load"]["review_rate"],
        "classification_review_load_per_100_records": classification_metrics["review_load"]["review_load_per_100_records"],
        "hard_negative": classification_metrics["hard_negative"],
        "llm_calls_per_1000_records": llm_calls_per_1000,
        "runtime_value_gate_applied": False,
        "llm_value_gate_mode": (
            "disabled_for_offline_evaluation"
            if llm_mode == "off"
            else "disabled_for_ablation_measurement"
        ),
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
    if args.min_classification_f1 is not None:
        if report.get("classification_f1") is None:
            failures.append("classification_f1_not_applicable:positive_gold_required")
        elif float(report["classification_f1"]) < args.min_classification_f1:
            failures.append(f"classification_f1_below_threshold:{report['classification_f1']}<{args.min_classification_f1}")
    if getattr(args, "min_primary_classification_f1", None) is not None:
        if report.get("primary_classification_f1") is None:
            failures.append("primary_classification_f1_not_applicable:positive_gold_required")
        elif float(report["primary_classification_f1"]) < args.min_primary_classification_f1:
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
    if getattr(args, "min_clue_recall", None) is not None:
        recall = ((report.get("clue") or {}).get("overall") or {}).get("recall")
        if recall is None:
            failures.append("clue_recall_not_applicable:clue_gold_required")
        elif float(recall) < args.min_clue_recall:
            failures.append(f"clue_recall_below_threshold:{recall}<{args.min_clue_recall}")
    if getattr(args, "min_object_clue_recall", None) is not None:
        recall = ((((report.get("clue") or {}).get("object_clue_eval") or {}).get("overall") or {}).get("recall"))
        if recall is None:
            failures.append("object_clue_recall_not_applicable:object_clue_gold_required")
        elif float(recall) < args.min_object_clue_recall:
            failures.append(f"object_clue_recall_below_threshold:{recall}<{args.min_object_clue_recall}")
    if getattr(args, "max_clue_overgeneration_ratio", None) is not None and float(report["clue"]["clue_overgeneration_ratio"]) > args.max_clue_overgeneration_ratio:
        failures.append(f"clue_overgeneration_ratio_above_threshold:{report['clue']['clue_overgeneration_ratio']}>{args.max_clue_overgeneration_ratio}")
    if getattr(args, "max_review_load_per_100_records", None) is not None and float(report["clue"]["review_load_per_100_records"]) > args.max_review_load_per_100_records:
        failures.append(f"review_load_per_100_records_above_threshold:{report['clue']['review_load_per_100_records']}>{args.max_review_load_per_100_records}")
    if getattr(args, "max_classification_review_rate", None) is not None:
        review_rate = report.get("classification_review_rate")
        if review_rate is None:
            failures.append("classification_review_rate_not_applicable:non_standard_report")
        elif float(review_rate) > args.max_classification_review_rate:
            failures.append(f"classification_review_rate_above_threshold:{review_rate}>{args.max_classification_review_rate}")
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
    primary_confusion: defaultdict[str, Counter[str]] = defaultdict(Counter)
    secondary_confusion: defaultdict[str, Counter[str]] = defaultdict(Counter)
    hierarchical_confusion: defaultdict[str, Counter[str]] = defaultdict(Counter)
    actual_item_list = [dict(item) for item in actual_items]
    actual_by_trace = {
        str(item.get("source_trace_id") or ""): item
        for item in actual_item_list
    }
    hard_tn = hard_fp = 0
    positive_record_count = 0
    negative_record_count = 0
    annotated_secondary_records = sum(1 for record in records if _has_secondary_gold_annotation(record))
    expected_secondary_gold_count = sum(len(_formal_expected_secondary_labels(record)) for record in records)
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
        expected_secondary = _formal_expected_secondary_labels(record)
        actual = actual_by_trace.get(trace_id, {})
        actual_primary = predicted_categories(actual)
        actual_secondary = actual_secondary_labels(actual)
        if not expected_primary:
            actual_secondary = {
                label
                for label in actual_secondary
                if label not in NON_RISK_SECONDARY_LABELS
            }
        if expected_primary:
            positive_record_count += 1
            primary_tp += len(expected_primary & actual_primary)
            primary_fp += len(actual_primary - expected_primary)
            primary_fn += len(expected_primary - actual_primary)
        else:
            negative_record_count += 1
        _update_confusion(primary_confusion, expected_primary, actual_primary)
        if hierarchical_gold_ready:
            secondary_tp += len(expected_secondary & actual_secondary)
            secondary_fp += len(actual_secondary - expected_secondary)
            secondary_fn += len(expected_secondary - actual_secondary)
            expected_pairs = _classification_pairs(expected_primary, expected_secondary)
            actual_pairs = _classification_pairs(actual_primary, actual_secondary)
            hierarchical_tp += len(expected_pairs & actual_pairs)
            hierarchical_fp += len(actual_pairs - expected_pairs)
            hierarchical_fn += len(expected_pairs - actual_pairs)
            _update_confusion(secondary_confusion, expected_secondary, actual_secondary)
            _update_confusion(
                hierarchical_confusion,
                {_pair_label(*pair) for pair in expected_pairs},
                {_pair_label(*pair) for pair in actual_pairs},
            )
        if not expected_primary:
            if actual_primary:
                hard_fp += 1
            else:
                hard_tn += 1
    primary = prf(primary_tp, primary_fp, primary_fn) if positive_record_count else {
        "precision": None,
        "recall": None,
        "f1": None,
    }
    primary_status = "completed" if positive_record_count else "no_positive_gold"
    evaluation_mode = (
        "hard_negative"
        if positive_record_count == 0 and negative_record_count > 0
        else "mixed_gold"
        if positive_record_count > 0 and negative_record_count > 0
        else "classification_gold"
    )
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
        overall = {
            **primary,
            "metric_note": "primary_only_f1" if positive_record_count else "hard_negative_only_tn_fp",
            "status": primary_status,
        }
    return {
        "overall": overall,
        "primary": {**primary, "tp": primary_tp, "fp": primary_fp, "fn": primary_fn, "status": primary_status},
        "secondary": {**secondary, "tp": secondary_tp, "fp": secondary_fp, "fn": secondary_fn},
        "hierarchical": {**hierarchical, "tp": hierarchical_tp, "fp": hierarchical_fp, "fn": hierarchical_fn},
        "confusion_analysis": {
            "status": "completed",
            "primary": _confusion_payload(primary_confusion),
            "secondary": _confusion_payload(secondary_confusion) if hierarchical_gold_ready else {},
            "hierarchical": _confusion_payload(hierarchical_confusion) if hierarchical_gold_ready else {},
            "metric_note": (
                "primary_secondary_confusion_from_gold"
                if hierarchical_gold_ready
                else "secondary_hierarchical_confusion_requires_secondary_gold"
            ),
        },
        "typical_errors": _classification_error_examples(
            records,
            actual_by_trace,
            include_secondary=hierarchical_gold_ready,
            limit=10,
        ),
        "review_load": _classification_review_load(actual_item_list, record_count=len(records)),
        "granularity": resolved_granularity,
        "evaluation_mode": evaluation_mode,
        "prediction_semantics": {
            "metric_scope": "review_augmented_predictions",
            "primary_predictions": "risk_category_plus_conflict_categories",
            "secondary_predictions": "secondary_label_plus_evidence_backed_conflict_candidate_secondary_labels",
            "conflict_categories_counted_as_predictions": True,
            "candidate_secondary_requires_evidence": True,
            "note": (
                "Classification precision/recall counts conflict alternatives as review predictions "
                "so overlap cases measure whether the candidate set preserves gold labels."
            ),
        },
        "positive_record_count": positive_record_count,
        "negative_record_count": negative_record_count,
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
    actual = [dict(item) for item in actual_clues]
    overall_eval = _evaluate_clue_layer(records, actual, layer="overall")
    standard_eval = _evaluate_clue_layer(records, actual, layer="standard")
    graph_eval = _evaluate_clue_layer(records, actual, layer="graph")
    object_eval = _evaluate_clue_objects(records, actual)
    review_load_eval = {
        "clue_overgeneration_ratio": overall_eval["clue_overgeneration_ratio"],
        "review_load_per_100_records": overall_eval["review_load_per_100_records"],
        "standard_review_load_per_100_records": standard_eval["review_load_per_100_records"],
        "graph_review_load_per_100_records": graph_eval["review_load_per_100_records"],
        "duplicate_clue_rate": overall_eval["duplicate_clue_rate"],
        "metric_note": "review_load_is_reported_separately_from_standard_vs_graph_quality",
    }
    return {
        **overall_eval,
        "standard_clue_eval": standard_eval,
        "graph_clue_eval": graph_eval,
        "object_clue_eval": object_eval,
        "overall_review_load_eval": review_load_eval,
        "evaluation_layers": ["standard_clue_eval", "graph_clue_eval", "object_clue_eval", "overall_review_load_eval"],
    }


def _evaluate_clue_layer(
    records: list[dict[str, Any]],
    actual_clues: list[dict[str, Any]],
    *,
    layer: str,
) -> dict[str, Any]:
    expected_count, expected_types = _expected_clue_gold(records, layer=layer)
    actual = [item for item in actual_clues if _clue_in_layer(item, layer=layer)]
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
    status = "completed"
    metric_note = f"{layer}_clue_quality"
    if expected_count == 0 and not expected_types:
        status = "no_gold_overgeneration_only" if actual else "not_applicable_no_gold"
        metric_note = f"{layer}_clue_gold_missing"
    return {
        "overall": {**overall, "tp": tp, "fp": fp, "fn": fn, "status": status, "metric_note": metric_note},
        "expected_clue_count": expected_count,
        "actual_clue_count": len(actual),
        "clue_overgeneration_ratio": round(len(actual) / max(expected_count, 1), 4),
        "valid_clue_precision_by_count": round(min(expected_count, len(actual)) / max(len(actual), 1), 4),
        "review_load_per_100_records": round(len(actual) / max(len(records), 1) * 100, 4),
        "duplicate_clue_rate": _duplicate_clue_rate(actual),
        "actual_clue_types": sorted(actual_types),
        "expected_clue_types": sorted(expected_types),
        "metric_layer": layer,
        "status": status,
    }


def _expected_clue_gold(records: list[dict[str, Any]], *, layer: str) -> tuple[int, set[str]]:
    expected_types = {
        clue_type
        for record in records
        for clue_type in _expected_clue_types(record, include_expected_clues=True)
        if _clue_type_in_layer(clue_type, layer=layer)
    }
    expected_count_values = [
        int(record.get("expected_clue_count") or 0)
        for record in records
        if record.get("expected_clue_count") is not None
        and (
            layer == "overall"
            or any(_clue_type_in_layer(clue_type, layer=layer) for clue_type in _expected_clue_types(record, include_expected_clues=True))
            or (not _expected_clue_types(record, include_expected_clues=True) and layer == "standard")
        )
    ]
    expected_count = max(expected_count_values) if expected_count_values else 0
    if not expected_count_values:
        expected_count = sum(
            1
            for record in records
            for clue_type in _expected_clue_types(record, include_expected_clues=True)
            if _clue_type_in_layer(clue_type, layer=layer)
        )
    expected_count = max(expected_count, len(expected_types))
    return expected_count, expected_types


def _evaluate_clue_objects(records: list[dict[str, Any]], actual_clues: list[dict[str, Any]]) -> dict[str, Any]:
    expected = _expected_clue_objects(records)
    if not expected:
        return {
            "status": "not_applicable_no_expected_clue_objects",
            "overall": {**prf(0, 0, 0), "tp": 0, "fp": 0, "fn": 0, "status": "not_applicable_no_expected_clue_objects"},
            "expected_clue_count": 0,
            "actual_clue_count": len(actual_clues),
            "evidence_chain_precision": 0.0,
            "evidence_chain_recall": 0.0,
            "evidence_reviewability_rate": 0.0,
            "duplicate_clue_rate": _duplicate_clue_rate(actual_clues),
        }

    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    matched_actual_indexes: set[int] = set()
    for expected_clue in expected:
        match_index = _best_clue_object_match(expected_clue, actual_clues, matched_actual_indexes)
        if match_index is None:
            continue
        matched_actual_indexes.add(match_index)
        matches.append((expected_clue, actual_clues[match_index]))

    tp = len(matches)
    fp = max(0, len(actual_clues) - tp)
    fn = max(0, len(expected) - tp)
    evidence_precision, evidence_recall = _evidence_chain_pr(matches)
    reviewable_count = sum(1 for expected_clue, actual in matches if _clue_reviewability_satisfied(expected_clue, actual))
    overall = prf(tp, fp, fn)
    return {
        "status": "completed",
        "overall": {**overall, "tp": tp, "fp": fp, "fn": fn, "status": "completed", "metric_note": "object_level_expected_clues"},
        "expected_clue_count": len(expected),
        "actual_clue_count": len(actual_clues),
        "matched_clue_count": tp,
        "evidence_chain_precision": evidence_precision,
        "evidence_chain_recall": evidence_recall,
        "evidence_reviewability_rate": round(reviewable_count / max(tp, 1), 4),
        "duplicate_clue_rate": _duplicate_clue_rate(actual_clues),
        "matched_expected_clue_types": sorted({str(item[0].get("clue_type") or "") for item in matches if str(item[0].get("clue_type") or "")}),
    }


def _expected_clue_objects(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for record in records:
        raw = record.get("expected_clues")
        if not isinstance(raw, list):
            continue
        for clue in raw:
            if not isinstance(clue, Mapping):
                continue
            item = dict(clue)
            item.setdefault("record_trace_id", _trace_id(record))
            if str(item.get("clue_type") or "").strip():
                objects.append(item)
    return objects


def _best_clue_object_match(
    expected: Mapping[str, Any],
    actual_clues: list[dict[str, Any]],
    used_indexes: set[int],
) -> int | None:
    scored: list[tuple[int, int]] = []
    for index, actual in enumerate(actual_clues):
        if index in used_indexes:
            continue
        score = _clue_object_match_score(expected, actual)
        if score > 0:
            scored.append((score, index))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][1]


def _clue_object_match_score(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> int:
    if _normalize_label(expected.get("clue_type")) != _normalize_label(actual.get("clue_type")):
        return 0
    score = 2
    expected_key = _normalize_entity_value(expected.get("key"))
    actual_key = _normalize_entity_value(actual.get("key"))
    has_key_match = False
    if expected_key and actual_key and (expected_key == actual_key or expected_key in actual_key or actual_key in expected_key):
        has_key_match = True
        score += 4
    expected_risk = _normalize_label(expected.get("risk_category"))
    actual_risk = _normalize_label(actual.get("risk_category"))
    if expected_risk and actual_risk and expected_risk == actual_risk:
        score += 2
    expected_entities = {_normalize_entity_value(item) for item in expected.get("expected_entity_values") or [] if _normalize_entity_value(item)}
    actual_entities = {_normalize_entity_value(item) for item in actual.get("entity_values") or [] if _normalize_entity_value(item)}
    has_entity_match = bool(expected_entities and actual_entities & expected_entities)
    if has_entity_match:
        score += 3
    expected_evidence = _evidence_set(expected)
    actual_evidence = _evidence_set(actual)
    overlap_count = len(expected_evidence & actual_evidence)
    if overlap_count:
        score += 2 + overlap_count
    if expected_key and not has_key_match and not has_entity_match:
        return 0
    if not expected_key and not expected_entities:
        if expected_evidence and actual_evidence & expected_evidence:
            score += 2
    return score if score >= 5 else 0


def _evidence_chain_pr(matches: list[tuple[dict[str, Any], dict[str, Any]]]) -> tuple[float, float]:
    expected_total = actual_total = matched_total = 0
    for expected, actual in matches:
        expected_evidence = _evidence_set(expected)
        actual_evidence = _evidence_set(actual)
        expected_total += len(expected_evidence)
        actual_total += len(actual_evidence)
        matched_total += len(expected_evidence & actual_evidence)
    precision = matched_total / actual_total if actual_total else 0.0
    recall = matched_total / expected_total if expected_total else 0.0
    return round(precision, 4), round(recall, 4)


def _clue_reviewability_satisfied(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    reviewability = actual.get("evidence_reviewability") if isinstance(actual.get("evidence_reviewability"), Mapping) else {}
    evidence_count = len(_evidence_set(actual))
    source_count = int(reviewability.get("source_count") or len({str(item) for item in actual.get("source_names") or [] if str(item).strip()}) or 0)
    entity_support_count = int(reviewability.get("entity_support_count") or len({str(item) for item in actual.get("entity_values") or [] if str(item).strip()}) or 0)
    if evidence_count < int(expected.get("min_evidence_count") or 0):
        return False
    if source_count < int(expected.get("min_source_count") or 0):
        return False
    expected_entities = {_normalize_entity_value(item) for item in expected.get("expected_entity_values") or [] if _normalize_entity_value(item)}
    actual_entities = {_normalize_entity_value(item) for item in actual.get("entity_values") or [] if _normalize_entity_value(item)}
    if expected_entities and not (expected_entities & actual_entities):
        return False
    if expected.get("requires_original_snippets") and not list(reviewability.get("original_snippets") or actual.get("original_snippets") or []):
        return False
    if expected.get("requires_time_range"):
        time_range = reviewability.get("time_range") if isinstance(reviewability.get("time_range"), Mapping) else {}
        if not (time_range.get("start") and time_range.get("end")):
            return False
    return entity_support_count >= 0


def _evidence_set(clue: Mapping[str, Any]) -> set[str]:
    values = clue.get("expected_evidence_trace_ids") or clue.get("evidence_trace_ids") or []
    return {str(item).strip() for item in values if str(item).strip()}


def dataset_profile(
    records: list[dict[str, Any]],
    *,
    explicit_name: str | None = None,
    explicit_kind: str | None = None,
) -> dict[str, Any]:
    """Summarize dataset provenance so held-out results are not mixed with smoke gold."""

    names = _counter_payload(Counter(str(record.get("dataset_name") or "").strip() for record in records if str(record.get("dataset_name") or "").strip()))
    kinds = _counter_payload(Counter(str(record.get("dataset_kind") or "").strip() for record in records if str(record.get("dataset_kind") or "").strip()))
    annotation_sources = _counter_payload(
        Counter(str(record.get("annotation_source") or "unspecified") for record in records)
    )
    source_types = _counter_payload(Counter(str(record.get("source_type") or "unknown") for record in records))
    source_names = _counter_payload(Counter(str(record.get("source_name") or "unknown") for record in records), limit=12)
    content_modalities = _counter_payload(Counter(str(record.get("content_modality") or "text") for record in records))
    holdout_splits = _counter_payload(Counter(str(record.get("holdout_split") or "").strip() for record in records if str(record.get("holdout_split") or "").strip()))
    resolved_kind = explicit_kind or (kinds[0]["value"] if kinds else "unspecified_gold")
    return {
        "name": explicit_name or (names[0]["value"] if names else None),
        "kind": resolved_kind,
        "record_count": len(records),
        "is_heldout": "heldout" in str(resolved_kind).lower() or bool(holdout_splits),
        "annotation_sources": annotation_sources,
        "source_types": source_types,
        "source_names_top": source_names,
        "content_modalities": content_modalities,
        "holdout_splits": holdout_splits,
        "claim_boundary": (
            "Held-out/public-authorized reports prove only this local annotated split; "
            "they must not be described as full online generalization."
        ),
    }


def _classification_review_load(actual_items: list[dict[str, Any]], *, record_count: int) -> dict[str, Any]:
    review_items = [item for item in actual_items if bool(item.get("review_required"))]
    return {
        "review_required_count": len(review_items),
        "record_count": record_count,
        "review_rate": round(len(review_items) / max(record_count, 1), 4),
        "review_load_per_100_records": round(len(review_items) / max(record_count, 1) * 100.0, 4),
        "by_review_bucket": _counter_payload(Counter(_review_bucket(item) for item in review_items)),
        "final_review_buckets": _counter_payload(Counter(_review_bucket(item) for item in actual_items)),
        "by_risk_category": _counter_payload(Counter(str(item.get("risk_category") or "unknown") for item in review_items)),
        "by_secondary_label": _counter_payload(Counter(str(item.get("secondary_label") or "待研判") for item in review_items)),
        "by_conflict_status": _counter_payload(Counter(str(item.get("conflict_status") or "RESOLVED") for item in review_items)),
    }


def _review_bucket(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("review_bucket") or "").strip()
    conflict_status = str(item.get("conflict_status") or "").strip()
    review_required = bool(item.get("review_required"))
    if explicit and not review_required and conflict_status != "CONFLICT_REVIEW":
        return explicit
    return review_bucket_for_classification(
        risk_category=str(item.get("risk_category") or "").strip(),
        review_required=review_required,
        secondary_label=str(item.get("secondary_label") or "").strip(),
        conflict_status=conflict_status,
    )


def _classification_error_examples(
    records: list[dict[str, Any]],
    actual_by_trace: Mapping[str, Mapping[str, Any]],
    *,
    include_secondary: bool,
    limit: int = 10,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for record in records:
        trace_id = _trace_id(record)
        expected_primary = predicted_categories(record)
        expected_secondary = expected_secondary_labels(record)
        actual = actual_by_trace.get(trace_id, {})
        actual_primary = predicted_categories(actual)
        actual_secondary = actual_secondary_labels(actual)
        primary_mismatch = expected_primary != actual_primary
        secondary_mismatch = include_secondary and expected_secondary != actual_secondary
        if not primary_mismatch and not secondary_mismatch:
            continue
        errors.append(
            {
                "source_trace_id": trace_id,
                "source_name": record.get("source_name"),
                "source_type": record.get("source_type"),
                "expected_primary": sorted(expected_primary),
                "actual_primary": sorted(actual_primary),
                "expected_secondary": sorted(expected_secondary),
                "actual_secondary": sorted(actual_secondary),
                "mismatch_type": (
                    "primary_and_secondary"
                    if primary_mismatch and secondary_mismatch
                    else "primary"
                    if primary_mismatch
                    else "secondary"
                ),
                "text_excerpt": _text_excerpt(record),
            }
        )
        if len(errors) >= max(0, int(limit)):
            break
    return errors


def _counter_payload(counter: Counter[str], *, limit: int | None = None) -> list[dict[str, Any]]:
    pairs = counter.most_common(limit)
    return [{"value": value, "count": count} for value, count in pairs if value]


def _text_excerpt(record: Mapping[str, Any], *, limit: int = 160) -> str:
    text = str(record.get("content_text") or record.get("clean_text") or "")
    return text[:limit] + ("..." if len(text) > limit else "")


def predicted_categories(classification: Mapping[str, Any]) -> set[str]:
    risk = _normalize_label(classification.get("risk_category") or classification.get("expected_primary_risk"))
    expected = classification.get("expected_risk_categories")
    categories = {_normalize_label(item) for item in expected} if isinstance(expected, list) else set()
    categories.update({_normalize_label(item) for item in classification.get("expected_primary_risks", [])} if isinstance(classification.get("expected_primary_risks"), list) else set())
    conflicts = classification.get("conflict_categories")
    if isinstance(conflicts, list):
        categories.update(_normalize_label(item) for item in conflicts)
    if risk:
        categories.add(risk)
    return {item for item in categories if item not in {"unknown", "normal_noise", "正常业务白噪声", "待研判", "无风险", "none"}}


def actual_secondary_labels(classification: Mapping[str, Any]) -> set[str]:
    labels = set()
    secondary = _normalize_label(classification.get("secondary_label"))
    if secondary:
        labels.add(secondary)
    conflicts = classification.get("conflict_categories")
    candidates = classification.get("candidate_secondary_labels")
    if isinstance(conflicts, list) and conflicts and isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            label = _normalize_label(item.get("label"))
            reason = str(item.get("reason") or "").strip()
            evidence = item.get("evidence")
            has_evidence = isinstance(evidence, list) and any(str(value).strip() for value in evidence)
            if label and has_evidence and reason not in {"single_secondary_marker_only"}:
                labels.add(label)
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


def _formal_expected_secondary_labels(classification: Mapping[str, Any]) -> set[str]:
    labels = expected_secondary_labels(classification)
    if predicted_categories(classification):
        return labels
    return {label for label in labels if label not in NON_RISK_SECONDARY_LABELS}


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
    if args.profile_curve:
        report = evaluate_profile_curve(
            loaded["records"],
            entity_records=loaded["entity_records"],
            clue_records=loaded["clue_records"],
            hard_negative_records=loaded["hard_negative_records"],
            llm_mode=args.llm_mode,
            with_budget=True,
            classification_granularity=args.classification_granularity,
            dataset_name=args.dataset_name,
            dataset_kind=args.dataset_kind,
        )
    elif args.ablation:
        report = evaluate_ablation(**loaded, with_budget=True, include_real=args.ablation_include_real)
    else:
        report = evaluate(
            loaded["records"],
            entity_records=loaded["entity_records"],
            clue_records=loaded["clue_records"],
            hard_negative_records=loaded["hard_negative_records"],
            profile=args.profile,
            llm_mode=args.llm_mode,
            with_budget=args.with_budget,
            classification_granularity=args.classification_granularity,
            dataset_name=args.dataset_name,
            dataset_kind=args.dataset_kind,
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
            "enable_graph_clue_generation": False,
        },
        "balanced": {
            "enable_llm_intent_parse": True,
            "enable_query_rewrite": True,
            "enable_live_collection": True,
            "enable_llm_record_enrich": True,
            "enable_llm_clue_refine": True,
            "enable_graph_clue_generation": True,
        },
        "high_recall": {
            "enable_llm_intent_parse": True,
            "enable_query_rewrite": True,
            "enable_live_collection": True,
            "enable_llm_record_enrich": True,
            "enable_llm_clue_refine": True,
            "enable_graph_clue_generation": True,
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
    dataset_fingerprint = _ablation_dataset_fingerprint(
        records,
        entity_records=entity_records,
        clue_records=clue_records,
        hard_negative_records=hard_negative_records,
    )
    input_counts = _ablation_input_counts(
        records,
        entity_records=entity_records,
        clue_records=clue_records,
        hard_negative_records=hard_negative_records,
    )

    def run_scenario(
        name: str,
        *,
        profile: str,
        requested_llm_mode: str,
        effective_llm_mode: str,
        provider_status: str,
        fallback_reason: str | None = None,
        real_requested: bool = False,
        real_gateway_configured: bool = False,
    ) -> dict[str, Any]:
        report = evaluate(
            deepcopy(records),
            entity_records=deepcopy(entity_records),
            clue_records=deepcopy(clue_records),
            hard_negative_records=deepcopy(hard_negative_records),
            profile=profile,
            llm_mode=effective_llm_mode,
            with_budget=with_budget,
        )
        marker = {
            "name": name,
            "profile": profile,
            "requested_llm_mode": requested_llm_mode,
            "effective_llm_mode": effective_llm_mode,
            "provider_status": provider_status,
            "fallback_reason": fallback_reason,
            "real_requested": real_requested,
            "real_gateway_configured": real_gateway_configured,
            "dataset_fingerprint": dataset_fingerprint,
            "input_counts": input_counts,
        }
        if requested_llm_mode == "real_or_configured_fallback" and provider_status == "real":
            marker.update(_real_scenario_runtime_status(report))
        report["ablation_scenario"] = marker
        report["dataset_fingerprint"] = dataset_fingerprint
        return report

    real_or_fallback = _real_or_fallback_ablation_choice(include_real=include_real)
    scenarios = {
        "fast_off": run_scenario(
            "fast_off",
            profile="fast",
            requested_llm_mode="off",
            effective_llm_mode="off",
            provider_status="off",
        ),
        "balanced_mock": run_scenario(
            "balanced_mock",
            profile="balanced",
            requested_llm_mode="mock",
            effective_llm_mode="mock",
            provider_status="mock",
        ),
        "high_recall_off": run_scenario(
            "high_recall_off",
            profile="high_recall",
            requested_llm_mode="off",
            effective_llm_mode="off",
            provider_status="off",
        ),
        "high_recall_mock": run_scenario(
            "high_recall_mock",
            profile="high_recall",
            requested_llm_mode="mock",
            effective_llm_mode="mock",
            provider_status="mock",
        ),
        "high_recall_real_or_configured_fallback": run_scenario(
            "high_recall_real_or_configured_fallback",
            profile="high_recall",
            requested_llm_mode="real_or_configured_fallback",
            effective_llm_mode=real_or_fallback["effective_llm_mode"],
            provider_status=real_or_fallback["provider_status"],
            fallback_reason=real_or_fallback.get("fallback_reason"),
            real_requested=bool(real_or_fallback["real_requested"]),
            real_gateway_configured=bool(real_or_fallback["real_gateway_configured"]),
        ),
    }
    if include_real:
        scenarios["high_recall_real"] = scenarios["high_recall_real_or_configured_fallback"]
    base = scenarios["high_recall_off"]
    llm = scenarios["high_recall_mock"]
    comparison = _llm_value_delta(base, llm)
    comparison["balanced_mock"] = _llm_value_delta(scenarios["fast_off"], scenarios["balanced_mock"])
    comparison["real_or_fallback"] = _llm_value_delta(base, scenarios["high_recall_real_or_configured_fallback"])
    real_or_fallback_marker = scenarios["high_recall_real_or_configured_fallback"].get("ablation_scenario") or {}
    comparison["real_or_fallback"]["provider_status"] = real_or_fallback_marker.get("provider_status") or real_or_fallback["provider_status"]
    comparison["real_or_fallback"]["fallback_reason"] = real_or_fallback_marker.get("fallback_reason") or real_or_fallback.get("fallback_reason")
    if include_real:
        comparison["real"] = comparison["real_or_fallback"]
    matrix_scenarios = [
        "fast_off",
        "balanced_mock",
        "high_recall_real_or_configured_fallback",
    ]
    scenario_fingerprints = {
        str((report.get("ablation_scenario") or {}).get("dataset_fingerprint") or "")
        for name, report in scenarios.items()
        if name != "high_recall_real"
    }
    value_gate = LLMValueGate()
    return {
        "status": "completed",
        "mode": "llm_ablation",
        "runtime_value_gate_applied": False,
        "llm_value_gate_mode": "disabled_for_ablation_measurement",
        "dataset_fingerprint": dataset_fingerprint,
        "scenario_consistency": {
            "dataset_fingerprint": dataset_fingerprint,
            "same_dataset_fingerprint": scenario_fingerprints == {dataset_fingerprint},
            "input_counts": input_counts,
        },
        "scenarios": scenarios,
        "llm_value_matrix": _llm_value_matrix(matrix_scenarios, scenarios, baseline_name="fast_off"),
        "llm_value": comparison,
        "llm_value_gate": {
            "should_enable_record_enrich": value_gate.should_enable_record_enrich("high_recall", comparison),
            "reason": comparison["gate_reason"],
        },
    }


def _ablation_input_counts(
    records: list[dict[str, Any]],
    *,
    entity_records: list[dict[str, Any]] | None,
    clue_records: list[dict[str, Any]] | None,
    hard_negative_records: list[dict[str, Any]] | None,
) -> dict[str, int]:
    return {
        "classification_records": len(records),
        "entity_records": len(entity_records or records),
        "clue_records": len(clue_records or []),
        "hard_negative_records": len(hard_negative_records or []),
    }


def _ablation_dataset_fingerprint(
    records: list[dict[str, Any]],
    *,
    entity_records: list[dict[str, Any]] | None,
    clue_records: list[dict[str, Any]] | None,
    hard_negative_records: list[dict[str, Any]] | None,
) -> str:
    payload = {
        "classification_records": records,
        "entity_records": entity_records if entity_records is not None else records,
        "clue_records": clue_records or [],
        "hard_negative_records": hard_negative_records or [],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _real_or_fallback_ablation_choice(*, include_real: bool) -> dict[str, Any]:
    config = LLMGatewayConfig.from_env()
    real_gateway_configured = bool(config.api_key) and not config.dry_run and not config.mock
    if real_gateway_configured:
        return {
            "effective_llm_mode": "real",
            "provider_status": "real",
            "fallback_reason": None,
            "real_requested": include_real,
            "real_gateway_configured": True,
        }
    if config.mock:
        fallback_reason = "real_gateway_mock_mode_configured"
    elif config.dry_run:
        fallback_reason = "real_gateway_dry_run_or_missing_credentials"
    elif not config.api_key:
        fallback_reason = "real_gateway_missing_api_key"
    else:
        fallback_reason = "real_gateway_unavailable"
    if include_real:
        fallback_reason = f"real_requested_{fallback_reason}"
    return {
        "effective_llm_mode": "mock",
        "provider_status": "fallback",
        "fallback_reason": fallback_reason,
        "real_requested": include_real,
        "real_gateway_configured": False,
    }


def _real_scenario_runtime_status(report: Mapping[str, Any]) -> dict[str, Any]:
    summary = report.get("pipeline_summary") if isinstance(report.get("pipeline_summary"), Mapping) else {}
    traces = summary.get("llm_call_traces") if isinstance(summary.get("llm_call_traces"), list) else []
    if not traces:
        return {
            "provider_status": "fallback",
            "fallback_reason": "real_gateway_configured_but_no_llm_calls_attempted",
        }
    network_attempted = any(bool(item.get("network_attempted")) for item in traces if isinstance(item, Mapping))
    successful_network = any(
        bool(item.get("network_attempted")) and bool(item.get("llm_ok"))
        for item in traces
        if isinstance(item, Mapping)
    )
    if successful_network:
        return {}
    errors = sorted(
        {
            str(item.get("error") or "").strip()
            for item in traces
            if isinstance(item, Mapping) and str(item.get("error") or "").strip()
        }
    )
    if network_attempted:
        reason = "real_gateway_network_failed"
        if errors:
            reason = f"{reason}:{','.join(errors[:3])}"
    else:
        reason = "real_gateway_configured_but_used_local_fallback"
        if errors:
            reason = f"{reason}:{','.join(errors[:3])}"
    return {
        "provider_status": "fallback",
        "fallback_reason": reason,
    }


def _llm_value_matrix(
    scenario_names: Iterable[str],
    scenarios: Mapping[str, Mapping[str, Any]],
    *,
    baseline_name: str,
) -> list[dict[str, Any]]:
    baseline = scenarios[baseline_name]
    rows: list[dict[str, Any]] = []
    for name in scenario_names:
        report = scenarios[name]
        marker = report.get("ablation_scenario") if isinstance(report.get("ablation_scenario"), Mapping) else {}
        dimensions = report.get("profile_comparison_dimensions") if isinstance(report.get("profile_comparison_dimensions"), Mapping) else {}
        row = {
            "scenario": name,
            "profile": marker.get("profile") or report.get("profile"),
            "requested_llm_mode": marker.get("requested_llm_mode") or report.get("llm_mode"),
            "effective_llm_mode": marker.get("effective_llm_mode") or report.get("llm_mode"),
            "provider_status": marker.get("provider_status") or report.get("llm_mode"),
            "fallback_reason": marker.get("fallback_reason"),
            "dataset_fingerprint": marker.get("dataset_fingerprint") or report.get("dataset_fingerprint"),
            "quality": {
                "primary_classification_f1": report.get("primary_classification_f1"),
                "secondary_classification_f1": report.get("secondary_classification_f1"),
                "hierarchical_classification_f1": report.get("hierarchical_classification_f1"),
                "entity_f1": report.get("entity_f1"),
                "clue_recall": report.get("clue_recall"),
                "clue_f1": report.get("clue_f1"),
                "false_positive_rate": report.get("false_positive_rate"),
            },
            "cost": {
                "llm_calls": dimensions.get("llm_calls"),
                "llm_calls_per_1000_records": report.get("llm_calls_per_1000_records"),
                "estimated_tokens": dimensions.get("estimated_tokens"),
                "estimated_tokens_per_valid_clue": report.get("estimated_tokens_per_valid_clue"),
            },
            "latency": {
                "p50_latency_ms": report.get("p50_latency_ms"),
                "p95_latency_ms": dimensions.get("p95_latency_ms") or report.get("p95_latency_ms"),
            },
        }
        if name != baseline_name:
            row[f"delta_vs_{baseline_name}"] = _llm_value_delta(baseline, report)
        rows.append(row)
    return rows


def evaluate_profile_curve(
    records: list[dict[str, Any]],
    *,
    entity_records: list[dict[str, Any]] | None = None,
    clue_records: list[dict[str, Any]] | None = None,
    hard_negative_records: list[dict[str, Any]] | None = None,
    profiles: Iterable[str] = ("fast", "balanced", "high_recall"),
    llm_mode: str = "off",
    with_budget: bool = True,
    classification_granularity: str = "auto",
    dataset_name: str | None = None,
    dataset_kind: str | None = None,
) -> dict[str, Any]:
    scenarios: dict[str, dict[str, Any]] = {}
    curve: list[dict[str, Any]] = []
    for profile in profiles:
        normalized_profile = str(profile or "").strip()
        if normalized_profile not in {"fast", "balanced", "high_recall"}:
            continue
        report = evaluate(
            deepcopy(records),
            entity_records=deepcopy(entity_records),
            clue_records=deepcopy(clue_records),
            hard_negative_records=deepcopy(hard_negative_records),
            profile=normalized_profile,
            llm_mode=llm_mode,
            with_budget=with_budget,
            classification_granularity=classification_granularity,
            dataset_name=dataset_name,
            dataset_kind=dataset_kind,
        )
        scenarios[normalized_profile] = report
        curve.append(_profile_curve_row(normalized_profile, report))
    return {
        "status": "completed",
        "mode": "profile_quality_cost_latency_curve",
        "profiles": [row["profile"] for row in curve],
        "llm_mode": llm_mode,
        "profile_quality_cost_latency_curve": curve,
        "scenarios": scenarios,
        "claim_boundary": (
            "Curve compares local/offline pipeline quality, deterministic routing cost estimates, "
            "and measured local p95 latency for the same evaluation fixtures."
        ),
    }


def _profile_curve_row(profile: str, report: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = report.get("profile_comparison_dimensions") if isinstance(report.get("profile_comparison_dimensions"), Mapping) else {}
    return {
        "profile": profile,
        "quality": {
            "primary_classification_f1": report.get("primary_classification_f1"),
            "secondary_classification_f1": report.get("secondary_classification_f1"),
            "hierarchical_classification_f1": report.get("hierarchical_classification_f1"),
            "classification_recall": dimensions.get("classification_recall") or report.get("classification_recall"),
            "false_positive_rate": report.get("false_positive_rate"),
            "classification_review_rate": report.get("classification_review_rate"),
            "entity_f1": report.get("entity_f1"),
            "clue_precision": report.get("clue_precision"),
            "clue_recall": report.get("clue_recall"),
            "clue_f1": report.get("clue_f1"),
        },
        "cost": {
            "llm_calls": dimensions.get("llm_calls"),
            "llm_calls_per_1000_records": report.get("llm_calls_per_1000_records"),
            "estimated_tokens": dimensions.get("estimated_tokens"),
            "estimated_tokens_per_valid_clue": report.get("estimated_tokens_per_valid_clue"),
            "profile_budget": _budget_profile(profile),
        },
        "latency": {
            "p50_latency_ms": report.get("p50_latency_ms"),
            "p95_latency_ms": dimensions.get("p95_latency_ms") or report.get("p95_latency_ms"),
        },
        "tradeoff_summary": {
            "profile": profile,
            "quality_signal": report.get("hierarchical_classification_f1") or report.get("primary_classification_f1"),
            "cost_signal": report.get("llm_calls_per_1000_records"),
            "latency_signal_ms": dimensions.get("p95_latency_ms") or report.get("p95_latency_ms"),
        },
    }


def _gateway_for_mode(llm_mode: str) -> LLMGateway | None:
    if llm_mode == "mock":
        return LLMGateway(LLMGatewayConfig(dry_run=True, mock=True))
    if llm_mode == "real":
        return LLMGateway(LLMGatewayConfig.from_env())
    return None


def _budget_profile(profile: str) -> dict[str, Any]:
    configured = _configured_budget_profile(profile)
    if configured:
        return configured
    return {
        "fast": {
            "max_candidate_clues": 20,
            "max_llm_calls": 3,
            "max_llm_tokens": 3000,
            "max_llm_classify_records": 5,
            "max_llm_extract_records": 5,
            "max_llm_refine_clues": 2,
        },
        "balanced": {
            "max_candidate_clues": 50,
            "max_llm_calls": 10,
            "max_llm_tokens": 10000,
            "max_llm_classify_records": 20,
            "max_llm_extract_records": 20,
            "max_llm_refine_clues": 6,
        },
        "high_recall": {
            "max_candidate_clues": 200,
            "max_llm_calls": 40,
            "max_llm_tokens": 50000,
            "max_llm_classify_records": 100,
            "max_llm_extract_records": 100,
            "max_llm_refine_clues": 20,
        },
    }.get(profile, {})


def _configured_budget_profile(profile: str) -> dict[str, Any]:
    path = PROJECT_ROOT / "config" / "routing_profiles.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    profiles = payload.get("routing_profiles") if isinstance(payload, Mapping) else {}
    raw = profiles.get(profile) if isinstance(profiles, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    budget_keys = {
        "max_elapsed_seconds",
        "max_sources",
        "max_raw_records",
        "max_candidate_clues",
        "max_llm_calls",
        "max_llm_tokens",
        "max_llm_classify_records",
        "max_llm_extract_records",
        "max_llm_refine_clues",
        "max_query_rewrite_sources",
    }
    return {key: raw[key] for key in budget_keys if key in raw}


def _llm_value_delta(base: Mapping[str, Any], llm: Mapping[str, Any]) -> dict[str, Any]:
    classification_delta = round(_numeric_metric(llm.get("primary_classification_f1")) - _numeric_metric(base.get("primary_classification_f1")), 4)
    entity_delta = round(_numeric_metric(llm.get("entity_f1")) - _numeric_metric(base.get("entity_f1")), 4)
    hard_negative_delta = round(_numeric_metric(llm.get("false_positive_rate")) - _numeric_metric(base.get("false_positive_rate")), 4)
    clue_precision_delta = round(_numeric_metric(llm.get("clue_f1")) - _numeric_metric(base.get("clue_f1")), 4)
    clue_recall_delta = round(_numeric_metric(llm.get("clue_recall")) - _numeric_metric(base.get("clue_recall")), 4)
    llm_calls_delta = round(_numeric_metric(llm.get("llm_calls_per_1000_records")) - _numeric_metric(base.get("llm_calls_per_1000_records")), 4)
    llm_dimensions = llm.get("profile_comparison_dimensions") if isinstance(llm.get("profile_comparison_dimensions"), Mapping) else {}
    base_dimensions = base.get("profile_comparison_dimensions") if isinstance(base.get("profile_comparison_dimensions"), Mapping) else {}
    token_delta = float(llm_dimensions.get("estimated_tokens") or 0) - float(base_dimensions.get("estimated_tokens") or 0)
    latency_delta = round(
        _numeric_metric(llm_dimensions.get("p95_latency_ms") or llm.get("p95_latency_ms"))
        - _numeric_metric(base_dimensions.get("p95_latency_ms") or base.get("p95_latency_ms")),
        4,
    )
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
        "p95_latency_ms_delta": latency_delta,
        "tokens_per_f1_gain": None if f1_gain <= 0 else round(token_delta / f1_gain, 4),
        "tokens_per_extra_valid_clue": None if extra_valid_clues <= 0 else round(token_delta / extra_valid_clues, 4),
        "latency_ms_per_f1_gain": None if f1_gain <= 0 else round(latency_delta / f1_gain, 4),
        "latency_ms_per_extra_valid_clue": None if extra_valid_clues <= 0 else round(latency_delta / extra_valid_clues, 4),
        "gate_reason": gate_reason,
    }


def _classification_pairs(primary: set[str], secondary: set[str]) -> set[tuple[str, str]]:
    if not primary:
        return set()
    if not secondary:
        return {(item, "*") for item in primary}
    return {(item, label) for item in primary for label in secondary}


def _pair_label(primary: str, secondary: str) -> str:
    return f"{primary}>{secondary}"


def _update_confusion(matrix: defaultdict[str, Counter[str]], expected: set[str], actual: set[str]) -> None:
    expected = {str(item) for item in expected if str(item).strip()}
    actual = {str(item) for item in actual if str(item).strip()}
    if not expected and not actual:
        matrix["__negative__"]["__negative__"] += 1
        return
    if not expected:
        for actual_label in sorted(actual):
            matrix["__no_gold__"][actual_label] += 1
        return
    if not actual:
        for expected_label in sorted(expected):
            matrix[expected_label]["__missing_prediction__"] += 1
        return
    for expected_label in sorted(expected):
        if expected_label in actual:
            matrix[expected_label][expected_label] += 1
        else:
            for actual_label in sorted(actual):
                matrix[expected_label][actual_label] += 1
    for actual_label in sorted(actual - expected):
        matrix["__extra_prediction__"][actual_label] += 1


def _confusion_payload(matrix: Mapping[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {
        expected_label: dict(sorted(actual_counts.items()))
        for expected_label, actual_counts in sorted(matrix.items())
    }


def _expected_clue_types(record: Mapping[str, Any], *, include_expected_clues: bool = False) -> list[str]:
    values: list[str] = []
    raw_types = record.get("expected_clue_types")
    if isinstance(raw_types, list):
        values.extend(str(item).strip() for item in raw_types if str(item).strip())
    if include_expected_clues:
        expected_clues = record.get("expected_clues")
        if isinstance(expected_clues, list):
            for clue in expected_clues:
                clue_type = clue.get("clue_type") if isinstance(clue, Mapping) else clue
                if str(clue_type or "").strip():
                    values.append(str(clue_type).strip())
    return values


def _clue_in_layer(clue: Mapping[str, Any], *, layer: str) -> bool:
    return _clue_type_in_layer(clue.get("clue_type"), layer=layer)


def _clue_type_in_layer(value: Any, *, layer: str) -> bool:
    if layer == "overall":
        return True
    is_graph = _is_graph_clue_type(value)
    if layer == "graph":
        return is_graph
    return not is_graph


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


def _contains_graph_clue_gold(records: Iterable[Mapping[str, Any]]) -> bool:
    for record in records:
        expected_types = record.get("expected_clue_types")
        if isinstance(expected_types, list) and any(_is_graph_clue_type(item) for item in expected_types):
            return True
        expected_clues = record.get("expected_clues")
        if isinstance(expected_clues, list):
            for clue in expected_clues:
                clue_type = clue.get("clue_type") if isinstance(clue, Mapping) else clue
                if _is_graph_clue_type(clue_type):
                    return True
    return False


def _is_graph_clue_type(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("entity_graph_") or text.startswith("graph_")


def _numeric_metric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
