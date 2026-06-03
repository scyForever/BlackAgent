"""OCR image-text ingestion adapters."""

from .engines import BitmapGlyphOCREngine, OCRRuntimeError, TesseractCliOCREngine, render_demo_pbm
from .image_text import OCRImageTextAdapter, OCRImageTextResult

__all__ = [
    "BitmapGlyphOCREngine",
    "OCRImageTextAdapter",
    "OCRImageTextResult",
    "OCRRuntimeError",
    "TesseractCliOCREngine",
    "render_demo_pbm",
]
