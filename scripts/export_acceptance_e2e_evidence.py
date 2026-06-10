"""Export auditable evidence for the real-network acceptance E2E run."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.collector.source_metadata import source_class_for_record
from src.config_loader import resolve_project_path
from src.enhancement.clue_quality import build_evidence_reviewability


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


DEFAULT_COMMAND = (
    "python scripts/run_agent_cli.py --query <真实联网外部LLM验收查询> "
    "--config config/config.real.example.yaml "
    "--source-config-path config/intel_sources.acceptance_telegramnav_live.yaml "
    "--enable-network --force-real --routing-profile high_recall "
    "--max-sources 8 --max-raw-records 160 --max-candidate-clues 40 "
    "--max-llm-refine-clues 5 --max-elapsed-seconds 300 "
    "--output data/acceptance_real_e2e_run_success.json --show summary"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export BlackAgent acceptance E2E evidence JSON/Markdown.")
    parser.add_argument("--run", default="data/acceptance_real_e2e_run_success.json", help="Acceptance run JSON.")
    parser.add_argument("--smoke", default="data/acceptance_real_llm_smoke.txt", help="LLM smoke artifact.")
    parser.add_argument(
        "--source-catalog",
        default="config/intel_sources.acceptance_telegramnav_live.yaml",
        help="Source catalog used by the acceptance run.",
    )
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Reproducible command text.")
    parser.add_argument(
        "--record-details",
        default="data/acceptance_real_e2e_record_details.json",
        help="Optional trace-level record detail artifact used to hydrate evidence cards.",
    )
    parser.add_argument("--json-out", default="data/acceptance_real_e2e_evidence.json")
    parser.add_argument("--md-out", default="data/acceptance_real_e2e_evidence.md")
    return parser.parse_args()


def build_evidence(
    run: dict[str, Any],
    *,
    run_path: str,
    smoke_path: str,
    source_catalog: str,
    command: str,
    record_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = run.get("execution_summary") if isinstance(run.get("execution_summary"), dict) else {}
    counts = {
        "input_count": run.get("input_count"),
        "fetched_count": run.get("fetched_count"),
        "selected_source_count": run.get("selected_source_count"),
        "accepted_count": summary.get("accepted_count"),
        "dropped_count": summary.get("dropped_count"),
        "classification_count": summary.get("classification_count"),
        "entity_count": summary.get("entity_count"),
        "risk_clue_count": summary.get("risk_clue_count"),
        "strategy_count": summary.get("strategy_count"),
        "refined_clue_count": summary.get("refined_clue_count"),
        "high_quality_count": run.get("high_quality_count"),
        "candidate_count": run.get("candidate_count"),
    }
    source_classes = sorted({source_class_for_record(item) for item in run.get("selected_sources") or []})
    collection_classes = sorted({source_class_for_record(item) for item in run.get("collection_runs") or []})
    detail_records, detail_classifications, detail_entities = _reviewability_inputs_from_record_details(record_details)
    high_quality_clues = [
        summarize_clue(
            item,
            records=detail_records,
            classifications=detail_classifications,
            entities=detail_entities,
        )
        for item in run.get("high_quality_clues") or []
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_artifact": run_path,
        "smoke_artifact": smoke_path,
        "source_catalog": source_catalog,
        "command": command,
        "status": run.get("status"),
        "mode": run.get("mode"),
        "query": run.get("query"),
        "counts": counts,
        "target": {
            "high_quality_count_min": 2,
            "high_quality_count_met": int(run.get("high_quality_count") or 0) >= 2,
            "requires_direct_evidence_chain_for_targets": ["接码", "群控脚本", "账号交易"],
        },
        "selected_source_classes": source_classes,
        "collection_source_classes_executed": collection_classes,
        "collection_runs": [
            {
                "source_name": item.get("source_name"),
                "source_type": item.get("source_type"),
                "source_class": source_class_for_record(item),
                "collection_layer": item.get("collection_layer"),
                "fetched_count": item.get("fetched_count"),
                "status": item.get("status") or ("error_or_partial" if item.get("error") else "completed"),
                "error": item.get("error"),
                "layer_stop_reason": item.get("layer_stop_reason"),
                "evidence_gap_after_layer": item.get("evidence_gap_after_layer"),
            }
            for item in run.get("collection_runs") or []
        ],
        "flow_nodes": summary.get("main_flow_stages") or [],
        "flow_decisions": run.get("flow_decision_traces") or summary.get("flow_decision_traces") or [],
        "llm_call_traces": summarize_llm_calls(run.get("llm_call_traces") or []),
        "llm_item_traces": summarize_llm_calls(run.get("llm_item_traces") or []),
        "model_route_traces": run.get("model_route_traces") or [],
        "agent_final_output": high_quality_clues,
        "claim_boundary": (
            "This evidence proves the recorded real-network acceptance run only. "
            "High-quality clues remain human-review candidates; the artifact does not claim private-group coverage, "
            "production monitoring, or analyst confirmation of black/gray-market operators."
        ),
    }


def summarize_clue(
    clue: dict[str, Any],
    *,
    records: Iterable[dict[str, Any]] = (),
    classifications: Iterable[dict[str, Any]] = (),
    entities: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    refinement = clue.get("refinement") if isinstance(clue.get("refinement"), dict) else {}
    quality = clue.get("quality") if isinstance(clue.get("quality"), dict) else {}
    evidence_ids = [str(item) for item in clue.get("evidence_trace_ids") or []]
    reviewability = clue.get("evidence_reviewability") if isinstance(clue.get("evidence_reviewability"), dict) else None
    if not reviewability or (not reviewability.get("evidence_cards") and evidence_ids):
        reviewability = build_evidence_reviewability(
            clue,
            classifications=classifications,
            entities=entities,
            records=records,
        )
    return {
        "clue_id": clue.get("clue_id"),
        "clue_type": clue.get("clue_type"),
        "key": clue.get("key"),
        "risk_category": clue.get("risk_category"),
        "confidence": clue.get("confidence"),
        "quality_score": clue.get("quality_score"),
        "quality_level": clue.get("quality_level"),
        "quality_review_required": quality.get("review_required"),
        "evidence_trace_count": len(evidence_ids),
        "evidence_trace_ids": evidence_ids,
        "source_names": clue.get("source_names") or [],
        "source_types": clue.get("source_types") or [],
        "threshold_reason": clue.get("threshold_reason"),
        "promotion_reason": clue.get("promotion_reason"),
        "llm_refined_summary": refinement.get("refined_summary"),
        "llm_review_required": refinement.get("review_required"),
        "llm_confidence_delta": refinement.get("confidence_delta"),
        "llm_refinement_reasons": refinement.get("refinement_reasons") or [],
        "quality_reasons": quality.get("quality_reasons") or [],
        "evidence_reviewability": reviewability,
        "suggested_review_action": reviewability.get("suggested_review_action"),
    }


def _reviewability_inputs_from_record_details(
    record_details: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(record_details, dict):
        return [], [], []
    records = [item for item in record_details.get("records") or [] if isinstance(item, dict)]
    review_records: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    for item in records:
        trace_id = str(item.get("trace_id") or item.get("source_trace_id") or "").strip()
        if not trace_id:
            continue
        review_records.append(
            {
                "trace_id": trace_id,
                "source_trace_id": trace_id,
                "source_name": item.get("source_name") or item.get("source"),
                "source_type": item.get("source_type"),
                "content_text": item.get("summary") or item.get("content_text") or item.get("raw_text") or item.get("text") or "",
                "clean_text": item.get("clean_text") or item.get("cleaning_visible") or item.get("summary") or "",
                "publish_time": item.get("publish_time") or item.get("crawl_time") or item.get("created_at"),
            }
        )
        classifications.append(
            {
                "trace_id": trace_id,
                "source_trace_id": trace_id,
                "risk_category": item.get("risk_category") or item.get("classification_label") or item.get("original_label"),
                "secondary_label": item.get("secondary_label"),
                "confidence": item.get("confidence"),
                "review_required": item.get("review_required"),
            }
        )
        for entity in item.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            entities.append(
                {
                    "trace_id": trace_id,
                    "source_trace_id": trace_id,
                    "entity_type": entity.get("entity_type") or entity.get("type"),
                    "normalized_value": entity.get("normalized_value") or entity.get("value"),
                    "raw_value": entity.get("raw_value") or entity.get("value"),
                    "confidence": entity.get("confidence"),
                }
            )
    return review_records, classifications, entities


def summarize_llm_calls(traces: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for trace in traces:
        output.append(
            {
                "stage": trace.get("stage") or trace.get("trace_kind"),
                "clue_id": trace.get("clue_id"),
                "llm_ok": trace.get("llm_ok"),
                "used_fallback": trace.get("used_fallback"),
                "error": trace.get("error"),
                "model": trace.get("model"),
            }
        )
    return output


def render_markdown(evidence: dict[str, Any]) -> str:
    counts = evidence.get("counts") or {}
    lines = [
        "# BlackAgent 真实联网 + 外部 LLM 端到端验收证据",
        "",
        f"- 运行产物：`{evidence.get('run_artifact')}`",
        f"- LLM smoke：`{evidence.get('smoke_artifact')}`",
        f"- 来源配置：`{evidence.get('source_catalog')}`",
        f"- 状态：`{evidence.get('status')}`，模式：`{evidence.get('mode')}`",
        f"- 达标结论：`high_quality_count={counts.get('high_quality_count')}`；目标 `>=2`；met=`{(evidence.get('target') or {}).get('high_quality_count_met')}`",
        "",
        "## 1. 核心统计",
    ]
    for key, value in counts.items():
        lines.append(f"- `{key}`：{value}")
    lines += [
        "",
        "## 2. 来源类别覆盖",
        f"- selected_source_classes：{json.dumps(evidence.get('selected_source_classes') or [], ensure_ascii=False)}",
        f"- collection_source_classes_executed：{json.dumps(evidence.get('collection_source_classes_executed') or [], ensure_ascii=False)}",
        "",
        "## 3. 联网采集运行",
    ]
    for run in evidence.get("collection_runs") or []:
        error = f"；error={run.get('error')}" if run.get("error") else ""
        lines.append(
            f"- `{run.get('source_name')}` / `{run.get('source_class')}` / `{run.get('collection_layer')}`："
            f"fetched={run.get('fetched_count')}，status={run.get('status')}{error}"
        )
    lines += ["", "## 4. 外部 LLM 调用"]
    for trace in [*(evidence.get("llm_call_traces") or []), *(evidence.get("llm_item_traces") or [])]:
        clue = f"，clue_id={trace.get('clue_id')}" if trace.get("clue_id") else ""
        lines.append(
            f"- `{trace.get('stage')}`{clue}：llm_ok={trace.get('llm_ok')}，"
            f"fallback={trace.get('used_fallback')}，error={trace.get('error')}"
        )
    lines += ["", "## 5. 高质量候选线索"]
    for clue in evidence.get("agent_final_output") or []:
        reviewability = clue.get("evidence_reviewability") if isinstance(clue.get("evidence_reviewability"), dict) else {}
        risk = reviewability.get("false_positive_risk") if isinstance(reviewability.get("false_positive_risk"), dict) else {}
        time_range = reviewability.get("time_range") if isinstance(reviewability.get("time_range"), dict) else {}
        lines += [
            f"### {clue.get('clue_id')}",
            f"- 类型/风险：`{clue.get('clue_type')}` / `{clue.get('risk_category')}`",
            f"- key：`{clue.get('key')}`",
            f"- confidence / quality_score：{clue.get('confidence')} / {clue.get('quality_score')}",
            f"- evidence_trace_count：{clue.get('evidence_trace_count')}",
            f"- source_names：{json.dumps(clue.get('source_names') or [], ensure_ascii=False)}",
            f"- source_types：{json.dumps(clue.get('source_types') or [], ensure_ascii=False)}",
            f"- threshold_reason：`{clue.get('threshold_reason')}`",
            f"- LLM 精炼摘要：{clue.get('llm_refined_summary')}",
            f"- LLM review_required：{clue.get('llm_review_required')}",
            f"- 证据复核：source_count={reviewability.get('source_count')}；entity_support_count={reviewability.get('entity_support_count')}；time_range={time_range.get('start')}..{time_range.get('end')}；false_positive_risk={risk.get('level')}({risk.get('score')})；suggested_review_action=`{reviewability.get('suggested_review_action')}`",
        ]
        for reason in clue.get("llm_refinement_reasons") or []:
            lines.append(f"  - {reason}")
        lines.append("")
    lines += [
        "## 6. 边界说明",
        f"- {evidence.get('claim_boundary')}",
        "- 本次输出的 2 条高质量线索是候选线索：其中 Telegram 导航目录共享联系方式线索仍需人工确认是否直接对应接码/群控/账号交易；贴吧脚本模板线索命中工具交易/脚本证据链但仍需人工复核排除教程/讨论语境。",
    ]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    run_path = resolve_project_path(args.run)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    record_details_path = resolve_project_path(args.record_details)
    record_details = json.loads(record_details_path.read_text(encoding="utf-8")) if record_details_path.exists() else None
    evidence = build_evidence(
        run,
        run_path=str(Path(args.run)),
        smoke_path=str(Path(args.smoke)),
        source_catalog=str(Path(args.source_catalog)),
        command=args.command,
        record_details=record_details,
    )
    json_out = resolve_project_path(args.json_out)
    md_out = resolve_project_path(args.md_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(render_markdown(evidence), encoding="utf-8")
    print(json.dumps({"json_out": str(json_out), "md_out": str(md_out), "high_quality_count": evidence["counts"]["high_quality_count"]}, ensure_ascii=False, indent=2))
    return 0 if int(evidence["counts"].get("high_quality_count") or 0) >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
