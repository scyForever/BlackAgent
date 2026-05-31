"""OpenAI-compatible chat-completions gateway.

The implementation uses only the Python standard library so the backend slice
can run in the current project without adding deployment dependencies.  Real
network calls are made only when an API key is configured and dry-run/mock mode
is disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request as urllib_request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"


@dataclass(frozen=True)
class LLMGatewayConfig:
    """Configuration for an OpenAI-compatible chat completion endpoint."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    service_tier: str | None = None
    dry_run: bool = True
    mock: bool = False
    timeout_seconds: float = 30.0
    auth_header: str = "authorization"
    max_tokens_param: str = "max_tokens"
    response_format_supported: bool = True
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "LLMGatewayConfig":
        api_key = os.getenv("BLACKAGENT_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        dry_run = _env_bool("BLACKAGENT_LLM_DRY_RUN", default=not bool(api_key))
        return cls(
            base_url=os.getenv("BLACKAGENT_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
            api_key=api_key,
            model=os.getenv("BLACKAGENT_LLM_MODEL") or DEFAULT_MODEL,
            service_tier=os.getenv("BLACKAGENT_LLM_SERVICE_TIER") or None,
            dry_run=dry_run,
            mock=_env_bool("BLACKAGENT_LLM_MOCK", default=False),
            timeout_seconds=float(os.getenv("BLACKAGENT_LLM_TIMEOUT_SECONDS", "30")),
            auth_header=_normalize_auth_header(os.getenv("BLACKAGENT_LLM_AUTH_HEADER") or "authorization"),
            max_tokens_param=_normalize_max_tokens_param(os.getenv("BLACKAGENT_LLM_MAX_TOKENS_PARAM") or "max_tokens"),
            response_format_supported=_env_bool("BLACKAGENT_LLM_RESPONSE_FORMAT_SUPPORTED", default=True),
            extra_body=_env_json_object("BLACKAGENT_LLM_EXTRA_BODY"),
        )


@dataclass
class LLMGatewayResponse:
    """Normalized response returned by the gateway."""

    ok: bool
    model: str
    content: str
    raw: dict[str, Any] = field(default_factory=dict)
    parsed_json: dict[str, Any] | None = None
    network_attempted: bool = False
    error: str | None = None
    status_code: int | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "model": self.model,
            "content": self.content,
            "raw": self.raw,
            "parsed_json": self.parsed_json,
            "network_attempted": self.network_attempted,
            "error": self.error,
            "status_code": self.status_code,
        }


@dataclass(frozen=True)
class LLMCallStats:
    """Observable metadata for one gateway call."""

    stage: str
    model: str
    prompt_tokens_estimated: int
    completion_tokens_limit: int
    elapsed_ms: int
    cache_hit: bool
    ok: bool
    error: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "model": self.model,
            "prompt_tokens_estimated": self.prompt_tokens_estimated,
            "completion_tokens_limit": self.completion_tokens_limit,
            "elapsed_ms": self.elapsed_ms,
            "cache_hit": self.cache_hit,
            "ok": self.ok,
            "error": self.error,
        }


class LLMGateway:
    """Minimal OpenAI-compatible LLM adapter with deterministic mock mode."""

    def __init__(
        self,
        config: LLMGatewayConfig | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        service_tier: str | None = None,
        dry_run: bool | None = None,
        mock: bool | None = None,
        timeout_seconds: float | None = None,
        auth_header: str | None = None,
        max_tokens_param: str | None = None,
        response_format_supported: bool | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        env_config = config or LLMGatewayConfig.from_env()
        resolved_api_key = api_key if api_key is not None else env_config.api_key
        resolved_dry_run = dry_run if dry_run is not None else (env_config.dry_run if config else not bool(resolved_api_key))
        resolved_extra_body = dict(env_config.extra_body)
        if extra_body is not None:
            resolved_extra_body.update(dict(extra_body))

        self.config = LLMGatewayConfig(
            base_url=base_url or env_config.base_url,
            api_key=resolved_api_key,
            model=model or env_config.model,
            service_tier=service_tier if service_tier is not None else env_config.service_tier,
            dry_run=resolved_dry_run,
            mock=mock if mock is not None else env_config.mock,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else env_config.timeout_seconds,
            auth_header=_normalize_auth_header(auth_header if auth_header is not None else env_config.auth_header),
            max_tokens_param=_normalize_max_tokens_param(
                max_tokens_param if max_tokens_param is not None else env_config.max_tokens_param
            ),
            response_format_supported=(
                response_format_supported if response_format_supported is not None else env_config.response_format_supported
            ),
            extra_body=resolved_extra_body,
        )
        self._cache: dict[str, LLMGatewayResponse] = {}
        self._stats: list[LLMCallStats] = []

    @property
    def endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        stage: str = "chat",
        budget: Any | None = None,
        cache_policy: str = "none",
        cache_key: str | None = None,
        deadline_ms: int | None = None,
    ) -> LLMGatewayResponse:
        """Send or simulate a chat completion request."""

        return self.chat_completions(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            extra_body=extra_body,
            stage=stage,
            budget=budget,
            cache_policy=cache_policy,
            cache_key=cache_key,
            deadline_ms=deadline_ms,
        )

    def chat_completions(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        stage: str = "chat",
        budget: Any | None = None,
        cache_policy: str = "none",
        cache_key: str | None = None,
        deadline_ms: int | None = None,
    ) -> LLMGatewayResponse:
        """Call an OpenAI-compatible ``/chat/completions`` endpoint."""

        started_at = time.perf_counter()
        payload = self._build_payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            extra_body=extra_body,
        )
        stage_name = str(stage or "chat")
        completion_limit = int(max_tokens or 0)
        prompt_tokens_estimated = _estimate_tokens(messages)
        estimated_tokens = prompt_tokens_estimated + completion_limit
        budget_estimated_tokens = int((extra_body or {}).get("budget_estimated_tokens") or estimated_tokens) if isinstance(extra_body, Mapping) else estimated_tokens
        resolved_cache_key = str(cache_key or "").strip() or _cache_key(payload, stage=stage_name)
        normalized_cache_policy = str(cache_policy or "none").strip().lower()
        budget_item_count = max(1, int((extra_body or {}).get("budget_item_count") or 1)) if isinstance(extra_body, Mapping) else 1

        if normalized_cache_policy in {"read", "read_write"} and resolved_cache_key in self._cache:
            cached = self._cache[resolved_cache_key]
            response = LLMGatewayResponse(
                ok=cached.ok,
                model=cached.model,
                content=cached.content,
                raw={**cached.raw, "cache_hit": True},
                parsed_json=cached.parsed_json,
                network_attempted=False,
                error=cached.error,
                status_code=cached.status_code,
            )
            if budget is not None and hasattr(budget, "consume_llm"):
                try:
                    budget.consume_llm(
                        stage=stage_name,
                        estimated_tokens=budget_estimated_tokens,
                        item_count=budget_item_count,
                        cache_hit=True,
                        ok=response.ok,
                        network=False,
                    )
                except TypeError:
                    budget.consume_llm(stage=stage_name, estimated_tokens=budget_estimated_tokens)
            self._record_stats(
                stage=stage_name,
                started_at=started_at,
                prompt_tokens_estimated=estimated_tokens,
                completion_tokens_limit=completion_limit,
                cache_hit=True,
                response=response,
            )
            return response

        if budget is not None and hasattr(budget, "allow_llm_call"):
            try:
                allowed = budget.allow_llm_call(
                    stage=stage_name,
                    estimated_tokens=budget_estimated_tokens,
                    item_count=budget_item_count,
                )
            except TypeError:
                allowed = budget.allow_llm_call(stage=stage_name, estimated_tokens=budget_estimated_tokens)
            if not allowed:
                response = self._blocked_response("budget_exhausted", f"LLM budget denied for stage={stage_name}")
                self._record_stats(
                    stage=stage_name,
                    started_at=started_at,
                    prompt_tokens_estimated=estimated_tokens,
                    completion_tokens_limit=completion_limit,
                    cache_hit=False,
                    response=response,
                )
                return response

        if self.config.mock or self.config.dry_run:
            response = self._mock_response(payload)
            if budget is not None and hasattr(budget, "consume_llm"):
                try:
                    budget.consume_llm(
                        stage=stage_name,
                        estimated_tokens=budget_estimated_tokens,
                        item_count=budget_item_count,
                        cache_hit=False,
                        ok=response.ok,
                        network=False,
                    )
                except TypeError:
                    budget.consume_llm(stage=stage_name, estimated_tokens=budget_estimated_tokens)
            if normalized_cache_policy in {"write", "read_write"}:
                self._cache[resolved_cache_key] = response
            self._record_stats(
                stage=stage_name,
                started_at=started_at,
                prompt_tokens_estimated=estimated_tokens,
                completion_tokens_limit=completion_limit,
                cache_hit=False,
                response=response,
            )
            return response

        if not self.config.api_key:
            response = self._blocked_response("missing_api_key", "api_key is required before a real LLM request")
            self._record_stats(
                stage=stage_name,
                started_at=started_at,
                prompt_tokens_estimated=estimated_tokens,
                completion_tokens_limit=completion_limit,
                cache_hit=False,
                response=response,
            )
            return response

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "BlackAgent-LLMGateway/0.1",
        }
        if self.config.auth_header == "api-key":
            headers["api-key"] = self.config.api_key
        else:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib_request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers=headers,
        )

        timeout_seconds = self.config.timeout_seconds
        if deadline_ms is not None and deadline_ms > 0:
            timeout_seconds = min(timeout_seconds, max(float(deadline_ms) / 1000.0, 0.001))

        try:
            with urllib_request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                raw_body = response.read().decode("utf-8")
                try:
                    raw = json.loads(raw_body) if raw_body else {}
                except json.JSONDecodeError as exc:
                    gateway_response = LLMGatewayResponse(
                        ok=False,
                        model=self.config.model,
                        content=raw_body,
                        raw={"raw_body": raw_body[:1000]},
                        network_attempted=True,
                        error=f"invalid_json_response:{exc.msg}",
                        status_code=getattr(response, "status", None),
                    )
                    self._record_stats(
                        stage=stage_name,
                        started_at=started_at,
                        prompt_tokens_estimated=estimated_tokens,
                        completion_tokens_limit=completion_limit,
                        cache_hit=False,
                        response=gateway_response,
                    )
                    return gateway_response
                status_code = getattr(response, "status", None)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            response = LLMGatewayResponse(
                ok=False,
                model=self.config.model,
                content="",
                raw={"error_body": body},
                network_attempted=True,
                error=f"http_error:{exc.code}",
                status_code=exc.code,
            )
            self._record_stats(
                stage=stage_name,
                started_at=started_at,
                prompt_tokens_estimated=estimated_tokens,
                completion_tokens_limit=completion_limit,
                cache_hit=False,
                response=response,
            )
            return response
        except urllib.error.URLError as exc:
            response = LLMGatewayResponse(
                ok=False,
                model=self.config.model,
                content="",
                raw={},
                network_attempted=True,
                error=f"url_error:{exc.reason}",
            )
            self._record_stats(
                stage=stage_name,
                started_at=started_at,
                prompt_tokens_estimated=estimated_tokens,
                completion_tokens_limit=completion_limit,
                cache_hit=False,
                response=response,
            )
            return response
        except (TimeoutError, socket.timeout) as exc:
            response = LLMGatewayResponse(
                ok=False,
                model=self.config.model,
                content="",
                raw={},
                network_attempted=True,
                error=f"timeout:{exc}",
            )
            self._record_stats(
                stage=stage_name,
                started_at=started_at,
                prompt_tokens_estimated=estimated_tokens,
                completion_tokens_limit=completion_limit,
                cache_hit=False,
                response=response,
            )
            return response
        except OSError as exc:
            response = LLMGatewayResponse(
                ok=False,
                model=self.config.model,
                content="",
                raw={},
                network_attempted=True,
                error=f"os_error:{exc}",
            )
            self._record_stats(
                stage=stage_name,
                started_at=started_at,
                prompt_tokens_estimated=estimated_tokens,
                completion_tokens_limit=completion_limit,
                cache_hit=False,
                response=response,
            )
            return response

        content = _extract_message_content(raw)
        response = LLMGatewayResponse(
            ok=True,
            model=str(raw.get("model") or self.config.model),
            content=content,
            raw=raw,
            parsed_json=_try_parse_json(content),
            network_attempted=True,
            status_code=status_code,
        )
        if budget is not None and hasattr(budget, "consume_llm"):
            try:
                    budget.consume_llm(
                        stage=stage_name,
                        estimated_tokens=budget_estimated_tokens,
                        item_count=budget_item_count,
                        cache_hit=False,
                        ok=response.ok,
                        network=response.network_attempted,
                    )
            except TypeError:
                budget.consume_llm(stage=stage_name, estimated_tokens=budget_estimated_tokens)
        if normalized_cache_policy in {"write", "read_write"}:
            self._cache[resolved_cache_key] = response
        self._record_stats(
            stage=stage_name,
            started_at=started_at,
            prompt_tokens_estimated=estimated_tokens,
            completion_tokens_limit=completion_limit,
            cache_hit=False,
            response=response,
        )
        return response

    def stats(self) -> list[dict[str, Any]]:
        """Return call statistics accumulated by this gateway instance."""

        return [item.model_dump() for item in self._stats]

    def stats_count(self) -> int:
        """Return the current stats cursor for run-scoped telemetry."""

        return len(self._stats)

    def stats_since(self, start_index: int) -> list[dict[str, Any]]:
        """Return call statistics recorded after ``start_index``."""

        try:
            index = int(start_index)
        except (TypeError, ValueError):
            index = 0
        index = max(0, min(index, len(self._stats)))
        return [item.model_dump() for item in self._stats[index:]]

    def clear_cache(self) -> None:
        self._cache.clear()

    def _record_stats(
        self,
        *,
        stage: str,
        started_at: float,
        prompt_tokens_estimated: int,
        completion_tokens_limit: int,
        cache_hit: bool,
        response: LLMGatewayResponse,
    ) -> None:
        self._stats.append(
            LLMCallStats(
                stage=stage,
                model=response.model,
                prompt_tokens_estimated=prompt_tokens_estimated,
                completion_tokens_limit=completion_tokens_limit,
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                cache_hit=cache_hit,
                ok=response.ok,
                error=response.error,
            )
        )

    def _build_payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        response_format: Mapping[str, Any] | None,
        extra_body: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not messages:
            raise ValueError("messages must not be empty")

        normalized_messages: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role", "")).strip()
            if not role:
                raise ValueError("each message requires a role")
            normalized_messages.append(dict(message))

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": normalized_messages,
            "temperature": temperature,
        }
        if self.config.extra_body:
            payload.update(dict(self.config.extra_body))
        runtime_extra_body = dict(extra_body or {})
        runtime_extra_body.pop("budget_item_count", None)
        runtime_extra_body.pop("budget_estimated_tokens", None)
        if max_tokens is not None:
            payload[self.config.max_tokens_param] = max_tokens
        if self.config.service_tier:
            payload["service_tier"] = self.config.service_tier
        if response_format is not None and self.config.response_format_supported:
            payload["response_format"] = dict(response_format)
        if runtime_extra_body:
            payload.update(runtime_extra_body)
        return payload

    def _mock_response(self, payload: Mapping[str, Any]) -> LLMGatewayResponse:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        parsed = {
            "mock": True,
            "input_digest": digest,
            "model": self.config.model,
            "service_tier": self.config.service_tier,
            "message_count": len(payload.get("messages", [])),
            "confidence": 0.0,
            "requires_human_review": True,
            "evidence": [],
        }
        content = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        return LLMGatewayResponse(
            ok=True,
            model=self.config.model,
            content=content,
            raw={
                "id": f"mock-chatcmpl-{digest}",
                "object": "chat.completion",
                "model": self.config.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            },
            parsed_json=parsed,
            network_attempted=False,
            status_code=200,
        )

    def _blocked_response(self, error: str, message: str) -> LLMGatewayResponse:
        parsed = {
            "error": error,
            "message": message,
            "network_attempted": False,
        }
        return LLMGatewayResponse(
            ok=False,
            model=self.config.model,
            content=json.dumps(parsed, sort_keys=True, ensure_ascii=False),
            raw={"error": parsed},
            parsed_json=parsed,
            network_attempted=False,
            error=error,
        )


def _extract_message_content(raw: Mapping[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def _try_parse_json(content: str) -> dict[str, Any] | None:
    candidates = [content]
    fenced = _strip_markdown_code_fence(content)
    if fenced and fenced not in candidates:
        candidates.append(fenced)
    extracted = _extract_json_candidate(content)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return None


def _estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    text = json.dumps(list(messages), ensure_ascii=False, sort_keys=True, default=str)
    return max(1, len(text) // 4)


def _cache_key(payload: Mapping[str, Any], *, stage: str = "chat", prompt_version: str | None = None) -> str:
    cache_payload = {
        "stage": stage,
        "prompt_version": prompt_version or "default",
        "payload": payload,
    }
    canonical = json.dumps(cache_payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strip_markdown_code_fence(content: str) -> str | None:
    if not isinstance(content, str):
        return None
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()


def _extract_json_candidate(content: str) -> str | None:
    if not isinstance(content, str):
        return None
    positions = [index for index in (content.find("{"), content.find("[")) if index >= 0]
    if not positions:
        return None
    start = min(positions)
    opening = content[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return content[start : index + 1].strip()
    return None


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_json_object(name: str) -> dict[str, Any]:
    value = os.getenv(name)
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


def _normalize_auth_header(value: str) -> str:
    normalized = str(value or "authorization").strip().lower().replace("_", "-")
    if normalized in {"bearer", "authorization"}:
        return "authorization"
    if normalized == "api-key":
        return "api-key"
    raise ValueError("auth_header must be one of authorization, bearer, api-key")


def _normalize_max_tokens_param(value: str) -> str:
    normalized = str(value or "max_tokens").strip()
    if normalized not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("max_tokens_param must be max_tokens or max_completion_tokens")
    return normalized


__all__ = ["LLMCallStats", "LLMGateway", "LLMGatewayConfig", "LLMGatewayResponse", "urllib_request"]

