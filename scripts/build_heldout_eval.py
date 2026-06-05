"""Build a deterministic held-out classification split from local authorized corpora.

The script intentionally uses only already-local public/authorized records.  It
creates a reviewable JSONL split that is independent from the synthetic gold
files used by unit tests; teams can manually confirm or edit the expected labels
before using the report in a defense.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.enhancement.text_intelligence import AdvancedEntityExtractor, FineGrainedIntentClassifier


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create BlackAgent's local public/authorized held-out eval split.")
    parser.add_argument("--input", default="data/cleaning_phase_high_risk_corpus.jsonl", help="Local cleaned JSONL corpus.")
    parser.add_argument("--output", default="tests/evaluation/heldout_classification.jsonl", help="Held-out JSONL to write.")
    parser.add_argument("--limit", type=int, default=60, help="Maximum records to write.")
    parser.add_argument("--per-category", type=int, default=12, help="Maximum records per primary category.")
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
        if category in {"unknown", "正常业务白噪声"}:
            continue
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
    for category in sorted(buckets):
        for record in buckets[category]:
            if record in selected or category_counts[category] >= max(1, per_category):
                continue
            selected.append(record)
            category_counts[category] += 1
            if len(selected) >= limit:
                return selected
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


def build_report(records: list[dict[str, Any]], *, output_path: str | Path) -> dict[str, Any]:
    human_review_statuses = Counter(
        str((record.get("human_review") or {}).get("status") or "missing_human_review")
        for record in records
    )
    return {
        "status": "completed",
        "run_type": "build_local_public_authorized_heldout",
        "output": str(_project_path(output_path).relative_to(PROJECT_ROOT)),
        "record_count": len(records),
        "category_counts": dict(Counter(next(iter(record["expected_risk_categories"]), "unknown") for record in records)),
        "secondary_label_counts": dict(Counter((record.get("expected_secondary_labels") or ["未细分"])[0] for record in records)),
        "source_type_counts": dict(Counter(str(record.get("source_type") or "unknown") for record in records)),
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
                "--output tests/evaluation/manual_heldout_classification.jsonl"
            ),
        },
        "claim_boundary": (
            "This is an independent local public/authorized held-out split seeded for analyst review; "
            "do not present it as live online generalization without human confirmation and fresh external validation."
        ),
    }


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
