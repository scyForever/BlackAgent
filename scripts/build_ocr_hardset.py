"""Build a reproducible OCR/image-text hard set with analyst labels.

The hard set is intentionally local and reviewable: demo PBM posters prove the
OCR adapter can read pixels without external dependencies, while each row also
contains manual target labels for contact, link, slang, and tool-name text.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping
import re
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ocr import BitmapGlyphOCREngine, OCRImageTextAdapter, TesseractCliOCREngine, render_demo_pbm

DEFAULT_OCR_ENGINE_PROVIDER = "bitmap_glyph"
OCR_ENGINE_CHOICES = ("bitmap_glyph", "tesseract", "bitmap_glyph,tesseract")


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
    parser.add_argument(
        "--manifest",
        default=None,
        help="Optional JSONL manifest of authorized real screenshot/poster rows to materialize through OCRImageTextAdapter.",
    )
    parser.add_argument(
        "--ocr-engine",
        default=DEFAULT_OCR_ENGINE_PROVIDER,
        choices=OCR_ENGINE_CHOICES,
        help="OCR engine(s) to evaluate on image paths; use tesseract to exercise the external CLI wrapper.",
    )
    parser.add_argument("--tesseract-executable", default="tesseract", help="Tesseract executable name/path.")
    parser.add_argument("--tesseract-language", default="chi_sim+eng", help="Tesseract language setting.")
    parser.add_argument("--tessdata-dir", default="", help="Optional Tesseract tessdata directory for TESSDATA_PREFIX.")
    parser.add_argument("--count", type=int, default=20, help="Number of hard-set rows to generate.")
    return parser.parse_args(argv)


def build_records(
    *,
    count: int = 20,
    image_dir: str | Path = "data/ocr_hardset_images",
    ocr_engine: str = DEFAULT_OCR_ENGINE_PROVIDER,
    tesseract_executable: str = "tesseract",
    tesseract_language: str = "chi_sim+eng",
    tessdata_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    adapter = _adapter_for_engine(
        ocr_engine=ocr_engine,
        tesseract_executable=tesseract_executable,
        tesseract_language=tesseract_language,
        tessdata_dir=tessdata_dir,
    )
    engine_provider = _provider_label(ocr_engine)
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
                "ocr_errors": ocr.errors,
                "ocr_engine_provider": engine_provider,
                "ocr_engine_outputs": ocr.engine_outputs,
                "ocr_engine_latencies_ms": ocr.engine_latencies_ms,
                "ocr_engine_costs": ocr.engine_costs,
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


def build_records_from_manifest(
    path: str | Path,
    *,
    ocr_engine: str = DEFAULT_OCR_ENGINE_PROVIDER,
    tesseract_executable: str = "tesseract",
    tesseract_language: str = "chi_sim+eng",
    tessdata_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    adapter = _adapter_for_engine(
        ocr_engine=ocr_engine,
        tesseract_executable=tesseract_executable,
        tesseract_language=tesseract_language,
        tessdata_dir=tessdata_dir,
    )
    engine_provider = _provider_label(ocr_engine)
    records: list[dict[str, Any]] = []
    for row in _load_jsonl(path):
        materialized = adapter.materialize_record(row)
        expected_entities = row.get("expected_entities") if isinstance(row.get("expected_entities"), list) else []
        manual_labels = {
            "annotator": row.get("annotator") or row.get("reviewer") or "unknown",
            "review_date": row.get("review_date"),
            "conflict_handling": row.get("conflict_handling") or "authorized_manifest_label",
            "source_manifest": str(_project_path(path)),
        }
        records.append(
            {
                **materialized,
                "source_trace_id": materialized.get("source_trace_id") or materialized.get("trace_id"),
                "ocr_engine_provider": engine_provider,
                "expected_image_text": row.get("expected_image_text"),
                "expected_risk_categories": list(row.get("expected_risk_categories") or []),
                "expected_secondary_labels": list(row.get("expected_secondary_labels") or row.get("expected_secondary_risks") or []),
                "expected_entities": expected_entities,
                "manual_labels": manual_labels,
                "claim_boundary": (
                    "Authorized screenshot/poster manifest row materialized through OCR adapter; quality claims are limited "
                    "to the provided manifest and configured OCR engines."
                ),
            }
        )
    return records


def build_report(
    records: list[dict[str, Any]],
    *,
    output_path: str | Path,
    manifest_path: str | Path | None = None,
    tesseract_executable: str = "tesseract",
    tesseract_language: str = "chi_sim+eng",
    tessdata_dir: str | Path | None = None,
) -> dict[str, Any]:
    run_type = "build_ocr_manifest_hardset" if manifest_path else "build_ocr_image_text_hardset"
    return {
        "status": "completed" if records and (manifest_path or len(records) >= 20) else "insufficient_records",
        "run_type": run_type,
        "output": _display_path(_project_path(output_path)),
        "manifest": str(_project_path(manifest_path)) if manifest_path else None,
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
        "ocr_engine_provider_counts": _counts(record.get("ocr_engine_provider") for record in records),
        "ocr_quality_metrics": _ocr_quality_metrics(records),
        "ocr_engine_comparison": {
            "configured_engines": _configured_engine_providers(records),
            "unavailable_engines": _unavailable_engine_summaries(records),
            "engine_availability": _engine_availability(records),
            "engine_quality_metrics": _ocr_engine_quality_metrics(records),
            "tesseract_environment": tesseract_environment_status(
                executable=tesseract_executable,
                language=tesseract_language,
                tessdata_dir=tessdata_dir,
            ),
            "cloud_ocr_environment": cloud_ocr_environment_status(),
            "optional_operator_engines": ["TesseractCliOCREngine", "cloud_ocr_callable"],
            "comparison_contract": "OCRImageTextAdapter accepts multiple named engines and records per-engine outputs.",
        },
        "claim_boundary": (
            "This hard set validates the image-text contract and deterministic pixel OCR path; production OCR quality "
            "still depends on the injected external engine and separately authorized screenshots/posters."
            if not manifest_path
            else "This manifest report covers only the listed authorized screenshots/posters and the configured OCR engines."
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
    records = (
        build_records_from_manifest(
            args.manifest,
            ocr_engine=args.ocr_engine,
            tesseract_executable=args.tesseract_executable,
            tesseract_language=args.tesseract_language,
            tessdata_dir=args.tessdata_dir or None,
        )
        if args.manifest
        else build_records(
            count=args.count,
            image_dir=args.image_dir,
            ocr_engine=args.ocr_engine,
            tesseract_executable=args.tesseract_executable,
            tesseract_language=args.tesseract_language,
            tessdata_dir=args.tessdata_dir or None,
        )
    )
    output = write_jsonl(records, args.output)
    report = build_report(
        records,
        output_path=output,
        manifest_path=args.manifest,
        tesseract_executable=args.tesseract_executable,
        tesseract_language=args.tesseract_language,
        tessdata_dir=args.tessdata_dir or None,
    )
    report_path = _project_path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _adapter_for_engine(
    *,
    ocr_engine: str,
    tesseract_executable: str,
    tesseract_language: str,
    tessdata_dir: str | Path | None = None,
) -> OCRImageTextAdapter:
    engines: dict[str, Any] = {}
    requested = {item.strip() for item in str(ocr_engine or DEFAULT_OCR_ENGINE_PROVIDER).split(",") if item.strip()}
    if "bitmap_glyph" in requested:
        engines["bitmap_glyph"] = BitmapGlyphOCREngine()
    if "tesseract" in requested:
        engines["tesseract"] = TesseractCliOCREngine(
            executable=tesseract_executable,
            language=tesseract_language,
            tessdata_dir=tessdata_dir,
        )
    return OCRImageTextAdapter(engines=engines)


def _provider_label(ocr_engine: str) -> str:
    return ",".join(sorted({item.strip() for item in str(ocr_engine or DEFAULT_OCR_ENGINE_PROVIDER).split(",") if item.strip()}))


def _counts(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _configured_engine_providers(records: Iterable[dict[str, Any]]) -> list[str]:
    providers = set()
    for record in records:
        raw = str(record.get("ocr_engine_provider") or "").strip()
        if not raw or raw == "none":
            continue
        providers.update(item.strip() for item in raw.split(",") if item.strip())
    if providers:
        return sorted(providers)
    return sorted(
        {
            str(source).removeprefix("ocr_engine.")
            for record in records
            for source in record.get("ocr_sources", [])
            if str(source).startswith("ocr_engine.")
        }
    )


def _ocr_quality_metrics(records: Iterable[dict[str, Any]]) -> dict[str, int | float]:
    evaluated_count = 0
    exact_match_count = 0
    substring_match_count = 0
    for record in records:
        expected = normalize_for_metric(record.get("expected_image_text"))
        if not expected:
            continue
        evaluated_count += 1
        actual = normalize_for_metric(record.get("ocr_text") or record.get("content_text"))
        if actual == expected:
            exact_match_count += 1
        if expected in actual:
            substring_match_count += 1

    return {
        "evaluated_count": evaluated_count,
        "exact_match_count": exact_match_count,
        "exact_match_rate": _rate(exact_match_count, evaluated_count),
        "substring_match_count": substring_match_count,
        "substring_match_rate": _rate(substring_match_count, evaluated_count),
    }


def _ocr_engine_quality_metrics(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, int | float]]:
    record_list = list(records)
    engine_names = _engine_names_from_records(record_list)
    errors_by_engine = _error_counts_by_engine(record_list)
    metrics: dict[str, dict[str, int | float]] = {}
    for engine_name in engine_names:
        evaluated_count = 0
        exact_match_count = 0
        substring_match_count = 0
        latency_values: list[float] = []
        cost_values: list[float] = []
        for record in record_list:
            expected = normalize_for_metric(record.get("expected_image_text"))
            if not expected:
                continue
            evaluated_count += 1
            output = normalize_for_metric(_engine_output_for(record, engine_name))
            if output == expected:
                exact_match_count += 1
            if expected and expected in output:
                substring_match_count += 1
            latency = _engine_metric_value(record.get("ocr_engine_latencies_ms"), engine_name)
            if latency is not None:
                latency_values.append(latency)
            cost = _engine_metric_value(record.get("ocr_engine_costs"), engine_name)
            if cost is not None:
                cost_values.append(cost)
        failure_count = errors_by_engine.get(engine_name, 0)
        metrics[engine_name] = {
            "evaluated_count": evaluated_count,
            "exact_match_count": exact_match_count,
            "exact_match_rate": _rate(exact_match_count, evaluated_count),
            "substring_match_count": substring_match_count,
            "substring_match_rate": _rate(substring_match_count, evaluated_count),
            "failure_count": failure_count,
            "failure_rate": _rate(failure_count, evaluated_count),
            "latency_count": len(latency_values),
            "avg_latency_ms": _average(latency_values),
            "total_cost": round(sum(cost_values), 6),
            "avg_cost": _average(cost_values),
        }
    return metrics


def tesseract_environment_status(
    *,
    executable: str = "tesseract",
    language: str = "chi_sim+eng",
    tessdata_dir: str | Path | None = None,
) -> dict[str, Any]:
    required_languages = [item.strip() for item in str(language or "").split("+") if item.strip()]
    binary = shutil.which(executable)
    tessdata_prefix = str(_project_path(tessdata_dir)) if tessdata_dir else None
    status: dict[str, Any] = {
        "executable": executable,
        "resolved_executable": binary,
        "language": language,
        "tessdata_dir": tessdata_prefix,
        "required_languages": required_languages,
        "version_status": "not_found" if not binary else "unknown",
        "version": None,
        "language_status": "not_checked",
        "available_languages": [],
        "missing_languages": [],
    }
    if not binary:
        return status

    version_result = _run_tesseract_probe([binary, "--version"], tessdata_dir=tessdata_prefix)
    status["version_status"] = "available" if version_result["returncode"] == 0 else "failed"
    status["version"] = _first_output_line(version_result)
    status["version_error"] = version_result["stderr"] if version_result["returncode"] != 0 else None

    language_result = _run_tesseract_probe([binary, "--list-langs"], tessdata_dir=tessdata_prefix)
    if language_result["returncode"] != 0:
        status["language_status"] = "failed"
        status["language_error"] = language_result["stderr"]
        return status
    available = [
        line.strip()
        for line in str(language_result["stdout"] or "").splitlines()
        if line.strip() and not line.lower().startswith("list of available languages")
    ]
    missing = [item for item in required_languages if item not in set(available)]
    status["language_status"] = "available" if not missing else "missing_required_language"
    status["available_languages"] = available
    status["missing_languages"] = missing
    return status


def cloud_ocr_environment_status(
    *,
    provider_env: str = "BLACKAGENT_CLOUD_OCR_PROVIDER",
    api_key_env: str = "BLACKAGENT_CLOUD_OCR_API_KEY",
) -> dict[str, Any]:
    provider = str(os.getenv(provider_env) or "").strip()
    has_key = bool(str(os.getenv(api_key_env) or "").strip())
    configured = bool(provider and has_key)
    return {
        "provider": provider or "not_configured",
        "api_key_env": api_key_env,
        "api_key_status": "configured" if has_key else "missing",
        "configured": configured,
        "claim_boundary": "Cloud OCR is not evaluated until a provider key and callable engine are configured.",
    }


def _unavailable_engine_summaries(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    errors_by_engine: dict[str, list[str]] = {}
    for record in records:
        for error in record.get("ocr_errors", []) or []:
            error_text = str(error)
            engine = _engine_name_from_error(error_text)
            if not engine:
                continue
            errors_by_engine.setdefault(engine, []).append(error_text)
    return [
        {
            "engine": engine,
            "error_count": len(errors),
            "sample_errors": errors[:3],
        }
        for engine, errors in sorted(errors_by_engine.items())
    ]


def _run_tesseract_probe(command: list[str], *, tessdata_dir: str | Path | None = None) -> dict[str, Any]:
    env = None
    if tessdata_dir:
        env = {**os.environ, "TESSDATA_PREFIX": str(tessdata_dir)}
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _first_output_line(result: Mapping[str, Any]) -> str | None:
    text = str(result.get("stdout") or result.get("stderr") or "").strip()
    if not text:
        return None
    return text.splitlines()[0].strip() or None


def _engine_names_from_records(records: Iterable[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for record in records:
        for key in (record.get("ocr_engine_outputs") or {}).keys():
            name = _base_engine_name(key)
            if name:
                names.add(name)
        for key in (record.get("ocr_engine_latencies_ms") or {}).keys():
            name = _base_engine_name(key)
            if name:
                names.add(name)
        for provider in str(record.get("ocr_engine_provider") or "").split(","):
            name = _base_engine_name(provider)
            if name and name != "none":
                names.add(name)
        for error in record.get("ocr_errors", []) or []:
            name = _engine_name_from_error(str(error))
            if name:
                names.add(name)
    return sorted(names)


def _engine_output_for(record: Mapping[str, Any], engine_name: str) -> str:
    outputs = record.get("ocr_engine_outputs") or {}
    if not isinstance(outputs, Mapping):
        return ""
    exact = outputs.get(engine_name)
    if exact is not None:
        return str(exact)
    values = [
        str(value)
        for key, value in outputs.items()
        if _base_engine_name(key) == engine_name and value is not None
    ]
    return " ".join(values)


def _engine_metric_value(value: Any, engine_name: str) -> float | None:
    if isinstance(value, Mapping):
        if engine_name in value:
            return _numeric_metric(value.get(engine_name))
        for key, candidate in value.items():
            if _base_engine_name(key) == engine_name:
                metric = _numeric_metric(candidate)
                if metric is not None:
                    return metric
        return None
    return _numeric_metric(value)


def _error_counts_by_engine(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for error in record.get("ocr_errors", []) or []:
            engine = _engine_name_from_error(str(error))
            if engine:
                counts[engine] = counts.get(engine, 0) + 1
    return counts


def _base_engine_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split(":", 1)[0].strip()


def _numeric_metric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _average(values: Iterable[float]) -> float:
    numbers = list(values)
    return round(sum(numbers) / len(numbers), 6) if numbers else 0.0


def _engine_availability(records: Iterable[dict[str, Any]]) -> dict[str, str]:
    record_list = list(records)
    configured = set(_configured_engine_providers(record_list))
    unavailable = {item["engine"] for item in _unavailable_engine_summaries(record_list)}
    return {
        "bitmap_glyph": "configured" if "bitmap_glyph" in configured else "not_configured",
        "TesseractCliOCREngine": _engine_status(configured=configured, unavailable=unavailable, engine_name="tesseract"),
        "cloud_ocr_callable": _engine_status(configured=configured, unavailable=unavailable, engine_name="cloud_ocr_callable"),
    }


def _engine_status(*, configured: set[str], unavailable: set[str], engine_name: str) -> str:
    if engine_name in unavailable:
        return "unavailable"
    if engine_name in configured:
        return "configured"
    return "not_configured"


def _engine_name_from_error(error: str) -> str | None:
    if not error.startswith("ocr_engine_error:"):
        return None
    parts = error.split(":", 3)
    if len(parts) < 3:
        return None
    return parts[1] or None


def normalize_for_metric(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


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


def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(path)
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    if target.is_relative_to(PROJECT_ROOT):
        return str(target.relative_to(PROJECT_ROOT))
    return str(target)


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
