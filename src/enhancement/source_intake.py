"""Source intake, multimodal text extraction, and compliance discovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.collector.base_collector import get_record_field
from src.cleaner.text_filter import normalize_text


@dataclass(frozen=True)
class SourceIntakeDecision:
    source_name: str
    allowed: bool
    reason: str
    source_type: str | None = None
    legal_basis: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class AuthorizedSourcePolicy:
    """Gate Phase II/III source expansion against PRD compliance boundaries."""

    AUTHORIZED_LEGAL_BASES = {
        "AUTHORIZED_PARTNER",
        "PUBLIC_COMPLIANT_DATA",
        "INTERNAL_AUTHORIZED_SOURCE",
        "THIRD_PARTY_AUTHORIZED_FEED",
    }
    FORBIDDEN_MARKERS = (
        "bypass",
        "proxy",
        "captcha",
        "login_state",
        "unauthorized",
        "绕过",
        "代理",
        "验证码",
        "未授权",
        "越权",
    )

    def decide(self, source: Mapping[str, Any] | Any) -> SourceIntakeDecision:
        source_name = str(get_record_field(source, "source_name") or get_record_field(source, "name") or "unknown_source")
        source_type = str(get_record_field(source, "source_type") or get_record_field(source, "type") or "unknown")
        raw_legal_basis = get_record_field(source, "legal_basis")
        legal_basis = str(getattr(raw_legal_basis, "value", raw_legal_basis) or "")
        text = f"{source_name} {source_type} {legal_basis} {get_record_field(source, 'source_url') or ''}".lower()
        if any(marker in text for marker in self.FORBIDDEN_MARKERS):
            return SourceIntakeDecision(source_name, False, "source_requires_bypass_or_unauthorized_access", source_type, legal_basis)
        if legal_basis not in self.AUTHORIZED_LEGAL_BASES:
            return SourceIntakeDecision(source_name, False, "missing_authorized_legal_basis", source_type, legal_basis)
        return SourceIntakeDecision(source_name, True, "authorized_source", source_type, legal_basis)

    def filter_records(self, records: Iterable[Mapping[str, Any] | Any]) -> tuple[list[Any], list[SourceIntakeDecision]]:
        accepted: list[Any] = []
        decisions: list[SourceIntakeDecision] = []
        for record in records:
            decision = self.decide(record)
            decisions.append(decision)
            if decision.allowed:
                accepted.append(record)
        return accepted, decisions


class MultimodalTextExtractor:
    """Fold local OCR/alt-text fields into the text pipeline.

    This is a deterministic Phase II adapter: it does not perform real OCR, but
    it accepts OCR text already produced by an authorized upstream processor and
    makes the pipeline contract ready for multimodal resources.
    """

    TEXT_FIELDS = (
        "content_text",
        "clean_text",
        "text",
        "raw_text",
        "caption",
        "ocr_text",
        "alt_text",
        "image_ocr_text",
        "poster_text",
        "subtitle_text",
    )
    ATTACHMENT_TEXT_FIELDS = ("ocr_text", "alt_text", "caption", "description", "image_ocr_text", "poster_text", "subtitle_text", "text")
    NESTED_COLLECTION_FIELDS = ("attachments", "media", "images", "screenshots", "albums", "cards", "frames", "ocr_blocks", "text_blocks")

    def extract_text(self, record: Mapping[str, Any] | Any) -> str:
        parts, _sources = self._collect_text_parts(record)
        return normalize_text(" ".join(parts))

    def materialize(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        data = dict(record) if isinstance(record, Mapping) else {
            key: getattr(record, key)
            for key in dir(record)
            if not key.startswith("_") and not callable(getattr(record, key))
        }
        parts, sources = self._collect_text_parts(record)
        data["content_text"] = normalize_text(" ".join(parts))
        data["multimodal_text_extracted"] = True
        data["multimodal_text_sources"] = sorted(sources)
        data["multimodal_signal_count"] = len(sources)
        return data

    def _collect_text_parts(self, record: Mapping[str, Any] | Any, *, _depth: int = 0) -> tuple[list[str], set[str]]:
        if _depth > 4:
            return [], set()
        parts: list[str] = []
        sources: set[str] = set()
        fields = self.TEXT_FIELDS if _depth == 0 else self.ATTACHMENT_TEXT_FIELDS
        for field_name in fields:
            value = get_record_field(record, field_name)
            if value:
                text = normalize_text(str(value))
                if text:
                    parts.append(text)
                    sources.add(field_name)
        for field_name in self.NESTED_COLLECTION_FIELDS:
            nested = get_record_field(record, field_name)
            if not nested:
                continue
            if isinstance(nested, Mapping):
                nested_items = [nested]
            elif isinstance(nested, Iterable) and not isinstance(nested, (str, bytes)):
                nested_items = list(nested)
            else:
                continue
            for item in nested_items:
                sub_parts, sub_sources = self._collect_text_parts(item, _depth=_depth + 1)
                parts.extend(sub_parts)
                sources.update({f"{field_name}.{name}" for name in sub_sources} or {field_name})
        return parts, sources


@dataclass(frozen=True)
class ComplianceCandidate:
    source_name: str
    source_url: str
    status: str
    reason: str
    next_action: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class ComplianceSourceDiscovery:
    """Phase III compliant-source discovery and scheduling pre-check."""

    def evaluate(self, candidate: Mapping[str, Any] | Any) -> ComplianceCandidate:
        source_name = str(get_record_field(candidate, "source_name") or get_record_field(candidate, "name") or "candidate")
        source_url = str(get_record_field(candidate, "source_url") or get_record_field(candidate, "url") or "")
        robots_allowed = bool(get_record_field(candidate, "robots_allowed", False))
        terms_allow = bool(get_record_field(candidate, "terms_allow_security_research", False))
        requires_login = bool(get_record_field(candidate, "requires_login", False))
        has_auth = bool(get_record_field(candidate, "has_written_authorization", False))
        rate_limit = int(get_record_field(candidate, "rate_limit_per_minute", 0) or 0)

        if requires_login and not has_auth:
            return ComplianceCandidate(source_name, source_url, "REJECTED", "login_required_without_authorization", "do_not_schedule", dict(candidate))
        if not robots_allowed or not terms_allow:
            return ComplianceCandidate(source_name, source_url, "NEEDS_LEGAL_REVIEW", "robots_or_terms_not_confirmed", "manual_compliance_review", dict(candidate))
        if rate_limit <= 0:
            return ComplianceCandidate(source_name, source_url, "NEEDS_RATE_LIMIT", "missing_rate_limit", "set_safe_rate_limit_before_schedule", dict(candidate))
        return ComplianceCandidate(source_name, source_url, "SCHEDULABLE", "compliance_precheck_passed", "schedule_with_rate_limit", dict(candidate))


__all__ = [
    "AuthorizedSourcePolicy",
    "ComplianceCandidate",
    "ComplianceSourceDiscovery",
    "MultimodalTextExtractor",
    "SourceIntakeDecision",
]
