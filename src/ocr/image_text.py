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
    engine_outputs: dict[str, str] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class OCRImageTextAdapter:
    """Materialize image text from supplied OCR fields or injected engines.

    ``engine`` keeps the original single-engine contract. ``engines`` is used
    when operators want a Tesseract/cloud/local comparison over the same
    authorized screenshot without changing the rest of the pipeline.
    """

    IMAGE_PATH_FIELDS = ("image_path", "screenshot_path", "file_path")

    def __init__(
        self,
        *,
        engine: OCREngine | None = None,
        engines: Mapping[str, OCREngine] | None = None,
        extractor: MultimodalTextExtractor | None = None,
    ) -> None:
        self.engines = {
            str(name or "").strip() or "unnamed": candidate
            for name, candidate in (engines or {}).items()
            if candidate is not None
        }
        if engine is not None:
            self.engines.setdefault("image_path", engine)
        self.extractor = extractor or MultimodalTextExtractor()

    def extract(self, record: Mapping[str, Any] | Any) -> OCRImageTextResult:
        materialized = self.extractor.materialize(record)
        text = normalize_text(materialized.get("content_text"))
        sources = list(materialized.get("multimodal_text_sources") or [])
        errors: list[str] = []
        engine_texts: list[str] = []
        engine_outputs: dict[str, str] = {}
        image_paths = self._image_paths(record)
        for path in image_paths:
            if not self.engines:
                errors.append(f"ocr_engine_not_configured:{path}")
                continue
            for engine_name, engine in self.engines.items():
                output_key = engine_name if len(image_paths) == 1 else f"{engine_name}:{Path(path).name}"
                try:
                    extracted_text = normalize_text(engine(path))
                    engine_outputs[output_key] = extracted_text
                    if extracted_text and extracted_text not in engine_texts:
                        engine_texts.append(extracted_text)
                    sources.append("ocr_engine.image_path" if engine_name == "image_path" else f"ocr_engine.{engine_name}")
                except Exception as exc:  # pragma: no cover - defensive for injected engines
                    errors.append(f"ocr_engine_error:{engine_name}:{path}:{exc}")
        final_text = normalize_text(" ".join([text, *engine_texts]))
        content_modality = _merge_modality(
            str(materialized.get("content_modality") or "text"),
            had_upstream_text=bool(text),
            had_engine_text=bool(engine_texts),
        )
        status = "completed" if final_text and not errors else "partial" if final_text else "missing_ocr_text"
        return OCRImageTextResult(
            status=status,
            text=final_text,
            content_modality=content_modality,
            sources=sorted(set(sources)),
            errors=errors,
            engine_outputs=engine_outputs,
        )

    def materialize_record(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        result = self.extract(record)
        data = dict(record) if isinstance(record, Mapping) else {"value": record}
        data["content_text"] = result.text
        data["content_modality"] = result.content_modality
        data["ocr_status"] = result.status
        data["ocr_sources"] = result.sources
        data["ocr_errors"] = result.errors
        data["ocr_engine_outputs"] = result.engine_outputs
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


def _merge_modality(base: str, *, had_upstream_text: bool, had_engine_text: bool) -> str:
    if not had_engine_text:
        return base
    if had_upstream_text and base != "image_text":
        return "mixed"
    return "image_text"


__all__ = ["OCRImageTextAdapter", "OCRImageTextResult"]
