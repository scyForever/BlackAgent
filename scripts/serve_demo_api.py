"""Stdlib one-click demo API/UI for local defense-only presentations."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

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
    report = {
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
    report["review_workbench"] = build_review_workbench(report)
    return report


def build_review_workbench(demo_report: Mapping[str, Any]) -> dict[str, Any]:
    """Build the local analyst review surface payload from a demo run."""

    clues = _demo_clues(demo_report)
    workbench_clues = [_workbench_clue(clue, index=index) for index, clue in enumerate(clues)]
    return {
        "status": "ready",
        "query": str(demo_report.get("query") or ""),
        "available_actions": ["confirm", "reject"],
        "clue_count": len(workbench_clues),
        "clues": workbench_clues,
        "export_preview": {
            "format": "json",
            "contains": ["clues", "evidence_snippets", "entity_highlights", "classification_result", "review_decisions"],
        },
    }


def record_review_decision(
    workbench: Mapping[str, Any],
    clue_id: str,
    *,
    decision: str,
    reviewer: str = "system",
    notes: str = "",
) -> dict[str, Any]:
    normalized = str(decision or "").strip().lower()
    status = {"confirm": "confirmed", "confirmed": "confirmed", "reject": "rejected", "rejected": "rejected"}.get(normalized)
    if status is None:
        raise ValueError("decision must be confirm or reject")
    clue_ids = {str(clue.get("clue_id") or "") for clue in _list_of_dicts(workbench.get("clues"))}
    if clue_id not in clue_ids:
        raise ValueError(f"unknown clue_id: {clue_id}")
    return {
        "clue_id": clue_id,
        "status": status,
        "reviewer": reviewer,
        "notes": notes,
        "decision_source": "local_review_workbench",
    }


def export_review_report(
    workbench: Mapping[str, Any],
    *,
    decisions: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    decisions_by_id = {str(item.get("clue_id") or ""): dict(item) for item in decisions or []}
    clues: list[dict[str, Any]] = []
    summary = {"confirmed": 0, "rejected": 0, "pending": 0}
    for clue in _list_of_dicts(workbench.get("clues")):
        exported = dict(clue)
        decision = decisions_by_id.get(str(clue.get("clue_id") or ""))
        if decision:
            exported["review_decision"] = decision
            if decision.get("status") in summary:
                summary[str(decision["status"])] += 1
        else:
            exported["review_decision"] = {"status": "pending"}
            summary["pending"] += 1
        clues.append(exported)
    return {
        "status": "completed",
        "query": str(workbench.get("query") or ""),
        "review_summary": summary,
        "clues": clues,
        "claim_boundary": "Local review export captures analyst decisions for demo evidence; it does not perform production enforcement.",
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
    state: dict[str, Any] = {"last_workbench": None, "decisions": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
            if self.path == "/health":
                self._write_json({"status": "ok", "service": "blackagent-demo-api"})
                return
            if self.path in {"/", "/index.html"}:
                self._write_html(_INDEX_HTML)
                return
            if self.path == "/api/export":
                workbench = state["last_workbench"] or build_review_workbench(run_demo_request({}))
                self._write_json(export_review_report(workbench, decisions=state["decisions"]))
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name.
            if self.path not in {"/api/demo", "/api/review"}:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(body or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object")
                if self.path == "/api/demo":
                    report = run_demo_request(payload)
                    state["last_workbench"] = report["review_workbench"]
                    state["decisions"] = []
                    self._write_json(report)
                    return
                workbench = state["last_workbench"] or build_review_workbench(run_demo_request({}))
                decision = record_review_decision(
                    workbench,
                    str(payload.get("clue_id") or ""),
                    decision=str(payload.get("decision") or ""),
                    reviewer=str(payload.get("reviewer") or "demo-analyst"),
                    notes=str(payload.get("notes") or ""),
                )
                state["decisions"].append(decision)
                self._write_json({"status": "completed", "decision": decision, "export": export_review_report(workbench, decisions=state["decisions"])})
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


def _demo_clues(demo_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    clues = _list_of_dicts(demo_report.get("top_clues"))
    if clues:
        return clues
    raw = demo_report.get("raw_result") if isinstance(demo_report.get("raw_result"), Mapping) else {}
    return _list_of_dicts(raw.get("high_quality_clues") or raw.get("candidate_clues"))


def _workbench_clue(clue: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    clue_id = str(clue.get("clue_id") or clue.get("key") or f"clue-{index + 1}")
    evidence_snippets = _evidence_snippets(clue)
    entity_highlights = _entity_highlights(clue, evidence_snippets)
    classification = _classification_result(clue, evidence_snippets)
    return {
        "clue_id": clue_id,
        "clue_type": clue.get("clue_type"),
        "risk_category": clue.get("risk_category"),
        "quality_score": clue.get("quality_score"),
        "confidence": clue.get("confidence"),
        "classification_result": classification,
        "evidence_trace_ids": list(clue.get("evidence_trace_ids") or []),
        "evidence_snippets": evidence_snippets,
        "entity_highlights": entity_highlights,
        "suggested_review_action": (clue.get("evidence_reviewability") or {}).get("suggested_review_action")
        if isinstance(clue.get("evidence_reviewability"), Mapping)
        else None,
        "review_status": "pending",
    }


def _evidence_snippets(clue: Mapping[str, Any]) -> list[dict[str, Any]]:
    reviewability = clue.get("evidence_reviewability") if isinstance(clue.get("evidence_reviewability"), Mapping) else {}
    cards = _list_of_dicts(reviewability.get("evidence_cards"))
    if cards:
        return [
            {
                "trace_id": card.get("trace_id"),
                "source_name": card.get("source_name"),
                "source_type": card.get("source_type"),
                "raw_snippet": card.get("raw_snippet") or card.get("summary") or card.get("clean_text"),
                "clean_text": card.get("clean_text"),
                "classification": card.get("classification") if isinstance(card.get("classification"), Mapping) else {},
                "entities": _list_of_dicts(card.get("entities")),
            }
            for card in cards
        ]
    snippets = [str(item) for item in reviewability.get("original_snippets") or [] if str(item).strip()]
    traces = [str(item) for item in clue.get("evidence_trace_ids") or [] if str(item).strip()]
    return [
        {
            "trace_id": traces[index] if index < len(traces) else None,
            "source_name": None,
            "source_type": None,
            "raw_snippet": snippet,
            "clean_text": snippet,
            "classification": _classification_result(clue, []),
            "entities": [{"normalized_value": value} for value in clue.get("entity_values") or []],
        }
        for index, snippet in enumerate(snippets)
    ]


def _entity_highlights(clue: Mapping[str, Any], evidence_snippets: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in clue.get("entity_values") or []:
        normalized = str(value or "").strip()
        if normalized:
            seen.add(("unknown", normalized))
            entities.append({"entity_type": "unknown", "normalized_value": normalized, "raw_value": normalized})
    for snippet in evidence_snippets:
        for entity in _list_of_dicts(snippet.get("entities")):
            normalized = str(entity.get("normalized_value") or entity.get("raw_value") or entity.get("value") or "").strip()
            entity_type = str(entity.get("entity_type") or entity.get("type") or "unknown")
            key = (entity_type, normalized)
            if not normalized or key in seen:
                continue
            seen.add(key)
            entities.append({**entity, "entity_type": entity_type, "normalized_value": normalized})
    return entities


def _classification_result(clue: Mapping[str, Any], evidence_snippets: list[Mapping[str, Any]]) -> dict[str, Any]:
    for snippet in evidence_snippets:
        classification = snippet.get("classification")
        if isinstance(classification, Mapping) and classification:
            return dict(classification)
    return {
        "risk_category": clue.get("risk_category"),
        "secondary_label": clue.get("secondary_label"),
        "confidence": clue.get("confidence"),
        "review_required": clue.get("review_required"),
    }


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


_INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>BlackAgent 答辩 Demo</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:1120px;margin:28px auto;padding:0 18px;background:#111318;color:#f3f4f6}
textarea{width:100%;height:76px;border-radius:6px;padding:12px;background:#ffffff;color:#111827}
button{margin:10px 8px 10px 0;padding:9px 14px;border:0;border-radius:6px;background:#2563eb;color:white;font-weight:700}
button.reject{background:#b91c1c}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:18px}
.panel{border:1px solid #303642;border-radius:8px;padding:14px;background:#181c24}
.clue{border-top:1px solid #303642;padding:12px 0}
.entity{display:inline-block;margin:3px 5px 3px 0;padding:2px 6px;border-radius:4px;background:#234238;color:#bbf7d0}
.snippet{margin:8px 0;padding:8px;border-left:3px solid #60a5fa;background:#101827}
pre{white-space:pre-wrap;background:#0b1020;padding:12px;border-radius:8px;max-height:360px;overflow:auto}
.muted{color:#aeb7c8}
@media(max-width:860px){.layout{grid-template-columns:1fr}}
</style>
<h1>BlackAgent 本地审阅工作台</h1>
<textarea id="query">分析 demo 样本中的接码、群控、引流和账号交易风险线索</textarea>
<br><button onclick="runDemo()">运行 Demo</button><button onclick="exportReport()">导出报告</button>
<div class="layout">
  <section class="panel"><h2>线索审阅</h2><div id="clues" class="muted">等待运行...</div></section>
  <section class="panel"><h2>报告</h2><pre id="out">等待运行...</pre></section>
</div>
<script>
let workbench = null;
async function runDemo(){
  const res = await fetch('/api/demo', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({query:document.getElementById('query').value})});
  const data = await res.json();
  workbench = data.review_workbench;
  document.getElementById('out').textContent = JSON.stringify(data, null, 2);
  renderWorkbench(workbench);
}
function renderWorkbench(wb){
  const root = document.getElementById('clues');
  if(!wb || !wb.clues || !wb.clues.length){ root.textContent = '暂无线索'; return; }
  root.innerHTML = wb.clues.map(clue => `
    <div class="clue">
      <strong>${escapeHtml(clue.clue_id)}</strong> ${escapeHtml(clue.classification_result.risk_category || '')} / ${escapeHtml(clue.classification_result.secondary_label || '')}
      <div>${(clue.entity_highlights || []).map(e => `<span class="entity">${escapeHtml(e.normalized_value || '')}</span>`).join('')}</div>
      ${(clue.evidence_snippets || []).map(s => `<div class="snippet">${escapeHtml(s.raw_snippet || s.clean_text || '')}</div>`).join('')}
      <button onclick="review('${escapeAttr(clue.clue_id)}','confirm')">确认</button>
      <button class="reject" onclick="review('${escapeAttr(clue.clue_id)}','reject')">驳回</button>
    </div>`).join('');
}
async function review(clueId, decision){
  const res = await fetch('/api/review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({clue_id:clueId, decision, reviewer:'demo-analyst'})});
  document.getElementById('out').textContent = JSON.stringify(await res.json(), null, 2);
}
async function exportReport(){
  const res = await fetch('/api/export');
  document.getElementById('out').textContent = JSON.stringify(await res.json(), null, 2);
}
function escapeHtml(value){ return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
function escapeAttr(value){ return String(value ?? '').replace(/['\\\\]/g, ''); }
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
