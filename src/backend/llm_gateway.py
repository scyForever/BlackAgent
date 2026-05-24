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
    ) -> None:
        env_config = config or LLMGatewayConfig.from_env()
        resolved_api_key = api_key if api_key is not None else env_config.api_key
        resolved_dry_run = dry_run if dry_run is not None else (env_config.dry_run if config else not bool(resolved_api_key))

        self.config = LLMGatewayConfig(
            base_url=base_url or env_config.base_url,
            api_key=resolved_api_key,
            model=model or env_config.model,
            service_tier=service_tier if service_tier is not None else env_config.service_tier,
            dry_run=resolved_dry_run,
            mock=mock if mock is not None else env_config.mock,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else env_config.timeout_seconds,
        )

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
    ) -> LLMGatewayResponse:
        """Send or simulate a chat completion request."""

        return self.chat_completions(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            extra_body=extra_body,
        )

    def chat_completions(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> LLMGatewayResponse:
        """Call an OpenAI-compatible ``/chat/completions`` endpoint."""

        payload = self._build_payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            extra_body=extra_body,
        )

        if self.config.mock or self.config.dry_run:
            return self._mock_response(payload)

        if not self.config.api_key:
            return self._blocked_response("missing_api_key", "api_key is required before a real LLM request")

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib_request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "BlackAgent-LLMGateway/0.1",
            },
        )

        try:
            with urllib_request.urlopen(request, timeout=self.config.timeout_seconds) as response:  # noqa: S310
                raw_body = response.read().decode("utf-8")
                raw = json.loads(raw_body) if raw_body else {}
                status_code = getattr(response, "status", None)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return LLMGatewayResponse(
                ok=False,
                model=self.config.model,
                content="",
                raw={"error_body": body},
                network_attempted=True,
                error=f"http_error:{exc.code}",
                status_code=exc.code,
            )
        except urllib.error.URLError as exc:
            return LLMGatewayResponse(
                ok=False,
                model=self.config.model,
                content="",
                raw={},
                network_attempted=True,
                error=f"url_error:{exc.reason}",
            )

        content = _extract_message_content(raw)
        return LLMGatewayResponse(
            ok=True,
            model=str(raw.get("model") or self.config.model),
            content=content,
            raw=raw,
            parsed_json=_try_parse_json(content),
            network_attempted=True,
            status_code=status_code,
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
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if self.config.service_tier:
            payload["service_tier"] = self.config.service_tier
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        if extra_body:
            payload.update(dict(extra_body))
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
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = ["LLMGateway", "LLMGatewayConfig", "LLMGatewayResponse", "urllib_request"]
