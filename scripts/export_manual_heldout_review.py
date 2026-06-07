"""Export seeded held-out rows into an analyst-friendly review package.

This does not create human labels.  It creates the CSV/README handoff an
analyst can fill, then ``validate_manual_heldout.py --review-csv`` can convert
confirmed rows back into defense-ready manual held-out JSONL.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


CSV_FIELDS = [
    "source_trace_id",
    "source_name",
    "source_type",
    "source_url",
    "legal_basis",
    "content_modality",
    "seed_expected_risk_categories",
    "seed_expected_secondary_labels",
    "seed_expected_entities",
    "matched_keywords",
    "matched_themes",
    "text_excerpt",
    "status",
    "annotator",
    "review_date",
    "final_risk_categories",
    "final_secondary_labels",
    "conflict_handling",
    "typical_error",
    "notes",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export BlackAgent held-out rows for human review.")
    parser.add_argument("--input", default="tests/evaluation/heldout_classification.jsonl", help="Seeded held-out JSONL.")
    parser.add_argument("--output", default="data/manual_review/heldout_review_task.csv", help="CSV task file for analysts.")
    parser.add_argument("--readme", default="data/manual_review/README.md", help="Review instructions to write.")
    parser.add_argument("--report", default="data/manual_review/heldout_review_task_report.json", help="Task package report JSON.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum rows to export.")
    parser.add_argument("--min-target", type=int, default=100, help="Minimum rows expected to be confirmed.")
    return parser.parse_args(argv)


def export_rows(records: Iterable[Mapping[str, Any]], *, limit: int = 100) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in list(records)[: max(1, int(limit))]:
        review = record.get("human_review") if isinstance(record.get("human_review"), Mapping) else {}
        rows.append(
            {
                "source_trace_id": str(record.get("source_trace_id") or record.get("trace_id") or ""),
                "source_name": str(record.get("source_name") or ""),
                "source_type": str(record.get("source_type") or ""),
                "source_url": str(record.get("source_url") or ""),
                "legal_basis": str(record.get("legal_basis") or ""),
                "content_modality": str(record.get("content_modality") or "text"),
                "seed_expected_risk_categories": _join(record.get("expected_risk_categories")),
                "seed_expected_secondary_labels": _join(record.get("expected_secondary_labels")),
                "seed_expected_entities": json.dumps(record.get("expected_entities") or [], ensure_ascii=False),
                "matched_keywords": _join(record.get("matched_keywords")),
                "matched_themes": _join(record.get("matched_themes")),
                "text_excerpt": _excerpt(record.get("content_text")),
                "status": str(review.get("status") or "pending_human_confirmation"),
                "annotator": str(review.get("annotator") or ""),
                "review_date": str(review.get("review_date") or ""),
                "final_risk_categories": _join(review.get("final_risk_categories")),
                "final_secondary_labels": _join(review.get("final_secondary_labels")),
                "conflict_handling": str(review.get("conflict_handling") or ""),
                "typical_error": str(review.get("typical_error") or ""),
                "notes": str(review.get("notes") or ""),
            }
        )
    return rows


def build_report(rows: list[dict[str, str]], *, output: str | Path, min_target: int = 50) -> dict[str, Any]:
    return {
        "status": "ready_for_human_review" if len(rows) >= max(1, int(min_target)) else "insufficient_review_rows",
        "run_type": "export_manual_heldout_review_task",
        "output": str(_project_path(output).relative_to(PROJECT_ROOT)),
        "row_count": len(rows),
        "min_target_confirmed_rows": max(1, int(min_target)),
        "source_type_counts": dict(Counter(row["source_type"] or "unknown" for row in rows)),
        "content_modality_counts": dict(Counter(row["content_modality"] or "unknown" for row in rows)),
        "required_review_fields": [
            "status",
            "annotator",
            "review_date",
            "final_risk_categories",
            "conflict_handling",
            "typical_error",
        ],
        "accepted_status_values": ["confirmed", "corrected", "rejected", "pending_human_confirmation"],
        "manual_gold_claim": {
            "can_claim_manual_gold": False,
            "claim_status": "review_package_only",
            "required_next_step": (
                "Analysts must fill status/annotator/review_date/final labels/conflict fields, "
                "then run scripts/validate_manual_heldout.py before claiming manual gold."
            ),
        },
        "claim_boundary": "This package is ready for a human analyst; it is not itself a human-confirmed held-out set.",
    }


def write_csv(rows: Iterable[Mapping[str, str]], path: str | Path) -> Path:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    return target


def write_readme(path: str | Path, *, csv_path: str | Path, min_target: int) -> Path:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    csv_rel = _project_path(csv_path).relative_to(PROJECT_ROOT)
    content = f"""# BlackAgent held-out 人工复核任务包

此目录用于把规则 seeded held-out 升级为可答辩的人工确认 held-out。

## 复核步骤

1. 打开 `{csv_rel}`。
2. 每行阅读 `text_excerpt`、来源、seed 标签和实体。
3. 至少确认 `{min_target}` 行，把 `status` 改为：
   - `confirmed`：seed 标签完全正确；
   - `corrected`：seed 标签需要修正，并填写 final 字段；
   - `rejected`：样本不应进入人工 held-out；
   - `pending_human_confirmation`：暂不确认。
4. 必填字段：
   - `annotator`
   - `review_date`，建议格式 `YYYY-MM-DD`
   - `final_risk_categories`，多个标签用分号分隔
   - `final_secondary_labels`，无二级标签可留空
   - `conflict_handling`，例如 `seed_confirmed`、`secondary_corrected`、`ambiguous_rejected`
   - `typical_error`，无误判填 `none`
5. 回收命令：

```powershell
python scripts/validate_manual_heldout.py `
  --input tests/evaluation/heldout_classification.jsonl `
  --review-csv {csv_rel} `
  --output tests/evaluation/manual_heldout_classification.jsonl `
  --report data/manual_heldout_report.json `
  --min-records {min_target}
```

只有验证器通过后，才能把输出称为 `manual_heldout_public_authorized`。
"""
    target.write_text(content, encoding="utf-8")
    return target


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(path)
    records: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                records.append(json.loads(line))
    return records


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = export_rows(load_jsonl(args.input), limit=args.limit)
    csv_path = write_csv(rows, args.output)
    readme_path = write_readme(args.readme, csv_path=csv_path, min_target=args.min_target)
    report = build_report(rows, output=csv_path, min_target=args.min_target)
    report["readme"] = str(readme_path.relative_to(PROJECT_ROOT))
    report_path = _project_path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready_for_human_review" else 1


def _join(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Iterable):
        return ";".join(str(item) for item in value if str(item).strip())
    return str(value)


def _excerpt(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
