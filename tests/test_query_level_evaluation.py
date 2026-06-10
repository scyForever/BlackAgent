from argparse import Namespace

import json
import sys

from scripts import evaluate_query_level
from scripts.evaluate_query_level import evaluate_query_benchmark, load_jsonl, quality_gate_failures


def test_query_level_benchmark_scores_top_k_evidence_latency_and_reviewability():
    rows = [
        {
            "query": "取当天诈骗引流线索信息",
            "top_k": 3,
            "latency_ms": 84.0,
            "expected_clues": [
                {
                    "clue_type": "shared_contact_48h",
                    "key": "TG:core01",
                    "risk_category": "诈骗引流",
                    "expected_evidence_trace_ids": ["q1", "q2"],
                    "expected_entity_values": ["TG:core01"],
                }
            ],
            "returned_clues": [
                {
                    "clue_id": "clue-hit",
                    "clue_type": "shared_contact_48h",
                    "key": "TG:core01",
                    "risk_category": "诈骗引流",
                    "evidence_trace_ids": ["q1", "q2", "q3"],
                    "entity_values": ["TG:core01"],
                    "evidence_reviewability": {
                        "source_count": 2,
                        "original_snippets": ["TG:core01 私域导流", "论坛复现 TG:core01"],
                        "time_range": {
                            "start": "2026-06-09T08:00:00+08:00",
                            "end": "2026-06-09T09:00:00+08:00",
                        },
                        "evidence_cards": [
                            {
                                "trace_id": "q1",
                                "raw_snippet": "原始样本 TG:core01 私域导流",
                                "clean_text": "TG:core01 私域导流",
                                "classification": {"risk_category": "诈骗引流", "secondary_label": "私域导流"},
                                "entities": [{"entity_type": "contact", "normalized_value": "TG:core01"}],
                            }
                        ],
                    },
                }
            ],
        }
    ]

    report = evaluate_query_benchmark(rows)

    assert report["status"] == "completed"
    assert report["query_count"] == 1
    assert report["top_k_accuracy"] == 1.0
    assert report["top_k_hits"] == 1
    assert report["evidence_completeness_rate"] == 1.0
    assert report["human_reviewability_rate"] == 1.0
    assert report["latency"]["p95_latency_ms"] == 84.0
    assert report["per_query"][0]["matched_expected_clue_count"] == 1
    assert report["per_query"][0]["top_k_accuracy"] == 1.0
    assert report["evaluation_mode"] == "replayed_query_output_fixture"
    assert report["claim_boundary"]


def test_query_level_match_requires_declared_entity_and_evidence_fields():
    rows = [
        {
            "query": "找近24小时接码和群控脚本跨源线索",
            "top_k": 3,
            "expected_clues": [
                {
                    "clue_type": "entity_graph_tool_trade_cluster",
                    "key": "TG:tool01",
                    "risk_category": "工具交易",
                    "expected_evidence_trace_ids": ["query-tool-a", "query-tool-b"],
                    "expected_entity_values": ["TG:tool01"],
                }
            ],
            "returned_clues": [
                {
                    "clue_id": "weak-partial",
                    "clue_type": "entity_graph_tool_trade_cluster",
                    "key": "TG:tool01",
                    "risk_category": "工具交易",
                    "evidence_trace_ids": [],
                    "entity_values": [],
                    "evidence_reviewability": {
                        "source_count": 1,
                        "original_snippets": ["partial text"],
                        "time_range": {"start": "2026-06-09T08:00:00+08:00"},
                        "evidence_cards": [
                            {
                                "trace_id": "partial",
                                "raw_snippet": "partial text",
                                "classification": {"risk_category": "工具交易"},
                                "entities": [],
                            }
                        ],
                    },
                }
            ],
        }
    ]

    report = evaluate_query_benchmark(rows)

    assert report["top_k_accuracy"] == 0.0
    assert report["matched_expected_clue_count"] == 0


def test_query_level_quality_gate_failures_are_explicit():
    report = {
        "top_k_accuracy": 0.4,
        "evidence_completeness_rate": 0.5,
        "human_reviewability_rate": 0.6,
        "latency": {"p95_latency_ms": 1500.0},
    }
    args = Namespace(
        min_top_k_accuracy=0.8,
        min_evidence_completeness_rate=0.9,
        min_human_reviewability_rate=0.95,
        max_p95_latency_ms=1000.0,
    )

    failures = quality_gate_failures(report, args)

    assert failures == [
        "top_k_accuracy_below_threshold:0.4<0.8",
        "evidence_completeness_rate_below_threshold:0.5<0.9",
        "human_reviewability_rate_below_threshold:0.6<0.95",
        "p95_latency_ms_above_threshold:1500.0>1000.0",
    ]


def test_default_query_level_fixture_supports_cli_report(tmp_path, monkeypatch):
    output = tmp_path / "query-level-report.json"

    rows = load_jsonl("tests/evaluation/query_level_benchmark.jsonl")
    report = evaluate_query_benchmark(rows)

    assert 2 <= len(rows) <= 20
    assert report["top_k_accuracy"] >= 0.8
    assert report["evidence_completeness_rate"] >= 0.8
    assert report["human_reviewability_rate"] >= 0.8

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_query_level.py",
            "--benchmark",
            "tests/evaluation/query_level_benchmark.jsonl",
            "--min-top-k-accuracy",
            "0.8",
            "--min-evidence-completeness-rate",
            "0.8",
            "--min-human-reviewability-rate",
            "0.8",
            "--max-p95-latency-ms",
            "1000",
            "--output",
            str(output),
        ],
    )

    assert evaluate_query_level.main() == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["quality_gate_failures"] == []
