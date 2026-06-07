"""Mine unknown/review-required rows into slang candidates for human review."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
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
        }
    manager = DynamicSlangLifecycleManager()
    approved_count = 0
    rejected_count = 0
    pending_count = 0
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
            record = manager.nominate(term, normalized, evidence_ids)
            reviewer = str(row.get("reviewer") or "human_review").strip() or "human_review"
            notes = str(row.get("notes") or "").strip() or None
            if status in {"approved", "approve", "reviewed", "accepted"}:
                record = manager.review(term, approved=True, reviewer=reviewer, notes=notes)
                approved_count += 1
            elif status in {"rejected", "reject", "denied"}:
                record = manager.review(term, approved=False, reviewer=reviewer, notes=notes)
                rejected_count += 1
            else:
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
        "records": records,
        "runtime_ready_records": [
            record.model_dump()
            for record in manager.runtime_records(include_candidates=False)
        ],
        "claim_boundary": (
            "Only approved/reviewed records are runtime-ready; pending candidates remain excluded from active slang rules."
        ),
    }


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
    review_csv = write_review_csv(report, args.review_csv_out)
    lifecycle = lifecycle_records_from_review_csv(review_csv)
    lifecycle_out = _project_path(args.lifecycle_out)
    lifecycle_out.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_out.write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2), encoding="utf-8")
    report["manual_review"]["review_csv"] = str(review_csv)
    report["manual_review"]["lifecycle_records"] = str(lifecycle_out)
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
