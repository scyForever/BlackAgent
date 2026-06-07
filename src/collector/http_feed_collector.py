"""Authorized HTTP feed collector for real threat-intelligence datasets.

This collector is intentionally generic: production operators provide an
explicit feed URL, legal basis, and optional allowlist instead of the code
hard-coding crawler behavior.  It performs ordinary HTTP(S) GET requests only;
it does not bypass logins, CAPTCHA, robots/terms restrictions, or rate limits.
"""

from __future__ import annotations

import csv
import http.client
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, unquote, urlparse

from .base_collector import build_raw_intelligence, model_dump
from .relevance import decide_text_relevance
from .source_metadata import classify_collection_failure, is_article_source_record, normalize_source_access_type


_HOST_NEXT_ALLOWED_AT: dict[str, float] = {}
_HOST_RATE_LIMIT_LOCK = threading.Lock()


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
    platform: str = ""
    legal_basis: str = "PUBLIC_COMPLIANT_DATA"
    feed_format: str = "auto"
    max_records: int = 100
    timeout_seconds: float = 15.0
    user_agent: str = "BlackAgent-HTTPFeedCollector/0.1"
    allowed_domains: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    include_themes: tuple[str, ...] = ()
    exclude_themes: tuple[str, ...] = ()
    search_query: str | None = None
    query_theme: str | None = None
    query_term: str | None = None
    query_term_stage: str | None = None
    query_variant_index: int | None = None
    min_keyword_hits: int = 1
    rate_limit_per_minute: int = 0
    retry_attempts: int = 0
    retry_backoff_seconds: float = 0.0
    retry_backoff_multiplier: float = 2.0
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)
    source_access_type: str | None = None
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
    DUCKDUCKGO_BLOCK_MARKERS = (
        "Unfortunately, bots use DuckDuckGo too",
        "Unfortunately, bots use DuckDuckGo",
        "automated requests",
    )
    DUCKDUCKGO_RESULT_RE = re.compile(
        r"## \[(?P<title>.+?)\]\((?P<link>https?://[^)]+)\)(?P<body>.*?)(?=(?:## \[)|$)"
    )

    def __init__(
        self,
        config: HTTPFeedConfig,
        *,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.opener = opener or urllib_request.urlopen
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

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
        normalized = [self._normalize_row(row, index) for index, row in enumerate(rows[: self.config.max_records], start=1)]
        return [row for row in (self._apply_relevance_filter(item) for item in normalized) if row is not None]

    def _validate_fetch_allowed(self) -> None:
        if not self.config.network_enabled:
            raise NetworkCollectionDisabled("network collection is disabled; set network.enabled=true before fetching real feeds")
        if self.config.max_records <= 0:
            raise ValueError("max_records must be positive")
        if self.config.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.config.min_keyword_hits <= 0:
            raise ValueError("min_keyword_hits must be positive")
        if self.config.rate_limit_per_minute < 0:
            raise ValueError("rate_limit_per_minute must be non-negative")
        if self.config.retry_attempts < 0:
            raise ValueError("retry_attempts must be non-negative")
        if self.config.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        if self.config.retry_backoff_multiplier < 1:
            raise ValueError("retry_backoff_multiplier must be at least 1")

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
        retries_used = 0
        while True:
            self._throttle_request_host()
            try:
                with self.opener(req, timeout=self.config.timeout_seconds) as response:  # noqa: S310 - explicit authorized URL only
                    content_type = ""
                    headers_obj = getattr(response, "headers", None)
                    if headers_obj is not None:
                        content_type = str(headers_obj.get("Content-Type", ""))
                    try:
                        raw_body = response.read()
                    except http.client.IncompleteRead as exc:
                        raw_body = exc.partial
                    charset = "utf-8"
                    if "charset=" in content_type:
                        charset = content_type.rsplit("charset=", 1)[-1].split(";", 1)[0].strip() or "utf-8"
                    return raw_body.decode(charset, errors="replace"), content_type
            except urllib_error.HTTPError as exc:
                if not self._should_retry_http_error(exc, retries_used):
                    raise
                delay_seconds = self._retry_delay_seconds(exc, retries_used)
                retries_used += 1
                if delay_seconds > 0:
                    self._sleep(delay_seconds)

    def _throttle_request_host(self) -> None:
        hostname = (urlparse(self.config.source_url).hostname or "").lower().strip(".")
        if not hostname:
            return
        interval_seconds = 0.0
        if self.config.rate_limit_per_minute > 0:
            interval_seconds = 60.0 / float(self.config.rate_limit_per_minute)

        sleep_for = 0.0
        with _HOST_RATE_LIMIT_LOCK:
            now = self._monotonic()
            next_allowed_at = _HOST_NEXT_ALLOWED_AT.get(hostname, 0.0)
            if next_allowed_at > now:
                sleep_for = next_allowed_at - now
                scheduled_at = next_allowed_at
            else:
                scheduled_at = now
            if interval_seconds > 0:
                _HOST_NEXT_ALLOWED_AT[hostname] = scheduled_at + interval_seconds
        if sleep_for > 0:
            self._sleep(sleep_for)

    def _should_retry_http_error(self, exc: urllib_error.HTTPError, retries_used: int) -> bool:
        return retries_used < self.config.retry_attempts and exc.code in self.config.retry_statuses

    def _retry_delay_seconds(self, exc: urllib_error.HTTPError, retries_used: int) -> float:
        retry_after = exc.headers.get("Retry-After") if getattr(exc, "headers", None) is not None else None
        parsed_retry_after = _parse_retry_after_seconds(retry_after)
        hostname = (urlparse(self.config.source_url).hostname or "").lower().strip(".")
        if parsed_retry_after is not None:
            self._register_host_backoff(hostname, parsed_retry_after)
            return parsed_retry_after
        delay_seconds = self.config.retry_backoff_seconds * (self.config.retry_backoff_multiplier ** retries_used)
        if exc.code == 429 and delay_seconds > 0:
            self._register_host_backoff(hostname, delay_seconds)
        return delay_seconds

    def _register_host_backoff(self, hostname: str, delay_seconds: float) -> None:
        if not hostname or delay_seconds <= 0:
            return
        with _HOST_RATE_LIMIT_LOCK:
            now = self._monotonic()
            next_allowed_at = _HOST_NEXT_ALLOWED_AT.get(hostname, 0.0)
            base_at = next_allowed_at if next_allowed_at > now else now
            _HOST_NEXT_ALLOWED_AT[hostname] = base_at + delay_seconds

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
            if "DuckDuckGo" in snapshot_text and any(marker in snapshot_text for marker in self.DUCKDUCKGO_BLOCK_MARKERS):
                return []
            duckduckgo_rows = list(self._duckduckgo_search_rows(snapshot_text))
            if duckduckgo_rows:
                yield from duckduckgo_rows
                return
            yield {"content_text": snapshot_text}

    def _normalize_row(self, row: Mapping[str, Any] | str, index: int) -> dict[str, Any]:
        if isinstance(row, Mapping):
            data = {str(key): value for key, value in row.items() if key is not None}
            content_text = self._content_text_from_mapping(data)
        else:
            data = {"indicator": str(row)}
            content_text = str(row)

        row_source_url = self._row_source_url(data)
        platform = str(data.get("platform") or self.config.platform).strip()
        payload = {
            **data,
            "source_type": str(data.get("source_type") or self.config.source_type),
            "source_name": str(data.get("source_name") or self.config.source_name),
            "source_url": str(row_source_url),
            "legal_basis": str(data.get("legal_basis") or self.config.legal_basis),
            "source_access_type": normalize_source_access_type(
                data.get("source_access_type") or self.config.source_access_type,
                legal_basis=data.get("legal_basis") or self.config.legal_basis,
                source_name=str(data.get("source_name") or self.config.source_name),
                source_url=str(row_source_url),
            ),
            "collector_version": "http_feed_collector_v1",
            "raw_payload_uri": self.config.source_url,
            "content_text": content_text,
            "feed_row_index": index,
        }
        if platform:
            payload["platform"] = platform
        if "publish_time" not in payload and data.get("published_at"):
            payload["publish_time"] = str(data.get("published_at"))
        if self.config.search_query:
            payload["search_query"] = self.config.search_query
        if self.config.query_theme:
            payload["query_theme"] = self.config.query_theme
        if self.config.query_term:
            payload["query_term"] = self.config.query_term
        if self.config.query_term_stage:
            payload["query_term_stage"] = self.config.query_term_stage
        if self.config.query_variant_index is not None:
            payload["query_variant_index"] = self.config.query_variant_index
        return payload

    def _row_source_url(self, data: Mapping[str, Any]) -> str:
        if data.get("source_url"):
            return str(data.get("source_url"))
        if is_article_source_record(
            {
                "source_type": data.get("source_type") or self.config.source_type,
                "platform": data.get("platform") or self.config.platform,
            }
        ) and data.get("url"):
            return str(data.get("url"))
        return self.config.source_url

    def _content_text_from_mapping(self, data: Mapping[str, Any]) -> str:
        for field_name in self.config.text_fields:
            value = data.get(field_name)
            if value not in (None, ""):
                return str(value)
        return json.dumps(model_dump(data), ensure_ascii=False, sort_keys=True)

    def _apply_relevance_filter(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if not (
            self.config.include_keywords
            or self.config.exclude_keywords
            or self.config.include_themes
            or self.config.exclude_themes
        ):
            return row

        decision = decide_text_relevance(
            row.get("content_text"),
            include_keywords=self.config.include_keywords,
            exclude_keywords=self.config.exclude_keywords,
            include_themes=self.config.include_themes,
            exclude_themes=self.config.exclude_themes,
            min_keyword_hits=self.config.min_keyword_hits,
        )
        if not decision.relevant:
            return None
        row["matched_keywords"] = list(decision.matched_keywords)
        row["excluded_keywords"] = list(decision.excluded_keywords)
        row["matched_themes"] = list(decision.matched_themes)
        row["excluded_themes"] = list(decision.excluded_themes)
        row["keyword_hit_count"] = decision.hit_count
        row["relevance_version"] = decision.policy_version
        return row

    def _duckduckgo_search_rows(self, snapshot_text: str) -> Iterable[dict[str, Any]]:
        if "DuckDuckGo" not in snapshot_text or "## [" not in snapshot_text:
            return []

        rows: list[dict[str, Any]] = []
        for rank, match in enumerate(self.DUCKDUCKGO_RESULT_RE.finditer(snapshot_text), start=1):
            title = _collapse_ws(match.group("title"))
            redirect_url = match.group("link")
            body = _collapse_ws(_markdown_visible_text(match.group("body")))
            target_url = _decode_duckduckgo_target(redirect_url)
            content_text = _collapse_ws(" ".join(part for part in (title, body) if part))
            if not content_text:
                continue
            rows.append(
                {
                    "source_url": target_url,
                    "search_query_url": self.config.source_url,
                    "search_query": self.config.search_query,
                    "query_theme": self.config.query_theme,
                    "query_term": self.config.query_term,
                    "query_term_stage": self.config.query_term_stage,
                    "query_variant_index": self.config.query_variant_index,
                    "result_title": title,
                    "result_rank": rank,
                    "content_text": content_text,
                }
            )
        return rows


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


def _markdown_visible_text(value: str) -> str:
    without_images = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
    without_links = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", without_images)
    return _collapse_ws(without_links)


def _decode_duckduckgo_target(link: str) -> str:
    parsed = urlparse(link)
    query_target = parse_qs(parsed.query).get("uddg")
    if query_target:
        return unquote(query_target[0])
    return link


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        now = time.time()
        return max(0.0, retry_at.timestamp() - now)
    return max(0.0, seconds)


__all__ = [
    "HTTPFeedCollector",
    "HTTPFeedConfig",
    "NetworkCollectionDisabled",
    "SourceAuthorizationError",
    "classify_collection_failure",
]
