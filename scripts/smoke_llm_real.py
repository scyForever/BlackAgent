"""Real LLM link smoke test for BlackAgent.

This script intentionally prints only masked configuration and normalized result
metadata.  It is meant for a minimal paid-token check after configuring an
OpenAI-compatible provider in ``.env`` or the shell.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backend import LLMGateway, LLMGatewayResponse
from src.config_loader import load_project_env_file, load_settings


def _response_summary(response: LLMGatewayResponse) -> dict[str, Any]:
    return {
        "ok": response.ok,
        "network_attempted": response.network_attempted,
        "status_code": response.status_code,
        "model": response.model,
        "error": response.error,
        "parsed_json": response.parsed_json,
        "content_preview": response.content[:300],
    }


def _settings_summary(settings: Any) -> dict[str, Any]:
    parsed_url = urlparse(settings.llm.base_url or "")
    return {
        "provider": settings.llm.provider,
        "base_url_host": parsed_url.netloc,
        "base_url_path": parsed_url.path,
        "api_key_configured": bool(settings.llm.api_key),
        "model": settings.llm.model,
        "enabled": settings.llm.enabled,
        "dry_run": settings.llm.dry_run,
        "auth_header": settings.llm.auth_header,
        "max_tokens_param": settings.llm.max_tokens_param,
        "response_format_supported": settings.llm.response_format_supported,
        "extra_body_keys": sorted(settings.llm.extra_body.keys()),
        "timeout_seconds": settings.llm.timeout_seconds,
    }


def _make_gateway(settings: Any) -> LLMGateway:
    return LLMGateway(
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key,
        model=settings.llm.model,
        service_tier=settings.llm.service_tier,
        dry_run=(settings.llm.dry_run or not settings.llm.enabled),
        mock=settings.llm.provider.lower() == "mock",
        timeout_seconds=settings.llm.timeout_seconds,
        auth_header=settings.llm.auth_header,
        max_tokens_param=settings.llm.max_tokens_param,
        response_format_supported=settings.llm.response_format_supported,
        extra_body=settings.llm.extra_body,
    )


def _run_direct_gateway(settings: Any, message: str) -> LLMGatewayResponse:
    gateway = _make_gateway(settings)
    return gateway.chat(
        [
            {
                "role": "system",
                "content": "Return only a compact JSON object with fields pong, route, and note.",
            },
            {"role": "user", "content": message},
        ],
        temperature=0.0,
        max_tokens=120,
        response_format={"type": "json_object"},
    )


def _run_local_llm_gateway(settings: Any, message: str) -> dict[str, Any]:
    from src.local_runtime import LocalAgentRuntime

    runtime = LocalAgentRuntime(settings)
    try:
        payload = runtime.llm_chat(
            [
                {
                    "role": "system",
                    "content": "Return only a compact JSON object with fields pong, route, and note.",
                },
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=120,
            response_format={"type": "json_object"},
        )
    finally:
        runtime.close()
    return {"runtime_status": "ok", "payload": payload}


def _run_local_investigation(settings: Any) -> dict[str, Any]:
    from src.local_runtime import LocalAgentRuntime

    runtime = LocalAgentRuntime(settings)
    try:
        payload = runtime.run_investigation(
            "请复核最近 24 小时内接码、群控脚本相关的高质量黑灰产线索，输出可复核证据链。",
            fixture_items=[
                {
                    "trace_id": "llm-smoke-r1",
                    "source_name": "tg-smoke-a",
                    "source_type": "telegram",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "publish_time": "2026-05-28T01:00:00+08:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
                },
                {
                    "trace_id": "llm-smoke-r2",
                    "source_name": "forum-smoke-b",
                    "source_type": "Forum",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-28T01:05:00+08:00",
                    "content_text": "接码服务和群控工具组合售卖，TG:core01 复用相同落地域名 risk.example 第二条",
                },
                {
                    "trace_id": "llm-smoke-r3",
                    "source_name": "feed-smoke-c",
                    "source_type": "THREAT_INTEL",
                    "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                    "publish_time": "2026-05-28T01:10:00+08:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
                },
            ],
            max_sources=2,
        )
    finally:
        runtime.close()
    return {
        "runtime_status": "ok",
        "status": payload.get("status"),
        "mode": payload.get("mode"),
        "input_count": payload.get("input_count"),
        "high_quality_count": payload.get("high_quality_count"),
        "candidate_count": payload.get("candidate_count"),
        "execution_summary": payload.get("execution_summary"),
        "llm_trace_summary": [
            {
                "stage": item.get("stage"),
                "llm_ok": item.get("llm_ok"),
                "used_fallback": item.get("used_fallback"),
                "error": item.get("error"),
            }
            for item in payload.get("llm_traces", [])
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real OpenAI-compatible LLM smoke test.")
    parser.add_argument("--config", default=None, help="Optional config YAML path.")
    parser.add_argument("--message", default="ping", help="Small prompt for the direct/local LLM smoke call.")
    parser.add_argument("--force-real", action="store_true", help="Override settings to enabled=true and dry_run=false.")
    parser.add_argument("--skip-local-gateway", action="store_true", help="Skip the local runtime LLM gateway path.")
    parser.add_argument("--include-investigation", action="store_true", help="Also run local investigation with fixture data.")
    args = parser.parse_args(argv)

    load_project_env_file()
    settings = load_settings(args.config)
    if args.force_real:
        settings.llm.enabled = True
        settings.llm.dry_run = False

    print("CONFIG " + json.dumps(_settings_summary(settings), ensure_ascii=False, sort_keys=True))

    if not settings.llm.api_key:
        print("ERROR missing llm.api_key; set BLACKAGENT_LLM_API_KEY or llm.api_key.", file=sys.stderr)
        return 2
    if settings.llm.dry_run or not settings.llm.enabled:
        print("ERROR LLM is not in real mode; set BLACKAGENT_LLM_DRY_RUN=false or pass --force-real.", file=sys.stderr)
        return 2

    direct = _run_direct_gateway(settings, args.message)
    print("DIRECT " + json.dumps(_response_summary(direct), ensure_ascii=False, sort_keys=True))
    if not direct.ok or not direct.network_attempted:
        return 1

    if not args.skip_local_gateway:
        gateway_result = _run_local_llm_gateway(settings, args.message)
        print("LOCAL_GATEWAY " + json.dumps(gateway_result, ensure_ascii=False, sort_keys=True))
        gateway_payload = gateway_result.get("payload") or {}
        if gateway_result.get("runtime_status") != "ok" or not gateway_payload.get("ok") or not gateway_payload.get("network_attempted"):
            return 1

    if args.include_investigation:
        investigation_result = _run_local_investigation(settings)
        print("INVESTIGATION " + json.dumps(investigation_result, ensure_ascii=False, sort_keys=True))
        if investigation_result.get("runtime_status") != "ok":
            return 1
        for trace in investigation_result.get("llm_trace_summary") or []:
            if not trace.get("llm_ok"):
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
