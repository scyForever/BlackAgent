"""Deterministic text filtering, normalization, and near-duplicate grouping."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from hashlib import sha1
from importlib import import_module
from typing import Any


MAX_CLEAN_TEXT_CHARS = 4000
NEAR_DUPLICATE_THRESHOLD = 0.92
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class FallbackCleanedText:
    source_trace_id: str
    clean_text: str
    noise_score: float
    dedup_group_id: str

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DroppedRecord:
    source_trace_id: str
    reason: str
    noise_score: float = 0.0
    dedup_group_id: str | None = None
    similarity: float | None = None


CLEANED_SCHEMA_MODEL = None


def _load_cleaned_schema_model() -> type[Any]:
    global CLEANED_SCHEMA_MODEL
    if CLEANED_SCHEMA_MODEL is not None:
        return CLEANED_SCHEMA_MODEL
    try:
        CLEANED_SCHEMA_MODEL = getattr(import_module("storage.schemas"), "CleanedText")
    except Exception:
        CLEANED_SCHEMA_MODEL = FallbackCleanedText
    return CLEANED_SCHEMA_MODEL


def _schema_fields(model: type[Any]) -> set[str]:
    return set(getattr(model, "model_fields", {}) or getattr(model, "__annotations__", {}) or [])


def build_cleaned_text(
    *,
    source_trace_id: str,
    clean_text: str,
    noise_score: float,
    dedup_group_id: str,
) -> Any:
    payload = {
        "source_trace_id": source_trace_id,
        "clean_text": clean_text,
        "noise_score": noise_score,
        "dedup_group_id": dedup_group_id,
    }
    model = _load_cleaned_schema_model()
    fields = _schema_fields(model)
    candidate = {key: value for key, value in payload.items() if not fields or key in fields}
    try:
        return model(**candidate)  # type: ignore[misc,operator]
    except Exception:
        return FallbackCleanedText(**payload)


def normalize_text(text: str | None) -> str:
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = ZERO_WIDTH_RE.sub("", normalized)
    normalized = WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()


def _is_signal_char(char: str) -> bool:
    return char.isalnum() or "\u4e00" <= char <= "\u9fff"


def calculate_noise_score(text: str) -> float:
    """Return a 0-1 noise estimate; higher means more likely pure garbage."""

    normalized = normalize_text(text)
    if not normalized:
        return 1.0

    visible = [char for char in normalized if not char.isspace()]
    if not visible:
        return 1.0

    signal_count = sum(1 for char in visible if _is_signal_char(char))
    replacement_ratio = sum(1 for char in visible if char == "�") / len(visible)
    signal_ratio = signal_count / len(visible)
    repeat_ratio = Counter(visible).most_common(1)[0][1] / len(visible)

    symbol_noise = 1.0 - signal_ratio
    repeated_noise = repeat_ratio if repeat_ratio >= 0.80 and len(visible) >= 8 else 0.0
    short_symbol_noise = 1.0 if len(visible) <= 4 and signal_count == 0 else 0.0
    noise_score = max(replacement_ratio, symbol_noise, repeated_noise, short_symbol_noise)
    return round(min(1.0, noise_score), 4)


def is_blank_or_garbled(text: str | None, *, threshold: float = 0.72) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return True
    if not any(_is_signal_char(char) for char in normalized):
        return True
    return calculate_noise_score(normalized) >= threshold


def canonicalize_for_dedup(text: str) -> str:
    """Canonical representation used for exact and near duplicate grouping."""

    normalized = normalize_text(text).lower()
    return "".join(char for char in normalized if _is_signal_char(char))


def stable_dedup_group_id(canonical_text: str) -> str:
    digest = sha1(canonical_text.encode("utf-8")).hexdigest()[:16]
    return f"dedup:{digest}"


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    if len(text) <= n:
        return {text} if text else set()
    return {text[index : index + n] for index in range(len(text) - n + 1)}


def text_similarity(left: str, right: str) -> float:
    left_canon = canonicalize_for_dedup(left)
    right_canon = canonicalize_for_dedup(right)
    if not left_canon or not right_canon:
        return 0.0
    if left_canon == right_canon:
        return 1.0
    length_ratio = min(len(left_canon), len(right_canon)) / max(len(left_canon), len(right_canon))
    if length_ratio < 0.60:
        return 0.0

    sequence_score = SequenceMatcher(None, left_canon, right_canon, autojunk=False).ratio()
    left_grams = _char_ngrams(left_canon)
    right_grams = _char_ngrams(right_canon)
    jaccard = len(left_grams & right_grams) / len(left_grams | right_grams) if left_grams and right_grams else 0.0
    return round(max(sequence_score, jaccard), 4)


class DedupIndex:
    """Stateful exact + near duplicate index for one cleaner run."""

    def __init__(self, *, threshold: float = NEAR_DUPLICATE_THRESHOLD) -> None:
        self.threshold = threshold
        self._exact: dict[str, str] = {}
        self._representatives: list[tuple[str, str]] = []

    def assign(self, text: str) -> tuple[str, bool, float]:
        canonical = canonicalize_for_dedup(text)
        if not canonical:
            return stable_dedup_group_id("empty"), False, 0.0
        if canonical in self._exact:
            return self._exact[canonical], True, 1.0

        for group_id, representative in self._representatives:
            score = text_similarity(canonical, representative)
            if score >= self.threshold:
                self._exact[canonical] = group_id
                return group_id, True, score

        group_id = stable_dedup_group_id(canonical)
        self._exact[canonical] = group_id
        self._representatives.append((group_id, canonical))
        return group_id, False, 0.0


__all__ = [
    "DedupIndex",
    "DroppedRecord",
    "FallbackCleanedText",
    "MAX_CLEAN_TEXT_CHARS",
    "NEAR_DUPLICATE_THRESHOLD",
    "build_cleaned_text",
    "calculate_noise_score",
    "canonicalize_for_dedup",
    "is_blank_or_garbled",
    "normalize_text",
    "stable_dedup_group_id",
    "text_similarity",
]

