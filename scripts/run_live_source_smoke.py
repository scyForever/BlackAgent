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

from src.collector import HTTPFeedCollector, HTTPFeedConfig
from src.collector.base_collector import model_dump
from src.pipeline import IntelligencePipeline


DEFAULT_TOKEN = "BLACKAGENT_AUTHORIZED_LOOPBACK_SMOKE"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local authorized live source collection smoke.")
    parser.add_argument("--output", default="data/source_live_smoke_report.json", help="Where to write the report JSON.")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token expected by the loopback feed.")
    return parser.parse_args(argv)


def run_smoke(*, token: str = DEFAULT_TOKEN) -> dict[str, Any]:
    server = _LiveSmokeServer(token=token)
    server.start()
    try:
        unauthorized_status = _probe_without_authorization(server.url)
        started = time.perf_counter()
        collector = HTTPFeedCollector(
            HTTPFeedConfig(
                source_url=server.url,
                source_name="loopback-authorized-feed",
                source_type="THREAT_INTEL",
                legal_basis="INTERNAL_AUTHORIZED_SOURCE",
                feed_format="json",
                max_records=10,
                timeout_seconds=3.0,
                allowed_domains=("127.0.0.1", "localhost"),
                headers={"Authorization": f"Bearer {token}"},
                include_keywords=("群控", "接码", "私域"),
                network_enabled=True,
            )
        )
        records = [model_dump(item) for item in collector.collect()]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        pipeline_result = IntelligencePipeline(load_runtime_llm_value=False).run(
            records,
            context={"quality_profile": "fast", "require_evidence_chain": False},
        )
        classifications = pipeline_result.classified
        return {
            "status": "completed",
            "run_type": "live_authorized_loopback_collection_smoke",
            "network_attempted": True,
            "loopback_only": True,
            "authorization_enforced": unauthorized_status == 401,
            "unauthorized_probe_status": unauthorized_status,
            "authorized_request_headers": ["Authorization"],
            "source": {
                "source_name": "loopback-authorized-feed",
                "source_url": server.url,
                "allowed_domains": ["127.0.0.1", "localhost"],
                "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            },
            "fetched_count": len(records),
            "high_risk_candidate_count": sum(
                1
                for item in classifications
                if str(item.get("risk_category") or "").strip() not in {"", "unknown", "正常业务白噪声"}
            ),
            "classification_count": len(classifications),
            "elapsed_ms": elapsed_ms,
            "raw_records": records,
            "pipeline_summary": pipeline_result.execution_summary.model_dump(),
            "claim_boundary": (
                "This is a real authorized HTTP collection smoke against a local loopback feed. "
                "It demonstrates live fetch, auth enforcement, parsing, filtering, and pipeline handoff; "
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
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/authorized-feed.json"

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
                if self.path != "/authorized-feed.json":
                    self.send_error(404)
                    return
                if self.headers.get("Authorization") != f"Bearer {expected_token}":
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))
                    return
                body = {
                    "items": [
                        {"full_text": "群控脚本接码上车，联系 TG:live001，落地 https://live-smoke.example/a"},
                        {"full_text": "私域导流返利拉新，开户链接 https://lead-smoke.example/b，联系 TG:live002"},
                    ]
                }
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


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
