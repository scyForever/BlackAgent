"""Stdlib one-click demo API/UI for local defense-only presentations."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, PROJECT_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from blackagent.interfaces.cli.main import DEFAULT_DEMO_QUERY, DEMO_RECORDS, run_agent
from src.config_loader import NetworkConfig, Settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve BlackAgent's local demo API/UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default keeps the demo local-only.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port.")
    parser.add_argument("--oneshot-output", default=None, help="If set, run one demo request and write JSON instead of serving.")
    return parser.parse_args(argv)


def run_demo_request(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the same local demo used by the HTTP API without starting a server."""

    payload = dict(payload or {})
    query = str(payload.get("query") or DEFAULT_DEMO_QUERY)
    records = payload.get("records") if isinstance(payload.get("records"), list) else DEMO_RECORDS
    routing_profile = str(payload.get("routing_profile") or "fast")
    status_code, result = run_agent(
        {
            "query": query,
            "fixture_items": records,
            "routing_profile": routing_profile,
            "policy_override": {
                "live_collection_enabled": False,
                "enable_llm_record_enrich": False,
                "enable_llm_clue_refine": False,
            },
        },
        _demo_settings(),
    )
    return {
        "status": "completed" if status_code == 200 and result.get("status") != "failed" else "failed",
        "http_status": status_code,
        "run_type": "local_one_click_defense_demo",
        "query": query,
        "routing_profile": routing_profile,
        "input_count": result.get("input_count"),
        "high_quality_count": result.get("high_quality_count"),
        "candidate_count": result.get("candidate_count"),
        "execution_summary": result.get("execution_summary") or {},
        "top_clues": (result.get("high_quality_clues") or result.get("candidate_clues") or [])[:5],
        "raw_result": result,
        "claim_boundary": "Local-only stdlib HTTP demo; no FastAPI/uvicorn dependency and no external collection by default.",
    }


def serve(*, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), _handler())
    print(f"BlackAgent demo UI/API: http://{host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.oneshot_output:
        report = run_demo_request({})
        output = _project_path(args.oneshot_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "completed" else 1
    serve(host=args.host, port=args.port)
    return 0


def _handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
            if self.path == "/health":
                self._write_json({"status": "ok", "service": "blackagent-demo-api"})
                return
            if self.path in {"/", "/index.html"}:
                self._write_html(_INDEX_HTML)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name.
            if self.path != "/api/demo":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(body or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object")
                self._write_json(run_demo_request(payload))
            except Exception as exc:  # noqa: BLE001 - normalized demo API error.
                self._write_json({"status": "failed", "error": str(exc), "error_type": type(exc).__name__}, status=400)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature.
            return

        def _write_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _demo_settings() -> Settings:
    return Settings(network=NetworkConfig(enabled=False))


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


_INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>BlackAgent 答辩 Demo</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:960px;margin:36px auto;padding:0 18px;background:#0b1020;color:#eef3ff}
textarea{width:100%;height:88px;border-radius:10px;padding:12px}
button{margin:12px 0;padding:10px 18px;border:0;border-radius:10px;background:#5b8cff;color:white;font-weight:700}
pre{white-space:pre-wrap;background:#111936;padding:16px;border-radius:12px}
.muted{color:#9fb0d8}
</style>
<h1>BlackAgent 本地答辩 Demo</h1>
<p class="muted">本页面只调用本机 stdlib demo API，默认不联网、不启动 FastAPI/uvicorn。</p>
<textarea id="query">分析 demo 样本中的接码、群控、引流和账号交易风险线索</textarea>
<br><button onclick="runDemo()">运行 Demo</button>
<pre id="out">等待运行...</pre>
<script>
async function runDemo(){
  const res = await fetch('/api/demo', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({query:document.getElementById('query').value})});
  document.getElementById('out').textContent = JSON.stringify(await res.json(), null, 2);
}
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
