"""Authorized HTTP feed collector for real threat-intelligence datasets.

This collector is intentionally generic: production operators provide an
explicit feed URL, legal basis, and optional allowlist instead of the code
hard-coding crawler behavior.  It performs ordinary HTTP(S) GET requests only;
it does not bypass logins, CAPTCHA, robots/terms restrictions, or rate limits.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping
from urllib import request as urllib_request
from urllib.parse import urlparse

from .base_collector import build_raw_intelligence, model_dump


class NetworkCollectionDisabled(RuntimeError):
    """Raised when a real HTTP fetch is requested without explicit enablement."""


class SourceAuthorizationError(ValueError):
    """Raised when feed metadata crosses the authorized-source boundary."""


@dataclass(frozen=True)
class HTTPFeedConfig:
    """Configuration for one authorized HTTP(S) intelligence feed."""

    source_url: str
    source_name: str
    source_type: str = "THREAT_INTEL"
    legal_basis: str = "PUBLIC_COMPLIANT_DATA"
    feed_format: str = "auto"
    max_records: int = 100
    timeout_seconds: float = 15.0
    user_agent: str = "BlackAgent-HTTPFeedCollector/0.1"
    allowed_domains: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    text_fields: tuple[str, ...] = (
        "content_text",
        "text",
        "raw_text",
        "url",
        "indicator",
        "domain",
        "host",
        "ioc",
        "threat",
    )
    network_enabled: bool = False


class HTTPFeedCollector:
    """Fetch and normalize rows from an explicitly authorized HTTP(S) feed."""

    COMMENT_PREFIXES = ("#", "//")
    JSON_LIST_KEYS = ("data", "items", "results", "urls", "indicators")
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

    def __init__(
        self,
        config: HTTPFeedConfig,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.opener = opener or urllib_request.urlopen

    def stream(self) -> Iterable[Any]:
        for row in self.fetch_rows():
            yield build_raw_intelligence(row)

    def collect(self) -> list[Any]:
        return list(self.stream())

    def read_all(self) -> list[Any]:
        return self.collect()

    def __iter__(self) -> Iterable[Any]:
        return self.stream()

    def fetch_rows(self) -> list[dict[str, Any]]:
        """Fetch, parse, and normalize feed rows without persisting them."""

        self._validate_fetch_allowed()
        body, content_type = self._fetch_text()
        rows = self._parse_body(body, content_type)
        return [self._normalize_row(row, index) for index, row in enumerate(rows[: self.config.max_records], start=1)]

    def _validate_fetch_allowed(self) -> None:
        if not self.config.network_enabled:
            raise NetworkCollectionDisabled("network collection is disabled; set network.enabled=true before fetching real feeds")
        if self.config.max_records <= 0:
            raise ValueError("max_records must be positive")
        if self.config.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        parsed = urlparse(self.config.source_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise SourceAuthorizationError("source_url must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise SourceAuthorizationError("source_url must not embed credentials; pass auth through headers or env-backed config")
        if self.config.allowed_domains and not _host_allowed(parsed.hostname or "", self.config.allowed_domains):
            raise SourceAuthorizationError("source_url host is outside the configured allowed_domains")

        metadata_text = " ".join(
            [
                self.config.source_name,
                self.config.source_type,
                self.config.source_url,
                self.config.legal_basis,
            ]
        ).lower()
        if any(marker in metadata_text for marker in self.FORBIDDEN_MARKERS):
            raise SourceAuthorizationError("source_requires_bypass_or_unauthorized_access")
        if self.config.legal_basis not in self.AUTHORIZED_LEGAL_BASES:
            raise SourceAuthorizationError("missing_authorized_legal_basis")

    def _fetch_text(self) -> tuple[str, str]:
        headers = {"User-Agent": self.config.user_agent, "Accept": "application/json,text/csv,text/plain,*/*"}
        headers.update({str(key): str(value) for key, value in self.config.headers.items()})
        req = urllib_request.Request(self.config.source_url, headers=headers, method="GET")
        with self.opener(req, timeout=self.config.timeout_seconds) as response:  # noqa: S310 - explicit authorized URL only
            raw_body = response.read()
            content_type = ""
            headers_obj = getattr(response, "headers", None)
            if headers_obj is not None:
                content_type = str(headers_obj.get("Content-Type", ""))
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.rsplit("charset=", 1)[-1].split(";", 1)[0].strip() or "utf-8"
            return raw_body.decode(charset, errors="replace"), content_type

    def _parse_body(self, body: str, content_type: str) -> list[Mapping[str, Any] | str]:
        feed_format = self._resolve_format(body, content_type)
        if feed_format == "json":
            loaded = json.loads(body)
            return list(self._json_rows(loaded))
        if feed_format == "jsonl":
            return [json.loads(line) for line in body.splitlines() if _data_line(line)]
        if feed_format == "csv":
            return list(self._csv_rows(body))
        if feed_format == "txt":
            return [line.strip() for line in body.splitlines() if _data_line(line)]
        if feed_format == "html":
            return list(self._html_rows(body))
        raise ValueError(f"unsupported feed_format: {self.config.feed_format}")

    def _resolve_format(self, body: str, content_type: str) -> str:
        configured = self.config.feed_format.lower().strip()
        if configured != "auto":
            return configured
        stripped = body.lstrip()
        lowered_type = content_type.lower()
        if "html" in lowered_type or stripped.startswith("<!doctype html") or stripped.startswith("<html"):
            return "html"
        if stripped.startswith("[") or stripped.startswith("{") or "json" in lowered_type:
            return "json"
        first_data_line = next((line.strip() for line in body.splitlines() if _data_line(line)), "")
        if first_data_line.startswith("{"):
            return "jsonl"
        if "," in first_data_line or "csv" in lowered_type:
            return "csv"
        return "txt"

    def _json_rows(self, loaded: Any) -> Iterable[Mapping[str, Any] | str]:
        if isinstance(loaded, list):
            yield from loaded
            return
        if isinstance(loaded, Mapping):
            for key in self.JSON_LIST_KEYS:
                value = loaded.get(key)
                if isinstance(value, list):
                    yield from value
                    return
            yield loaded
            return
        yield str(loaded)

    def _csv_rows(self, body: str) -> Iterable[Mapping[str, Any] | str]:
        data_lines = [line for line in body.splitlines() if _data_line(line)]
        if not data_lines:
            return []
        sample = "\n".join(data_lines[:5])
        try:
            has_header = csv.Sniffer().has_header(sample)
        except csv.Error:
            has_header = "," in data_lines[0]
        stream = io.StringIO("\n".join(data_lines))
        if has_header:
            yield from csv.DictReader(stream)
            return
        reader = csv.reader(stream)
        for row in reader:
            if row:
                yield {"indicator": row[0], "raw_columns": row}

    def _html_rows(self, body: str) -> Iterable[Mapping[str, Any] | str]:
        parser = _HTMLSnapshotParser()
        parser.feed(body)
        snapshot_text = parser.snapshot_text()
        if snapshot_text:
            yield {"content_text": snapshot_text}

    def _normalize_row(self, row: Mapping[str, Any] | str, index: int) -> dict[str, Any]:
        if isinstance(row, Mapping):
            data = {str(key): value for key, value in row.items() if key is not None}
            content_text = self._content_text_from_mapping(data)
        else:
            data = {"indicator": str(row)}
            content_text = str(row)

        payload = {
            **data,
            "source_type": str(data.get("source_type") or self.config.source_type),
            "source_name": str(data.get("source_name") or self.config.source_name),
            "source_url": str(data.get("source_url") or self.config.source_url),
            "legal_basis": str(data.get("legal_basis") or self.config.legal_basis),
            "collector_version": "http_feed_collector_v1",
            "raw_payload_uri": self.config.source_url,
            "content_text": content_text,
            "feed_row_index": index,
        }
        return payload

    def _content_text_from_mapping(self, data: Mapping[str, Any]) -> str:
        for field_name in self.config.text_fields:
            value = data.get(field_name)
            if value not in (None, ""):
                return str(value)
        return json.dumps(model_dump(data), ensure_ascii=False, sort_keys=True)


def _data_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith(HTTPFeedCollector.COMMENT_PREFIXES)


def _host_allowed(hostname: str, allowed_domains: Iterable[str]) -> bool:
    host = hostname.lower().strip(".")
    for domain in allowed_domains:
        candidate = str(domain).lower().strip(".")
        if host == candidate or host.endswith(f".{candidate}"):
            return True
    return False


class _HTMLSnapshotParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._meta: dict[str, str] = {}
        self._links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {str(key).lower(): str(value) for key, value in attrs if key and value}
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = (attr_map.get("name") or attr_map.get("property") or attr_map.get("itemprop") or "").lower()
            content = attr_map.get("content", "")
            if key and content and key not in self._meta:
                self._meta[key] = _collapse_ws(content)
        href = attr_map.get("href") or attr_map.get("src")
        if href and href.startswith(("http://", "https://")) and href not in self._links:
            self._links.append(href)
        alt_text = attr_map.get("alt")
        if alt_text:
            self._append_text(alt_text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._append_text(data)

    def snapshot_text(self) -> str:
        title = " ".join(self._title_parts).strip()
        description = (
            self._meta.get("description")
            or self._meta.get("og:description")
            or self._meta.get("twitter:description")
            or ""
        )
        if not title:
            title = self._meta.get("og:title") or self._meta.get("twitter:title") or ""
        parts = [part for part in (title, description, " ".join(self._text_parts).strip()) if part]
        if self._links:
            parts.append("Links: " + " ".join(self._links[:25]))
        return _collapse_ws(" ".join(parts))

    def _append_text(self, text: str) -> None:
        normalized = _collapse_ws(text)
        if not normalized:
            return
        self._text_parts.append(normalized)
        if self._in_title:
            self._title_parts.append(normalized)


def _collapse_ws(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "HTTPFeedCollector",
    "HTTPFeedConfig",
    "NetworkCollectionDisabled",
    "SourceAuthorizationError",
]
