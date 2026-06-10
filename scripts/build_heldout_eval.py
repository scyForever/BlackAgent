"""Build a deterministic held-out classification split from local authorized corpora.

The script intentionally uses only already-local public/authorized records.  It
creates a reviewable JSONL split that is independent from the synthetic gold
files used by unit tests; teams can manually confirm or edit the expected labels
before using the report in a defense.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.enhancement.text_intelligence import AdvancedEntityExtractor, FineGrainedIntentClassifier

try:
    from src.collector.source_metadata import source_quota_groups_for_record
except ImportError:  # pragma: no cover - compatibility for stripped-down script use.
    source_quota_groups_for_record = None


REQUIRED_HOLDOUT_SOURCE_GROUPS = (
    "real_telegram",
    "secondhand_market",
    "crowdsourcing_platform",
    "public_account_or_article",
)

TIME_HOLDOUT_BUCKETS = ("recent_0_7d", "mid_8_30d", "older_31d_plus", "missing_time")

SLANG_FAMILY_PATTERNS = {
    "telegram_alias": re.compile(
        r"(telegram|t\.me|纸飞机|电报|(?:(?<=^)|(?<=[^a-z0-9])|(?<=[\u4e00-\u9fff]))tg(?=$|[^a-z0-9]|[\u4e00-\u9fff]))",
        re.IGNORECASE,
    ),
    "wechat_alias": re.compile(
        r"(wechat|微信|加v|加微|加薇|加威|加围|➕v|(?:(?<=^)|(?<=[^a-z0-9])|(?<=[\u4e00-\u9fff]))(?:vx|wx)(?=$|[^a-z0-9]|[\u4e00-\u9fff]))",
        re.IGNORECASE,
    ),
    "sms_code_alias": re.compile(r"(短信验证码|验证码|接码|\bsms\b|取号)", re.IGNORECASE),
    "group_alias": re.compile(r"(拉群|群发|进群|群聊|吃瓜群)", re.IGNORECASE),
    "account_material_alias": re.compile(r"(账号资料|实名号|协议号|session|tdata|卡密)", re.IGNORECASE),
    "unknown_new_slang": re.compile(r"(新暗语|未知新黑话|暂无归类|待研判|unknown|new slang|蓝标货)", re.IGNORECASE),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create BlackAgent's local public/authorized held-out eval split.")
    parser.add_argument("--input", default="data/cleaning_phase_cleaned_corpus.jsonl", help="Local cleaned JSONL corpus.")
    parser.add_argument("--output", default="tests/evaluation/heldout_classification.jsonl", help="Held-out JSONL to write.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum records to write.")
    parser.add_argument("--per-category", type=int, default=50, help="Maximum records per primary category.")
    return parser.parse_args(argv)


def build_heldout_records(
    records: Iterable[dict[str, Any]],
    *,
    limit: int = 60,
    per_category: int = 12,
) -> list[dict[str, Any]]:
    classifier = FineGrainedIntentClassifier()
    extractor = AdvancedEntityExtractor()
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in records:
        content = str(raw.get("clean_text") or raw.get("content_text") or "").strip()
        if not content:
            continue
        classification = classifier.classify({**raw, "content_text": content}).model_dump()
        category = str(classification.get("risk_category") or "unknown")
        entities = [
            item.model_dump()
            for item in extractor.extract({**raw, "content_text": content, "classification": classification})
        ]
        buckets[category].append(
            {
                "source_trace_id": str(raw.get("source_trace_id") or raw.get("trace_id") or raw.get("clean_id")),
                "trace_id": str(raw.get("source_trace_id") or raw.get("trace_id") or raw.get("clean_id")),
                "source_name": raw.get("source_name"),
                "source_type": raw.get("source_type"),
                "source_url": raw.get("source_url"),
                "legal_basis": raw.get("legal_basis") or "PUBLIC_COMPLIANT_DATA",
                "content_text": content,
                "content_modality": raw.get("content_modality") or ("image_text" if "image" in content.lower() else "text"),
                "matched_keywords": raw.get("matched_keywords") or [],
                "matched_themes": raw.get("matched_themes") or [],
                "expected_risk_categories": [category],
                "expected_secondary_labels": [classification.get("secondary_label")]
                if classification.get("secondary_label") not in {None, "", "待研判", "未细分"}
                else [],
                "expected_entities": [
                    {
                        "entity_type": entity.get("entity_type"),
                        "normalized_value": entity.get("normalized_value"),
                    }
                    for entity in entities[:8]
                    if entity.get("normalized_value")
                ],
                "dataset_name": "blackagent_local_public_authorized_heldout_v1",
                "dataset_kind": "heldout_public_authorized_seed",
                "holdout_split": "p0_p2_2026_06",
                "annotation_source": "seeded_from_local_authorized_corpus_for_manual_review",
                "annotation_note": (
                    "Expected labels are deterministically seeded from current rules; "
                    "human analysts should confirm before making online-generalization claims."
                ),
                "human_review": {
                    "status": "pending_human_confirmation",
                    "annotator": "",
                    "review_date": "",
                    "final_risk_categories": [],
                    "final_secondary_labels": [],
                    "conflict_handling": "",
                    "typical_error": "",
                    "notes": "",
                },
            }
        )

    selected: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    secondary_seen: set[tuple[str, str]] = set()
    # First pass: maximize secondary-label coverage.
    for category in sorted(buckets):
        for record in buckets[category]:
            secondary = next(iter(record.get("expected_secondary_labels") or [""]), "")
            key = (category, str(secondary))
            if key in secondary_seen:
                continue
            if category_counts[category] >= max(1, per_category):
                continue
            selected.append(record)
            secondary_seen.add(key)
            category_counts[category] += 1
            if len(selected) >= limit:
                return selected
    # Second pass: fill remaining category quotas deterministically.
    selected_ids = {id(record) for record in selected}
    while len(selected) < limit:
        added = False
        for category in sorted(buckets):
            if category_counts[category] >= max(1, per_category):
                continue
            for record in buckets[category]:
                if id(record) in selected_ids:
                    continue
                selected.append(record)
                selected_ids.add(id(record))
                category_counts[category] += 1
                added = True
                break
            if len(selected) >= limit:
                return selected
        if not added:
            break
    return selected


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(path)
    output: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                output.append(json.loads(line))
    return output


def write_jsonl(records: Iterable[dict[str, Any]], path: str | Path) -> Path:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file_obj:
        for record in records:
            file_obj.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return target


def build_holdout_coverage_report(records: list[dict[str, Any]], output_path: str | Path) -> dict[str, Any]:
    source_counts: Counter[str] = Counter()
    timestamps = [_record_time(record) for record in records]
    reference_time = max((item for item in timestamps if item is not None), default=None)
    time_counts: Counter[str] = Counter({bucket: 0 for bucket in TIME_HOLDOUT_BUCKETS})
    slang_counts: Counter[str] = Counter({family: 0 for family in SLANG_FAMILY_PATTERNS})

    for record, timestamp in zip(records, timestamps):
        source_counts.update(_source_holdout_groups(record))
        time_counts[_time_bucket(timestamp, reference_time)] += 1
        text = _slang_detection_text(record)
        for family, pattern in SLANG_FAMILY_PATTERNS.items():
            if pattern.search(text):
                slang_counts[family] += 1

    covered = sorted(group for group in REQUIRED_HOLDOUT_SOURCE_GROUPS if source_counts.get(group, 0) > 0)
    missing = sorted(group for group in REQUIRED_HOLDOUT_SOURCE_GROUPS if source_counts.get(group, 0) == 0)
    return {
        "output": _report_path(output_path),
        "source_holdout": {
            "required_groups": list(REQUIRED_HOLDOUT_SOURCE_GROUPS),
            "counts": dict(sorted(source_counts.items())),
            "covered_required_groups": covered,
            "missing_required_groups": missing,
        },
        "time_holdout": {
            "reference_time": reference_time.isoformat() if reference_time else None,
            "bucket_counts": {bucket: time_counts[bucket] for bucket in TIME_HOLDOUT_BUCKETS},
        },
        "slang_family_holdout": {
            "family_counts": {family: slang_counts[family] for family in SLANG_FAMILY_PATTERNS},
        },
        "claim_boundary": (
            "Holdout coverage describes source, time, and slang-family balance in this local seeded split; "
            "it does not prove live online generalization without human confirmation and fresh external validation."
        ),
    }


def build_report(records: list[dict[str, Any]], *, output_path: str | Path) -> dict[str, Any]:
    human_review_statuses = Counter(
        str((record.get("human_review") or {}).get("status") or "missing_human_review")
        for record in records
    )
    return {
        "status": "completed",
        "run_type": "build_local_public_authorized_heldout",
        "output": _report_path(output_path),
        "record_count": len(records),
        "category_counts": dict(Counter(next(iter(record["expected_risk_categories"]), "unknown") for record in records)),
        "secondary_label_counts": dict(Counter((record.get("expected_secondary_labels") or ["未细分"])[0] for record in records)),
        "source_type_counts": dict(Counter(str(record.get("source_type") or "unknown") for record in records)),
        "holdout_dimensions": build_holdout_coverage_report(records, output_path),
        "human_review": {
            "status_counts": dict(human_review_statuses),
            "required_fields": [
                "human_review.annotator",
                "human_review.review_date",
                "human_review.final_risk_categories",
                "human_review.conflict_handling",
                "human_review.typical_error",
            ],
            "finalize_command": (
                "python scripts/validate_manual_heldout.py "
                "--input tests/evaluation/heldout_classification.jsonl "
                "--review-csv data/manual_review/heldout_review_task.csv "
                "--output tests/evaluation/manual_heldout_classification.jsonl "
                "--min-records 100"
            ),
        },
        "review_bucket_policy": (
            "Includes positive risk rows plus normal-noise and unknown/review buckets so analysts can "
            "measure false positives, residual unknowns, and review-load reductions."
        ),
        "claim_boundary": (
            "This is an independent local public/authorized held-out split seeded for analyst review; "
            "do not present it as live online generalization without human confirmation and fresh external validation."
        ),
    }


def _source_holdout_groups(record: dict[str, Any]) -> tuple[str, ...]:
    groups: list[str] = []
    if source_quota_groups_for_record is not None:
        groups.extend(
            group
            for group in source_quota_groups_for_record(record)
            if str(group).strip().lower() != "real_telegram"
        )
    if _is_telegram_record(record):
        groups.append("real_telegram")
    return tuple(dict.fromkeys(group for group in groups if group))


def _report_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(target)


def _is_telegram_record(record: dict[str, Any]) -> bool:
    aliases = {"telegram", "tg", "telegram_channel", "telegram_group"}
    for field in ("platform", "source", "source_type"):
        if str(record.get(field) or "").strip().lower() in aliases:
            return True

    source_name = str(record.get("source_name") or "").strip().lower()
    if (
        source_name.startswith(("telegram_", "tg_"))
        or "telegram_public_delivery" in source_name
        or re.search(r"(?:(?<=^)|(?<=[^a-z0-9])|(?<=[\u4e00-\u9fff]))tg(?=$|[^a-z0-9]|[\u4e00-\u9fff])", source_name)
    ):
        return True

    for field in ("source_url", "url"):
        parsed = urlparse(str(record.get(field) or ""))
        host = (parsed.hostname or "").lower()
        if host in {"t.me", "telegram.me", "telegram.org"} or host.endswith(".telegram.org"):
            return True
    return False


def _record_time(record: dict[str, Any]) -> datetime | None:
    parsed = [
        _parse_timestamp(record.get(field))
        for field in ("publish_time", "crawl_time", "collection_time", "timestamp", "created_at")
    ]
    return max((item for item in parsed if item is not None), default=None)


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _time_bucket(timestamp: datetime | None, reference_time: datetime | None) -> str:
    if timestamp is None or reference_time is None:
        return "missing_time"
    age_days = (reference_time - timestamp).total_seconds() / 86400
    if age_days <= 7:
        return "recent_0_7d"
    if age_days <= 30:
        return "mid_8_30d"
    return "older_31d_plus"


def _slang_detection_text(record: dict[str, Any]) -> str:
    values: list[str] = []
    for field in (
        "content_text",
        "matched_keywords",
        "matched_themes",
        "expected_entities",
        "expected_secondary_labels",
        "evidence",
    ):
        values.extend(_flatten_text(record.get(field)))
    return " ".join(values)


def _flatten_text(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        output: list[str] = []
        for item in value.values():
            output.extend(_flatten_text(item))
        return output
    if isinstance(value, Iterable):
        output = []
        for item in value:
            output.extend(_flatten_text(item))
        return output
    return [str(value)]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = build_heldout_records(load_jsonl(args.input), limit=args.limit, per_category=args.per_category)
    output = write_jsonl(records, args.output)
    report = build_report(records, output_path=output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if records else 1


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
