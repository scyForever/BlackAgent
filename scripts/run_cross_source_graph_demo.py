"""Generate a defense-demo report for cross-source entity graph clues."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.enhancement.engine import PhaseTwoThreeEngine


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local cross-source entity graph demo.")
    parser.add_argument("--output", default="data/cross_source_graph_demo_report.json", help="Where to write JSON report.")
    return parser.parse_args(argv)


def demo_records() -> list[dict[str, Any]]:
    return [
        {
            "trace_id": "graph-demo-im-1",
            "source_name": "tg-authorized-demo",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": "2026-06-03T01:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:graph001，落地 https://graph-demo.example/path。",
        },
        {
            "trace_id": "graph-demo-forum-2",
            "source_name": "forum-public-demo",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": "2026-06-03T02:00:00+00:00",
            "content_text": "论坛帖子复现同一联系方式 TG:graph001，提到群发脚本和 graph-demo.example 域名。",
        },
        {
            "trace_id": "graph-demo-feed-3",
            "source_name": "feed-authorized-demo",
            "source_type": "THREAT_INTEL",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": "2026-06-03T03:00:00+00:00",
            "content_text": "授权情报源补充：TG:graph001 与 https://graph-demo.example/path 关联接码平台推广。",
        },
    ]


def run_demo() -> dict[str, Any]:
    engine = PhaseTwoThreeEngine()
    result = engine.run(
        demo_records(),
        prompt_text="Return strict JSON with confidence, evidence, requires_human_review and no production action.",
    ).model_dump()
    clues = list(result.get("risk_clues") or [])
    cross_source_clues = [
        clue
        for clue in clues
        if len({str(item) for item in (clue.get("source_names") or []) if str(item).strip()}) >= 2
        or str(clue.get("clue_type") or "").startswith(("shared_", "entity_graph_", "graph_"))
    ]
    related_entities = [
        entity
        for entity in result.get("entities", [])
        if str(entity.get("normalized_value") or "").lower() in {"graph001", "graph-demo.example/path", "graph-demo.example"}
        or "graph001" in str(entity.get("entity_value") or "").lower()
        or "graph-demo.example" in str(entity.get("entity_value") or "").lower()
    ]
    return {
        "status": "completed" if cross_source_clues else "no_cross_source_clue",
        "run_type": "local_cross_source_entity_graph_demo",
        "input_count": result.get("accepted_count"),
        "source_count": len({record["source_name"] for record in demo_records()}),
        "graph_summary": result.get("graph_summary"),
        "cross_source_clue_count": len(cross_source_clues),
        "cross_source_clues": cross_source_clues,
        "related_entities": related_entities,
        "claim_boundary": (
            "Demonstrates the evidence pattern 'same TG/contact/domain appears in multiple authorized/public sources'. "
            "It is a local reproducible demo, not a claim of ongoing external-source coverage."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_demo()
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
