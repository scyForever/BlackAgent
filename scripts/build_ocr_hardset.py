"""Build a reproducible OCR/image-text hard set with analyst labels.

The hard set is intentionally local and reviewable: demo PBM posters prove the
OCR adapter can read pixels without external dependencies, while each row also
contains manual target labels for contact, link, slang, and tool-name text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable
import re

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ocr import BitmapGlyphOCREngine, OCRImageTextAdapter, render_demo_pbm


SEED_CASES = (
    {"text": "TG:OCR001", "caption": "海报：群控脚本 接码 hxxps://ocr001.example/tool", "risk": "工具交易", "secondary": "群控脚本"},
    {"text": "TG:OCR002", "caption": "截图：私域导流 返利 拉新 hxxps://ocr002.example/lead", "risk": "诈骗引流", "secondary": "私域导流"},
    {"text": "TG:OCR003", "caption": "海报：接码注册 实名号 出号 2U", "risk": "账号交易", "secondary": "接码注册"},
    {"text": "TG:OCR004", "caption": "截图：群发广告 代投 包量 客户", "risk": "众包服务", "secondary": "代投服务"},
    {"text": "TG:OCR005", "caption": "海报：卡单 支付失败 下单 补发", "risk": "刷单作弊", "secondary": "订单卡单"},
    {"text": "TG:OCR006", "caption": "截图：云控脚本 后台 卡密 下载", "risk": "工具交易", "secondary": "群控脚本"},
    {"text": "TG:OCR007", "caption": "海报：飞机号 协议号 自助取号", "risk": "账号交易", "secondary": "账号养号"},
    {"text": "TG:OCR008", "caption": "截图：拉群 秒拉 不封 业务联系", "risk": "众包服务", "secondary": "拉群获客"},
    {"text": "TG:OCR009", "caption": "海报：跑分代付 流水 进群", "risk": "诈骗引流", "secondary": "跑分代付"},
    {"text": "TG:OCR010", "caption": "截图：手工单 平台单 日结", "risk": "刷单作弊", "secondary": "手工做单"},
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build BlackAgent OCR/image-text hard set.")
    parser.add_argument("--output", default="tests/evaluation/ocr_image_text_hardset.jsonl")
    parser.add_argument("--image-dir", default="data/ocr_hardset_images")
    parser.add_argument("--report", default="data/ocr_hardset_report.json")
    parser.add_argument("--count", type=int, default=20, help="Number of hard-set rows to generate.")
    return parser.parse_args(argv)


def build_records(*, count: int = 20, image_dir: str | Path = "data/ocr_hardset_images") -> list[dict[str, Any]]:
    adapter = OCRImageTextAdapter(engine=BitmapGlyphOCREngine())
    target_dir = _project_path(image_dir)
    records: list[dict[str, Any]] = []
    for index in range(max(1, int(count))):
        seed = SEED_CASES[index % len(SEED_CASES)]
        variant = index // len(SEED_CASES) + 1
        text = str(seed["text"])
        image_path = render_demo_pbm(text, target_dir / f"ocr_hard_{index + 1:03d}.pbm")
        caption = f"{seed['caption']} 变体{variant} 暗号 code:{index + 1:03d}"
        raw_record = {
            "trace_id": f"ocr-hard-{index + 1:03d}",
            "source_name": "local-authorized-ocr-hardset",
            "source_type": "Image",
            "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            "caption": caption,
            "image_path": str(image_path),
        }
        ocr = adapter.extract(raw_record)
        manual_labels = _manual_labels(text=text, caption=caption)
        records.append(
            {
                **raw_record,
                "source_trace_id": raw_record["trace_id"],
                "content_text": ocr.text,
                "ocr_text": ocr.text,
                "content_modality": ocr.content_modality,
                "ocr_status": ocr.status,
                "ocr_sources": ocr.sources,
                "ocr_engine_outputs": ocr.engine_outputs,
                "ocr_confidence": ocr.ocr_confidence,
                "ocr_engine_confidences": ocr.ocr_engine_confidences,
                "ocr_confidence_details": ocr.ocr_confidence_details,
                "expected_image_text": text,
                "expected_risk_categories": [seed["risk"]],
                "expected_secondary_labels": [seed["secondary"]],
                "expected_entities": _expected_entities(manual_labels),
                "manual_labels": manual_labels,
                "claim_boundary": (
                    "Local authorized OCR hard-set row; PBM pixels are generated for deterministic regression "
                    "and do not claim external production OCR coverage."
                ),
            }
        )
    return records


def build_report(records: list[dict[str, Any]], *, output_path: str | Path) -> dict[str, Any]:
    return {
        "status": "completed" if len(records) >= 20 else "insufficient_records",
        "run_type": "build_ocr_image_text_hardset",
        "output": str(_project_path(output_path).relative_to(PROJECT_ROOT)),
        "record_count": len(records),
        "content_modality_counts": _counts(record.get("content_modality") for record in records),
        "risk_category_counts": _counts((record.get("expected_risk_categories") or ["unknown"])[0] for record in records),
        "labeled_fields": ["contact", "links", "slang", "tool_names"],
        "expected_entity_type_counts": _counts(
            entity.get("entity_type")
            for record in records
            for entity in record.get("expected_entities", [])
            if isinstance(entity, dict)
        ),
        "ocr_status_counts": _counts(record.get("ocr_status") for record in records),
        "ocr_engine_comparison": {
            "configured_engines": sorted(
                {
                    str(source).removeprefix("ocr_engine.")
                    for record in records
                    for source in record.get("ocr_sources", [])
                    if str(source).startswith("ocr_engine.")
                }
            ),
            "optional_operator_engines": ["TesseractCliOCREngine", "cloud_ocr_callable"],
            "comparison_contract": "OCRImageTextAdapter accepts multiple named engines and records per-engine outputs.",
        },
        "claim_boundary": (
            "This hard set validates the image-text contract and deterministic pixel OCR path; production OCR quality "
            "still depends on the injected external engine and separately authorized screenshots/posters."
        ),
    }


def write_jsonl(records: Iterable[dict[str, Any]], path: str | Path) -> Path:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file_obj:
        for record in records:
            file_obj.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return target


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = build_records(count=args.count, image_dir=args.image_dir)
    output = write_jsonl(records, args.output)
    report = build_report(records, output_path=output)
    report_path = _project_path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _counts(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _manual_labels(*, text: str, caption: str) -> dict[str, Any]:
    links = [_canonical_url(part) for part in re.findall(r"hxxps?://[^\s，。；;]+|https?://[^\s，。；;]+", caption)]
    code_match = re.search(r"code[:：](\d{3,})", caption, flags=re.IGNORECASE)
    tool_terms = [
        term
        for term in ("群控", "脚本", "接码平台", "接码", "云控", "卡密", "跑分平台")
        if term in caption
    ]
    settlement_terms = [term for term in ("跑分", "代付", "USDT", "支付宝", "银行卡") if term in caption]
    return {
        "contact": text,
        "links": links,
        "slang": ["飞机"] if "飞机" in caption else ["TG"],
        "tool_names": tool_terms,
        "settlement_terms": settlement_terms,
        "invite_codes": [code_match.group(1)] if code_match else [],
        "annotator": "local_seed_fixture",
        "review_date": "2026-06-03",
        "conflict_handling": "seeded_ocr_hardset_for_regression",
    }


def _expected_entities(labels: dict[str, Any]) -> list[dict[str, str]]:
    contact = str(labels.get("contact") or "")
    contact_value = contact.split(":", 1)[-1] if ":" in contact else contact
    entities: list[dict[str, str]] = []
    if contact_value:
        entities.append({"entity_type": "contact", "normalized_value": contact_value})
    for link in labels.get("links") or []:
        entities.append({"entity_type": "url", "normalized_value": str(link)})
    for tool in labels.get("tool_names") or []:
        entities.append({"entity_type": "tool_name", "normalized_value": str(tool)})
    for value in labels.get("settlement_terms") or []:
        entities.append({"entity_type": "settlement", "normalized_value": str(value)})
    for value in labels.get("invite_codes") or []:
        entities.append({"entity_type": "invite_code", "normalized_value": str(value)})
    return _dedupe_entities(entities)


def _dedupe_entities(entities: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, str]] = []
    for entity in entities:
        key = (str(entity.get("entity_type") or ""), str(entity.get("normalized_value") or ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        output.append({"entity_type": key[0], "normalized_value": key[1]})
    return output


def _canonical_url(value: str) -> str:
    return str(value).replace("hxxps://", "https://").replace("hxxp://", "http://").strip(" ,，。；;")


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
