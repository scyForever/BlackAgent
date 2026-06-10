"""Mine unknown/review-required rows into slang candidates for human review."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cleaner.text_filter import normalize_text
from src.collector.base_collector import get_record_field
from src.enhancement.lifecycle import DynamicSlangLifecycleManager
from src.rules.registry import RuleRegistry


UNKNOWN_CATEGORIES = {"unknown", "unknown_risk_pattern", "待研判", "未细分", ""}
PENDING_SECONDARY = {"待研判", "未细分", "unknown", ""}
CONTACT_MARKERS = ("联系", "私聊", "咨询", "客服", "@", "tg:", "telegram", "微信", "vx", "+v", "➕v")
ACTION_MARKERS = ("上车", "接单", "合作", "对接", "老板", "包量", "低价", "价格", "发货", "下单")
COMMON_STOP_TERMS = {
    "今天",
    "今晚",
    "继续",
    "联系",
    "咨询",
    "客服",
    "截图",
    "暗号",
    "业务",
    "平台",
    "工具",
    "账号",
    "注册",
    "任务",
    "老板",
    "合作",
    "上车",
    "下单",
    "版本",
    "更新",
    "功能",
    "教程",
    "说明",
    "普通",
    "文章",
    "内容",
    "公开",
    "可以",
    "使用",
    "自动",
    "详聊",
    "优惠",
    "public",
    "read",
    "full",
    "guide",
    "channel",
    "update",
    "contact",
    "automationforum",
    "com",
    "and",
    "the",
    "at",
    "tg",
    "admin",
    "image",
    "useful",
    "for",
    "in",
    "to",
    "of",
    "me",
    "is",
    "it",
    "on",
    "as",
    "by",
    "or",
    "be",
    "we",
    "you",
    "your",
    "from",
    "with",
    "this",
    "that",
    "will",
    "can",
    "using",
    "used",
    "students",
    "fault",
    "http",
    "https",
    "telegram",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build reviewable slang candidate report from unknown/pending rows.")
    parser.add_argument("--records", default="data/cleaning_phase_cleaned_corpus.jsonl", help="Record JSONL with content_text/clean_text.")
    parser.add_argument(
        "--classifications",
        default="data/classification_extraction_phase_classifications.jsonl",
        help="Classification JSONL with source_trace_id.",
    )
    parser.add_argument("--output", default="data/slang_candidate_report.json", help="JSON report output path.")
    parser.add_argument(
        "--review-csv-out",
        default="data/manual_review/slang_candidate_review_template.csv",
        help="CSV template for analyst slang candidate review.",
    )
    parser.add_argument(
        "--lifecycle-out",
        default="data/manual_review/slang_lifecycle_records.json",
        help="Lifecycle JSON generated from --review-csv-out when analyst decisions are present.",
    )
    parser.add_argument(
        "--dictionary-update-out",
        default="data/manual_review/slang_dictionary_update.json",
        help="Export-only slang dictionary overlay generated from runtime-ready lifecycle records.",
    )
    parser.add_argument("--min-count", type=int, default=3, help="Minimum candidate frequency.")
    parser.add_argument("--max-candidates", type=int, default=80, help="Maximum candidates to emit.")
    return parser.parse_args(argv)


def build_report(
    records: Iterable[Mapping[str, Any] | Any],
    classifications: Iterable[Mapping[str, Any] | Any],
    *,
    min_count: int = 3,
    max_candidates: int = 80,
) -> dict[str, Any]:
    record_by_trace = {_trace_id(record): record for record in records}
    target_classifications = [item for item in classifications if _is_pending_classification(item)]
    target_trace_ids = [_trace_id(item) for item in target_classifications]
    known_terms = _known_terms()
    term_counts: Counter[str] = Counter()
    term_trace_ids: dict[str, list[str]] = defaultdict(list)
    term_contexts: dict[str, list[str]] = defaultdict(list)
    term_context_markers: dict[str, set[str]] = defaultdict(set)

    for trace_id in target_trace_ids:
        record = record_by_trace.get(trace_id)
        if record is None:
            continue
        text = _record_text(record)
        if not text:
            continue
        markers = _context_markers(text)
        for term, count in _candidate_terms(text, known_terms=known_terms, context_markers=markers).items():
            term_counts[term] += count
            if trace_id not in term_trace_ids[term]:
                term_trace_ids[term].append(trace_id)
            if len(term_contexts[term]) < 3:
                term_contexts[term].append(_excerpt_around(text, term))
            term_context_markers[term].update(markers)

    rows = []
    for term, count in term_counts.most_common():
        if count < max(1, int(min_count)):
            continue
        rows.append(
            {
                "term": term,
                "normalized_term": term,
                "count": count,
                "document_count": len(term_trace_ids[term]),
                "source_trace_ids_sample": term_trace_ids[term][:10],
                "context_examples": term_contexts[term],
                "context_markers": sorted(term_context_markers[term]),
                "lifecycle_stage": DynamicSlangLifecycleManager.NEW_CANDIDATE,
                "review_status": "pending_human_confirmation",
                "runtime_ready": False,
                "reason": "high_frequency_unknown_pending_context",
            }
        )
        if len(rows) >= max(1, int(max_candidates)):
            break

    return {
        "status": "completed",
        "run_type": "slang_candidate_discovery_from_unknown_pending_rows",
        "input_record_count": len(record_by_trace),
        "pending_classification_count": len(target_classifications),
        "candidate_count": len(rows),
        "min_count": max(1, int(min_count)),
        "candidates": rows,
        "lifecycle_flow": lifecycle_flow_metadata(),
        "manual_review": {
            "required_next_step": "Review candidates before calling DynamicSlangLifecycleManager.review/gray_rollout/activate.",
            "claim_boundary": "Candidates are discovery leads only; no slang term is activated without human confirmation.",
        },
    }


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(path)
    rows: list[dict[str, Any]] = []
    if not target.exists():
        return rows
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                rows.append(json.loads(line))
    return rows


REVIEW_CSV_FIELDS = [
    "term",
    "normalized_term",
    "review_status",
    "target_risk_category",
    "target_stage",
    "lifecycle_version",
    "batch_id",
    "baseline_eval_report",
    "post_eval_report",
    "reviewer",
    "review_date",
    "notes",
    "source_trace_ids",
    "context_examples",
    "context_markers",
]


def write_review_csv(report: Mapping[str, Any], path: str | Path) -> Path:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=REVIEW_CSV_FIELDS)
        writer.writeheader()
        for candidate in report.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            writer.writerow(
                {
                    "term": candidate.get("term") or "",
                    "normalized_term": candidate.get("normalized_term") or candidate.get("term") or "",
                    "review_status": "pending",
                    "target_risk_category": "",
                    "target_stage": DynamicSlangLifecycleManager.GRAY_ROLLOUT,
                    "lifecycle_version": "",
                    "batch_id": "",
                    "baseline_eval_report": "",
                    "post_eval_report": "",
                    "reviewer": "",
                    "review_date": "",
                    "notes": "",
                    "source_trace_ids": "|".join(str(item) for item in (candidate.get("source_trace_ids_sample") or [])),
                    "context_examples": " || ".join(str(item) for item in (candidate.get("context_examples") or [])),
                    "context_markers": "|".join(str(item) for item in (candidate.get("context_markers") or [])),
                }
            )
    return target


def lifecycle_records_from_review_csv(path: str | Path) -> dict[str, Any]:
    target = _project_path(path)
    if not target.exists():
        return {
            "status": "missing_review_csv",
            "records": [],
            "approved_count": 0,
            "rejected_count": 0,
            "pending_count": 0,
            "approved_reviewed_only_count": 0,
            "reviewed_only_count": 0,
            "invalid_target_stage_count": 0,
            "invalid_target_stage_warnings": [],
            "runtime_ready_records": [],
            "lifecycle_flow": lifecycle_flow_metadata(),
            "activation_blocked_count": 0,
            "activation_warnings": [],
            "claim_boundary": (
                "No lifecycle records are runtime-ready until a human review CSV exists and approved rows are promoted."
            ),
        }
    manager = DynamicSlangLifecycleManager()
    approved_count = 0
    rejected_count = 0
    pending_count = 0
    approved_reviewed_only_count = 0
    reviewed_only_count = 0
    invalid_target_stage_warnings: list[str] = []
    activation_warnings: list[str] = []
    default_lifecycle_version = f"slang-lifecycle-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    default_batch_id = f"slang-review-{target.stem}"
    with target.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for row in reader:
            term = str(row.get("term") or "").strip()
            if not term:
                continue
            status = str(row.get("review_status") or "").strip().lower()
            evidence_ids = [
                item.strip()
                for item in str(row.get("source_trace_ids") or "").split("|")
                if item.strip()
            ]
            normalized = str(row.get("normalized_term") or term).strip() or term
            reviewer = str(row.get("reviewer") or "human_review").strip() or "human_review"
            notes = str(row.get("notes") or "").strip() or None
            target_risk_category = str(row.get("target_risk_category") or "").strip() or None
            review_date = str(row.get("review_date") or "").strip() or None
            lifecycle_version = str(row.get("lifecycle_version") or "").strip() or default_lifecycle_version
            batch_id = str(row.get("batch_id") or "").strip() or default_batch_id
            baseline_eval_report = parse_eval_report(row.get("baseline_eval_report"))
            post_eval_report = parse_eval_report(row.get("post_eval_report"))
            evaluation_gain = evaluation_gain_from_reports(baseline_eval_report, post_eval_report)
            baseline_eval_version = _eval_rule_version(baseline_eval_report)
            post_eval_version = _eval_rule_version(post_eval_report)
            target_stage, invalid_target_stage = _target_stage_from_row(row)
            if status in {"approved", "approve", "accepted"}:
                manager.nominate(term, normalized, evidence_ids)
                record = manager.review(
                    term,
                    approved=True,
                    reviewer=reviewer,
                    notes=notes,
                    lifecycle_version=lifecycle_version,
                    batch_id=batch_id,
                    target_risk_category=target_risk_category,
                    reviewed_at=review_date,
                    baseline_eval_version=baseline_eval_version,
                    post_eval_version=post_eval_version,
                    evaluation_gain=evaluation_gain or None,
                )
                if invalid_target_stage is not None:
                    invalid_target_stage_warnings.append(f"{term}:invalid_target_stage:{invalid_target_stage}")
                if target_stage in {DynamicSlangLifecycleManager.GRAY_ROLLOUT, DynamicSlangLifecycleManager.ACTIVE}:
                    record = manager.gray_rollout(
                        term,
                        reviewer=reviewer,
                        notes=notes,
                        lifecycle_version=lifecycle_version,
                        batch_id=batch_id,
                        target_risk_category=target_risk_category,
                        baseline_eval_version=baseline_eval_version,
                        post_eval_version=post_eval_version,
                        evaluation_gain=evaluation_gain or None,
                    )
                else:
                    approved_reviewed_only_count += 1
                if target_stage == DynamicSlangLifecycleManager.ACTIVE:
                    activation_warnings.append(f"{term}:activation_deferred_until_gray_rollout_eval")
                approved_count += 1
            elif status in {"rejected", "reject", "denied"}:
                manager.nominate(term, normalized, evidence_ids)
                record = manager.review(
                    term,
                    approved=False,
                    reviewer=reviewer,
                    notes=notes,
                    lifecycle_version=lifecycle_version,
                    batch_id=batch_id,
                    target_risk_category=target_risk_category,
                    reviewed_at=review_date,
                    baseline_eval_version=baseline_eval_version,
                    post_eval_version=post_eval_version,
                    evaluation_gain=evaluation_gain or None,
                )
                rejected_count += 1
            elif status in {"reviewed", "review_only", "review-only"}:
                manager.nominate(term, normalized, evidence_ids)
                record = manager.review(
                    term,
                    approved=True,
                    reviewer=reviewer,
                    notes=notes,
                    lifecycle_version=lifecycle_version,
                    batch_id=batch_id,
                    target_risk_category=target_risk_category,
                    reviewed_at=review_date,
                    baseline_eval_version=baseline_eval_version,
                    post_eval_version=post_eval_version,
                    evaluation_gain=evaluation_gain or None,
                )
                reviewed_only_count += 1
            else:
                record = manager.nominate(term, normalized, evidence_ids)
                pending_count += 1
            _ = record
    records = [record.model_dump() for record in manager.list_records()]
    return {
        "status": "completed",
        "run_type": "slang_lifecycle_from_human_review_csv",
        "review_csv": str(target),
        "record_count": len(records),
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "pending_count": pending_count,
        "approved_reviewed_only_count": approved_reviewed_only_count,
        "reviewed_only_count": reviewed_only_count,
        "invalid_target_stage_count": len(invalid_target_stage_warnings),
        "invalid_target_stage_warnings": invalid_target_stage_warnings,
        "activation_blocked_count": len(activation_warnings),
        "activation_warnings": activation_warnings,
        "records": records,
        "runtime_ready_records": [
            record.model_dump()
            for record in manager.runtime_records(include_candidates=False, include_gray=True)
            if record.stage in {DynamicSlangLifecycleManager.GRAY_ROLLOUT, DynamicSlangLifecycleManager.ACTIVE}
        ],
        "lifecycle_flow": lifecycle_flow_metadata(),
        "claim_boundary": (
            "Only approved rows targeted to gray rollout or active become runtime-ready; approved rows targeted to "
            "reviewed, pending rows, and reviewed-only records remain excluded from runtime overlays."
        ),
    }


def lifecycle_flow_metadata() -> dict[str, Any]:
    """Describe the required analyst-controlled slang lifecycle for reports."""

    return {
        "stages": [
            {
                "stage": "candidate",
                "script_step": "mine_unknown_pending_rows",
                "runtime_ready": False,
            },
            {
                "stage": "human_review_csv",
                "script_step": "analyst_confirms_or_rejects_review_template",
                "runtime_ready": False,
            },
            {
                "stage": "gray_rollout",
                "manager_stage": DynamicSlangLifecycleManager.GRAY_ROLLOUT,
                "runtime_ready": True,
            },
            {
                "stage": "activate",
                "manager_stage": DynamicSlangLifecycleManager.ACTIVE,
                "runtime_ready": True,
            },
            {
                "stage": "evaluation_gain",
                "script_step": "compare_baseline_and_post_eval_reports",
                "runtime_ready": False,
            },
        ],
        "runtime_ready_policy": {
            "pending_candidates_runtime_ready": False,
            "include_candidates": False,
            "runtime_stages": [
                DynamicSlangLifecycleManager.GRAY_ROLLOUT,
                DynamicSlangLifecycleManager.ACTIVE,
            ],
        },
        "rerun_scripts": {
            "candidate_report": "python scripts/build_slang_candidate_report.py --records <records.jsonl> --classifications <classifications.jsonl>",
            "human_review_csv": "Edit data/manual_review/slang_candidate_review_template.csv review_status and eval report columns.",
            "lifecycle_export": "python scripts/build_slang_candidate_report.py --review-csv-out <review.csv> --lifecycle-out <lifecycle.json>",
        },
    }


def _target_stage_from_row(row: Mapping[str, Any]) -> tuple[str, str | None]:
    raw_value = str(row.get("target_stage") or "").strip()
    raw = raw_value.upper()
    if not raw:
        return DynamicSlangLifecycleManager.GRAY_ROLLOUT, None
    aliases = {
        "REVIEW": DynamicSlangLifecycleManager.REVIEWED,
        "REVIEWED": DynamicSlangLifecycleManager.REVIEWED,
        "HUMAN_REVIEW": DynamicSlangLifecycleManager.REVIEWED,
        "GRAY": DynamicSlangLifecycleManager.GRAY_ROLLOUT,
        "GREY": DynamicSlangLifecycleManager.GRAY_ROLLOUT,
        "GRAY_ROLLOUT": DynamicSlangLifecycleManager.GRAY_ROLLOUT,
        "GREY_ROLLOUT": DynamicSlangLifecycleManager.GRAY_ROLLOUT,
        "ACTIVE": DynamicSlangLifecycleManager.ACTIVE,
        "ACTIVATE": DynamicSlangLifecycleManager.ACTIVE,
    }
    if raw in aliases:
        return aliases[raw], None
    return DynamicSlangLifecycleManager.REVIEWED, raw_value


def lifecycle_manager_from_records(records: Iterable[Mapping[str, Any] | Any]) -> DynamicSlangLifecycleManager:
    return DynamicSlangLifecycleManager.from_records(records)


def slang_dictionary_update_from_lifecycle(
    records: Iterable[Mapping[str, Any] | Any],
    base_dictionary: Mapping[str, Any] | None = None,
    rules_version: str | None = None,
) -> dict[str, Any]:
    accepted_terms: list[dict[str, Any]] = []
    excluded_count = 0
    dictionary_patch: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    duplicate_runtime_term_conflicts: list[dict[str, Any]] = []
    first_runtime_terms: dict[str, dict[str, Any]] = {}
    base_terms = {str(term): str(normalized) for term, normalized in (base_dictionary or {}).items()}
    runtime_stages = {DynamicSlangLifecycleManager.GRAY_ROLLOUT, DynamicSlangLifecycleManager.ACTIVE}

    for raw_record in records:
        record = _lifecycle_record_mapping(raw_record)
        if record is None:
            excluded_count += 1
            continue
        stage = str(record.get("stage") or "").strip().upper()
        term = str(record.get("term") or "").strip()
        normalized = str(record.get("normalized_term") or term).strip() or term
        if stage not in runtime_stages or not term:
            excluded_count += 1
            continue
        accepted = {
            "term": term,
            "normalized_term": normalized,
            "stage": stage,
            "reviewer": record.get("reviewer"),
            "lifecycle_version": record.get("lifecycle_version"),
            "batch_id": record.get("batch_id"),
            "target_risk_category": record.get("target_risk_category"),
            "evidence_trace_ids": list(record.get("evidence_trace_ids") or []),
            "evaluation_gain": dict(record.get("evaluation_gain") or {}),
        }
        if term not in dictionary_patch:
            dictionary_patch[term] = normalized
            first_runtime_terms[term] = accepted
            if term in base_terms and base_terms[term] != normalized:
                conflicts.append({"term": term, "base_normalized_term": base_terms[term], "overlay_normalized_term": normalized})
        elif dictionary_patch[term] != normalized:
            kept = first_runtime_terms[term]
            duplicate_runtime_term_conflicts.append(
                {
                    "term": term,
                    "kept_normalized_term": kept["normalized_term"],
                    "conflicting_normalized_term": normalized,
                    "kept_stage": kept["stage"],
                    "conflicting_stage": stage,
                }
            )
        accepted_terms.append(accepted)

    derived_rules_version = rules_version or _rules_version_from_accepted_terms(accepted_terms)
    payload: dict[str, Any] = {
        "status": "completed" if accepted_terms else "no_runtime_terms",
        "run_type": "slang_dictionary_overlay_from_lifecycle",
        "rules_version": derived_rules_version,
        "dictionary_patch": {"slang_dictionary": dictionary_patch},
        "accepted_terms": accepted_terms,
        "accepted_record_count": len(accepted_terms),
        "unique_accepted_term_count": len(dictionary_patch),
        "excluded_record_count": excluded_count,
        "duplicate_runtime_term_conflicts": duplicate_runtime_term_conflicts,
        "claim_boundary": (
            "This is an overlay/export for reviewer-approved runtime slang terms; it does not automatically mutate "
            "config/slang_dictionary.yaml or production rule configuration."
        ),
    }
    if base_dictionary is not None:
        payload["base_dictionary_term_count"] = len(base_terms)
        payload["conflicts"] = conflicts
    return payload


def _lifecycle_record_mapping(record: Mapping[str, Any] | Any) -> dict[str, Any] | None:
    if isinstance(record, Mapping):
        return dict(record)
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else None
    if is_dataclass(record):
        dumped = asdict(record)
        return dict(dumped) if isinstance(dumped, Mapping) else None
    return None


def _rules_version_from_accepted_terms(accepted_terms: Iterable[Mapping[str, Any]]) -> str:
    for term in accepted_terms:
        evaluation_gain = term.get("evaluation_gain")
        if isinstance(evaluation_gain, Mapping):
            post_rule_version = str(evaluation_gain.get("post_rule_version") or "").strip()
            if post_rule_version:
                return post_rule_version
        lifecycle_version = str(term.get("lifecycle_version") or "").strip()
        if lifecycle_version:
            return lifecycle_version
    return f"slang-dictionary-overlay-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


EVAL_GAIN_METRICS = (
    "classification_f1",
    "primary_classification_f1",
    "secondary_classification_f1",
    "hierarchical_classification_f1",
    "entity_f1",
    "clue_f1",
    "classification_review_rate",
)


def evaluation_gain_from_reports(
    baseline: Mapping[str, Any] | str | Path | None,
    post: Mapping[str, Any] | str | Path | None,
) -> dict[str, Any]:
    baseline_report = parse_eval_report(baseline)
    post_report = parse_eval_report(post)
    if not baseline_report or not post_report:
        return {}

    gain: dict[str, Any] = {}
    baseline_rule_version = _eval_rule_version(baseline_report)
    post_rule_version = _eval_rule_version(post_report)
    if baseline_rule_version is not None:
        gain["baseline_rule_version"] = baseline_rule_version
    if post_rule_version is not None:
        gain["post_rule_version"] = post_rule_version
    for metric in EVAL_GAIN_METRICS:
        baseline_value = _numeric_metric(baseline_report.get(metric))
        post_value = _numeric_metric(post_report.get(metric))
        if baseline_value is None or post_value is None:
            continue
        gain[f"{metric}_delta"] = round(post_value - baseline_value, 6)
    return gain


def parse_eval_report(value: Mapping[str, Any] | str | Path | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    path = _project_path(text)
    if not path.exists() or not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _eval_rule_version(report: Mapping[str, Any]) -> str | None:
    value = report.get("rule_version")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _numeric_metric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    return numeric_value if math.isfinite(numeric_value) else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        load_jsonl(args.records),
        load_jsonl(args.classifications),
        min_count=args.min_count,
        max_candidates=args.max_candidates,
    )
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    review_csv = _project_path(args.review_csv_out)
    if not review_csv.exists():
        review_csv = write_review_csv(report, args.review_csv_out)
    lifecycle = lifecycle_records_from_review_csv(review_csv)
    lifecycle_out = _project_path(args.lifecycle_out)
    lifecycle_out.parent.mkdir(parents=True, exist_ok=True)
    dictionary_update_path = None
    if str(args.dictionary_update_out or "").strip():
        dictionary_update = slang_dictionary_update_from_lifecycle(lifecycle.get("records") or [])
        dictionary_update_path = _project_path(args.dictionary_update_out)
        dictionary_update_path.parent.mkdir(parents=True, exist_ok=True)
        dictionary_update_path.write_text(json.dumps(dictionary_update, ensure_ascii=False, indent=2), encoding="utf-8")
        lifecycle["dictionary_update"] = str(dictionary_update_path)
    lifecycle_out.write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2), encoding="utf-8")
    report["manual_review"]["review_csv"] = str(review_csv)
    report["manual_review"]["lifecycle_records"] = str(lifecycle_out)
    if dictionary_update_path is not None:
        report["manual_review"]["dictionary_update"] = str(dictionary_update_path)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


def _is_pending_classification(classification: Mapping[str, Any] | Any) -> bool:
    risk_category = str(get_record_field(classification, "risk_category") or "").strip()
    secondary_label = str(get_record_field(classification, "secondary_label") or "").strip()
    return (
        risk_category in UNKNOWN_CATEGORIES
        or secondary_label in PENDING_SECONDARY
        or bool(get_record_field(classification, "review_required"))
    )


def _candidate_terms(text: str, *, known_terms: set[str], context_markers: Iterable[str] = ()) -> Counter[str]:
    normalized = normalize_text(text)
    counter: Counter[str] = Counter()
    for cjk_run in re.findall(r"[\u4e00-\u9fff]{2,12}", normalized):
        for size in (2, 3, 4):
            if len(cjk_run) < size:
                continue
            for index in range(0, len(cjk_run) - size + 1):
                candidate = cjk_run[index : index + size]
                if _usable_candidate(candidate, known_terms=known_terms):
                    counter[candidate] += 1
    if list(context_markers):
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])", normalized):
            lowered = match.group(1).lower()
            if _usable_latin_candidate(lowered, known_terms=known_terms) and _latin_candidate_has_slang_shape(
                normalized,
                match.start(1),
                match.end(1),
            ):
                counter[lowered] += 1
    return counter


def _usable_candidate(term: str, *, known_terms: set[str]) -> bool:
    normalized = term.strip()
    lowered = normalized.lower()
    if len(normalized) < 2:
        return False
    if normalized.isdigit() or lowered in COMMON_STOP_TERMS:
        return False
    if normalized in known_terms or lowered in known_terms:
        return False
    if _contains_blocked_term(normalized, known_terms=known_terms):
        return False
    if any(char.isdigit() for char in normalized):
        return False
    return True


def _usable_latin_candidate(term: str, *, known_terms: set[str]) -> bool:
    lowered = term.strip().lower()
    if len(lowered) < 2:
        return False
    if lowered in COMMON_STOP_TERMS or lowered in known_terms:
        return False
    if lowered.isdigit() or any(char.isdigit() for char in lowered):
        return False
    if len(set(lowered)) == 1:
        return False
    if len(lowered) > 6:
        return False
    if _contains_blocked_term(lowered, known_terms=known_terms):
        return False
    return True


def _latin_candidate_has_slang_shape(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 1) : start]
    after = text[end : end + 1]
    if _contains_cjk(before) or _contains_cjk(after):
        return True
    local = text[max(0, start - 8) : min(len(text), end + 8)]
    return bool(_contains_cjk(local) and re.search(r"[@:+＋➕]|低价|价格|接单|私聊|联系|暗号|上车", local))


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def _contains_blocked_term(term: str, *, known_terms: set[str]) -> bool:
    lowered = term.lower()
    blocked_terms = [
        candidate
        for candidate in known_terms
        if len(candidate) >= 2 and not re.search(r"[A-Za-z0-9]", candidate)
    ]
    return any(candidate in term or candidate.lower() in lowered for candidate in blocked_terms)


def _known_terms() -> set[str]:
    registry = RuleRegistry()
    terms: set[str] = set()
    for raw, normalized in registry.load_slang_dictionary().items():
        terms.add(str(raw))
        terms.add(str(normalized))
        terms.add(str(raw).lower())
        terms.add(str(normalized).lower())
    for label, values in registry.risk_marker_sets().items():
        terms.add(str(label))
        terms.add(str(label).lower())
        for value in values:
            terms.add(str(value))
            terms.add(str(value).lower())
    for label in registry.labels():
        terms.add(str(label))
        terms.add(str(label).lower())
    for _label, values in registry.promotion_markers_by_label().items():
        for value in values:
            terms.add(str(value))
            terms.add(str(value).lower())
    classifier_policy = registry.classifier_policy()
    marker_groups = classifier_policy.get("promotion_marker_groups") if isinstance(classifier_policy, Mapping) else {}
    if isinstance(marker_groups, Mapping):
        for values in marker_groups.values():
            if isinstance(values, list):
                for value in values:
                    terms.add(str(value))
                    terms.add(str(value).lower())
    for value in [*CONTACT_MARKERS, *ACTION_MARKERS]:
        terms.add(str(value))
        terms.add(str(value).lower())
    terms.update(COMMON_STOP_TERMS)
    return {term for term in terms if term}


def _context_markers(text: str) -> list[str]:
    lowered = text.lower()
    markers: list[str] = []
    if any(marker.lower() in lowered for marker in CONTACT_MARKERS):
        markers.append("contact_or_call_to_action")
    if any(marker.lower() in lowered for marker in ACTION_MARKERS):
        markers.append("transaction_or_task_context")
    return markers


def _trace_id(record: Mapping[str, Any] | Any) -> str:
    return str(
        get_record_field(record, "source_trace_id")
        or get_record_field(record, "trace_id")
        or get_record_field(record, "hash_id")
        or ""
    )


def _record_text(record: Mapping[str, Any] | Any) -> str:
    return normalize_text(str(get_record_field(record, "content_text") or get_record_field(record, "clean_text") or ""))


def _excerpt_around(text: str, term: str, *, window: int = 60) -> str:
    normalized = normalize_text(text)
    index = normalized.find(term)
    if index < 0:
        return normalized[: window * 2]
    start = max(0, index - window)
    end = min(len(normalized), index + len(term) + window)
    return normalized[start:end]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
