import re
import shutil
import subprocess

import pytest

from scripts.serve_demo_api import _INDEX_HTML, build_review_workbench, export_review_report, record_review_decision


def test_review_workbench_exposes_clues_evidence_entities_decisions_and_export():
    demo_report = {
        "status": "completed",
        "query": "取当天诈骗引流线索信息",
        "top_clues": [
            {
                "clue_id": "clue-1",
                "clue_type": "shared_contact_48h",
                "risk_category": "诈骗引流",
                "secondary_label": "私域导流",
                "confidence": 0.88,
                "quality_score": 0.82,
                "evidence_trace_ids": ["r1", "r2"],
                "entity_values": ["TG:core01"],
                "evidence_reviewability": {
                    "suggested_review_action": "human_verify_cross_source_trace",
                    "evidence_cards": [
                        {
                            "trace_id": "r1",
                            "source_name": "forum",
                            "source_type": "Forum",
                            "raw_snippet": "原始样本：私域导流 TG:core01",
                            "clean_text": "私域导流 TG:core01",
                            "classification": {
                                "risk_category": "诈骗引流",
                                "secondary_label": "私域导流",
                                "confidence": 0.88,
                                "review_required": False,
                            },
                            "entities": [
                                {
                                    "entity_type": "contact",
                                    "raw_value": "TG:core01",
                                    "normalized_value": "TG:core01",
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    }

    workbench = build_review_workbench(demo_report)
    decision = record_review_decision(workbench, "clue-1", decision="confirmed", reviewer="analyst-a")
    exported = export_review_report(workbench, decisions=[decision])

    clue = workbench["clues"][0]
    assert workbench["query"] == "取当天诈骗引流线索信息"
    assert workbench["available_actions"] == ["confirm", "reject"]
    assert clue["clue_id"] == "clue-1"
    assert clue["classification_result"]["risk_category"] == "诈骗引流"
    assert clue["evidence_snippets"][0]["raw_snippet"] == "原始样本：私域导流 TG:core01"
    assert clue["entity_highlights"][0]["normalized_value"] == "TG:core01"
    assert decision["status"] == "confirmed"
    assert exported["review_summary"]["confirmed"] == 1
    assert exported["clues"][0]["review_decision"]["reviewer"] == "analyst-a"


def test_review_workbench_inline_javascript_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to parse the inline workbench JavaScript")
    match = re.search(r"<script>(.*?)</script>", _INDEX_HTML, flags=re.DOTALL)
    assert match is not None
    script_path = tmp_path / "workbench.js"
    script_path.write_text(match.group(1), encoding="utf-8")

    result = subprocess.run([node, "--check", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
