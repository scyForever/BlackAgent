"""OCR image-text ingestion contract for BlackAgent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from src.cleaner.text_filter import normalize_text
from src.enhancement.source_intake import MultimodalTextExtractor


OCREngine = Callable[[str | Path], str]


@dataclass(frozen=True)
class OCRImageTextResult:
    status: str
    text: str
    content_modality: str
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class OCRImageTextAdapter:
    """Materialize image text from supplied OCR fields or an injected engine."""

    IMAGE_PATH_FIELDS = ("image_path", "screenshot_path", "file_path")

    def __init__(self, *, engine: OCREngine | None = None, extractor: MultimodalTextExtractor | None = None) -> None:
        self.engine = engine
        self.extractor = extractor or MultimodalTextExtractor()

    def extract(self, record: Mapping[str, Any] | Any) -> OCRImageTextResult:
        materialized = self.extractor.materialize(record)
        text = normalize_text(materialized.get("content_text"))
        sources = list(materialized.get("multimodal_text_sources") or [])
        errors: list[str] = []
        engine_texts: list[str] = []
        for path in self._image_paths(record):
            if self.engine is None:
                errors.append(f"ocr_engine_not_configured:{path}")
                continue
            try:
                engine_texts.append(normalize_text(self.engine(path)))
                sources.append("ocr_engine.image_path")
            except Exception as exc:  # pragma: no cover - defensive for injected engines
                errors.append(f"ocr_engine_error:{path}:{exc}")
        final_text = normalize_text(" ".join([text, *engine_texts]))
        status = "completed" if final_text and not errors else "partial" if final_text else "missing_ocr_text"
        return OCRImageTextResult(
            status=status,
            text=final_text,
            content_modality=str(materialized.get("content_modality") or "text"),
            sources=sorted(set(sources)),
            errors=errors,
        )

    def materialize_record(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        result = self.extract(record)
        data = dict(record) if isinstance(record, Mapping) else {"value": record}
        data["content_text"] = result.text
        data["content_modality"] = result.content_modality
        data["ocr_status"] = result.status
        data["ocr_sources"] = result.sources
        data["ocr_errors"] = result.errors
        return data

    def _image_paths(self, record: Mapping[str, Any] | Any) -> list[str]:
        paths: list[str] = []
        for field in self.IMAGE_PATH_FIELDS:
            value = _get(record, field)
            if value:
                paths.append(str(value))
        for field in ("attachments", "media", "images", "screenshots"):
            nested = _get(record, field)
            if isinstance(nested, Mapping):
                nested = [nested]
            if isinstance(nested, Iterable) and not isinstance(nested, (str, bytes)):
                for item in nested:
                    for path_field in self.IMAGE_PATH_FIELDS:
                        value = _get(item, path_field)
                        if value:
                            paths.append(str(value))
        return paths


def _get(record: Mapping[str, Any] | Any, field: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(field)
    return getattr(record, field, None)


__all__ = ["OCRImageTextAdapter", "OCRImageTextResult"]
