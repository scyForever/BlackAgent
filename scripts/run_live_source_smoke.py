"""Run an authorized live-collection smoke against a local loopback feed.

This proves the runtime can perform a real HTTP fetch with an authorization
header and compliance metadata, without touching external sites or relying on
private credentials during tests/demo.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cleaner.text_filter import normalize_text
from src.collector import HTTPFeedCollector, HTTPFeedConfig
from src.collector.base_collector import model_dump
from src.pipeline import IntelligencePipeline


DEFAULT_TOKEN = "BLACKAGENT_AUTHORIZED_LOOPBACK_SMOKE"
SOURCE_CLASS_SCENARIOS = (
    {
        "source_class": "im_or_group",
        "source_name": "loopback-authorized-im-feed",
        "source_type": "IM",
        "path": "/authorized-im-feed.json",
        "items": [
            {"full_text": "群控脚本接码上车，联系 TG:live001，落地 https://live-smoke.example/a"},
            {"full_text": "群控脚本接码上车，联系 TG:live001，落地 https://live-smoke.example/a"},
            {"full_text": "普通安全研究讨论，不含交易招募"},
        ],
        "include_keywords": ("群控", "接码", "私域"),
    },
    {
        "source_class": "social_or_forum",
        "source_name": "loopback-authorized-forum-feed",
        "source_type": "Forum",
        "path": "/authorized-forum-feed.json",
        "items": [
            {"full_text": "论坛帖子：私域导流返利拉新，开户链接 https://lead-smoke.example/b，联系 TG:live002"},
            {"full_text": "论坛帖子：群发广告投放业务，客户包量，联系 @forumops"},
            {"full_text": "平台公告：反诈安全通告与防护建议"},
        ],
        "include_keywords": ("私域", "返利", "群发", "投放"),
    },
    {
        "source_class": "vertical_or_technical",
        "source_name": "loopback-authorized-vertical-feed",
        "source_type": "Vertical",
        "path": "/authorized-vertical-feed.json",
        "items": [
            {"full_text": "垂直站点供给：账号批量出号，支持接码注册，价格 2U，客服 @vertical01"},
            {"full_text": "垂直站点供给：账号批量出号，支持接码注册，价格 2U，客服 @vertical01"},
            {"full_text": "站点帮助文档：合规账号安全设置"},
        ],
        "include_keywords": ("账号", "出号", "接码", "价格"),
    },
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local authorized live source collection smoke.")
    parser.add_argument("--output", default="data/source_live_smoke_report.json", help="Where to write the report JSON.")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token expected by the loopback feed.")
    return parser.parse_args(argv)


def run_smoke(*, token: str = DEFAULT_TOKEN) -> dict[str, Any]:
    server = _LiveSmokeServer(token=token)
    server.start()
    try:
        unauthorized_statuses = {
            scenario["source_class"]: _probe_without_authorization(server.url_for(str(scenario["path"])))
            for scenario in SOURCE_CLASS_SCENARIOS
        }
        started = time.perf_counter()
        source_reports: list[dict[str, Any]] = []
        all_records: list[dict[str, Any]] = []
        for scenario in SOURCE_CLASS_SCENARIOS:
            collector = HTTPFeedCollector(
                HTTPFeedConfig(
                    source_url=server.url_for(str(scenario["path"])),
                    source_name=str(scenario["source_name"]),
                    source_type=str(scenario["source_type"]),
                    legal_basis="INTERNAL_AUTHORIZED_SOURCE",
                    feed_format="json",
                    max_records=10,
                    timeout_seconds=3.0,
                    allowed_domains=("127.0.0.1", "localhost"),
                    headers={"Authorization": f"Bearer {token}"},
                    include_keywords=tuple(scenario["include_keywords"]),
                    network_enabled=True,
                )
            )
            records = [model_dump(item) for item in collector.collect()]
            classifications = IntelligencePipeline(load_runtime_llm_value=False).run(
                records,
                context={"quality_profile": "fast", "require_evidence_chain": False},
            ).classified
            source_reports.append(
                _source_report(
                    scenario=scenario,
                    records=records,
                    classifications=classifications,
                    unauthorized_status=unauthorized_statuses.get(str(scenario["source_class"])),
                )
            )
            all_records.extend(records)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        pipeline_result = IntelligencePipeline(load_runtime_llm_value=False).run(
            all_records,
            context={"quality_profile": "fast", "require_evidence_chain": False},
        )
        classifications = pipeline_result.classified
        return {
            "status": "completed",
            "run_type": "live_authorized_loopback_collection_smoke",
            "smoke_scope": "three_required_source_classes",
            "network_attempted": True,
            "loopback_only": True,
            "authorization_enforced": all(status == 401 for status in unauthorized_statuses.values()),
            "unauthorized_probe_statuses": unauthorized_statuses,
            "authorized_request_headers": ["Authorization"],
            "required_source_classes": [str(item["source_class"]) for item in SOURCE_CLASS_SCENARIOS],
            "covered_source_classes": [item["source_class"] for item in source_reports if item["collected_count"] > 0],
            "sources": source_reports,
            "source": {
                "source_name": "loopback-authorized-feed",
                "source_url": server.base_url,
                "allowed_domains": ["127.0.0.1", "localhost"],
                "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            },
            "fetched_count": len(all_records),
            "high_risk_candidate_count": sum(
                1
                for item in classifications
                if str(item.get("risk_category") or "").strip() not in {"", "unknown", "正常业务白噪声"}
            ),
            "classification_count": len(classifications),
            "elapsed_ms": elapsed_ms,
            "raw_records": all_records,
            "pipeline_summary": pipeline_result.execution_summary.model_dump(),
            "claim_boundary": (
                "This is a real authorized HTTP collection smoke against local loopback IM/forum/vertical feeds. "
                "It demonstrates live fetch, auth enforcement, parsing, filtering, de-dup metrics, and pipeline handoff; "
                "it does not claim external platform access."
            ),
        }
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_smoke(token=args.token)
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


class _LiveSmokeServer:
    def __init__(self, *, token: str) -> None:
        self.token = token
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def url_for(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, name="blackagent-live-smoke", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3.0)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        expected_token = self.token

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
                scenario = next((item for item in SOURCE_CLASS_SCENARIOS if self.path == item["path"]), None)
                if scenario is None:
                    self.send_error(404)
                    return
                if self.headers.get("Authorization") != f"Bearer {expected_token}":
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))
                    return
                body = {"items": scenario["items"]}
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature.
                return

        return Handler


def _probe_without_authorization(url: str) -> int:
    try:
        urllib_request.urlopen(url, timeout=3.0).read()  # noqa: S310 - local loopback smoke only.
    except urllib_error.HTTPError as exc:
        return int(exc.code)
    return 200


def _source_report(
    *,
    scenario: dict[str, Any],
    records: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    unauthorized_status: int | None,
) -> dict[str, Any]:
    normalized_texts = [normalize_text(str(record.get("content_text") or record.get("full_text") or "")) for record in records]
    duplicate_rate = 0.0
    if normalized_texts:
        duplicate_rate = round(1.0 - (len(set(normalized_texts)) / len(normalized_texts)), 4)
    high_risk = sum(
        1
        for item in classifications
        if str(item.get("risk_category") or "").strip() not in {"", "unknown", "正常业务白噪声"}
    )
    configured_count = len(scenario.get("items") or [])
    return {
        "source_class": str(scenario["source_class"]),
        "source_name": str(scenario["source_name"]),
        "source_type": str(scenario["source_type"]),
        "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
        "authorization_statement": (
            f"{scenario['source_name']}: local operator-owned loopback feed; bearer token required; "
            "used only for authorized smoke validation."
        ),
        "network_enabled": True,
        "run_type": "live_authorized_loopback_collection_smoke",
        "collected_count": len(records),
        "filtered_count": max(0, configured_count - len(records)),
        "duplicate_rate": duplicate_rate,
        "high_risk_candidate_count": high_risk,
        "failure_reason": None,
        "authorization_enforced": unauthorized_status == 401,
        "unauthorized_probe_status": unauthorized_status,
    }


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
