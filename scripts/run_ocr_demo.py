"""Run a reproducible OCR demo through the pluggable image-text adapter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ocr import BitmapGlyphOCREngine, OCRImageTextAdapter, render_demo_pbm


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run BlackAgent's local pluggable OCR demo.")
    parser.add_argument("--text", default="TG:OCR001", help="ASCII demo text rendered into a PBM image.")
    parser.add_argument("--image-output", default="data/ocr_demo_image.pbm", help="Where to write the generated demo image.")
    parser.add_argument("--output", default="data/ocr_demo_report.json", help="Where to write the OCR demo report.")
    return parser.parse_args(argv)


def run_demo(*, text: str = "TG:OCR001", image_output: str | Path = "data/ocr_demo_image.pbm") -> dict[str, Any]:
    image_path = _project_path(image_output)
    render_demo_pbm(text, image_path)
    record = {
        "trace_id": "ocr-demo-001",
        "source_name": "local-ocr-demo",
        "source_type": "Image",
        "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
        "caption": "授权海报 OCR demo：群控脚本 接码",
        "image_path": str(image_path),
    }
    result = OCRImageTextAdapter(engine=BitmapGlyphOCREngine()).extract(record)
    return {
        "status": result.status,
        "run_type": "pluggable_real_ocr_demo",
        "engine": "BitmapGlyphOCREngine",
        "image_path": str(image_path.relative_to(PROJECT_ROOT) if image_path.is_relative_to(PROJECT_ROOT) else image_path),
        "expected_image_text": text.upper(),
        "extracted_text": result.text,
        "content_modality": result.content_modality,
        "sources": result.sources,
        "errors": result.errors,
        "claim_boundary": (
            "This demo reads PBM image pixels with a no-dependency glyph OCR engine; "
            "production OCR can inject TesseractCliOCREngine or any callable engine."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_demo(text=args.text, image_output=args.image_output)
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
