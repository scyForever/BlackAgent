from __future__ import annotations

import json
import subprocess

from scripts import run_acceptance_gate


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_text(path, text="{}\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_completed_acceptance_artifacts(root):
    data = root / "data"
    _write_json(
        data / "manual_heldout_eval_current.json",
        {
            "status": "completed",
            "dataset": {"kind": "manual_heldout_public_authorized"},
            "profile": "fast",
            "record_count": 42,
            "primary_classification_f1": 0.91,
            "secondary_classification_f1": 0.82,
            "hierarchical_classification_f1": 0.87,
            "entity_f1": 0.79,
            "false_positive_rate": 0.04,
            "classification_review_rate": 0.18,
            "classification": {
                "review_load": {
                    "review_required_count": 7,
                    "review_required_rate": 0.18,
                }
            },
        },
    )
    _write_json(
        data / "eval_manual_heldout_clue_recall_report.json",
        {
            "status": "completed",
            "dataset": {"kind": "manual_heldout_clue_gold"},
            "profile": "high_recall",
            "clue_precision": 0.93,
            "clue_recall": 0.97,
            "clue_f1": 0.95,
            "clue": {
                "object_clue_eval": {
                    "overall": {
                        "precision": 0.92,
                        "recall": 0.96,
                        "f1": 0.94,
                    },
                    "evidence_chain_precision": 0.89,
                    "evidence_chain_recall": 0.91,
                    "evidence_reviewability_rate": 0.98,
                },
                "duplicate_clue_rate": 0.02,
            },
        },
    )
    _write_json(
        data / "external_balanced_source_evidence_pack_report.json",
        {
            "status": "completed",
            "selected_count": 80,
            "per_group_target": 20,
            "target_groups": ["im_or_group", "social_or_forum", "vertical_or_technical"],
            "available_group_counts": {"im_or_group": 120, "social_or_forum": 90},
            "eligible_group_counts": {"im_or_group": 20, "social_or_forum": 20},
            "selected_group_counts": {"im_or_group": 20, "social_or_forum": 20},
            "source_counts": [{"source_name": "forum", "count": 12}],
            "missing_required_fields": [],
            "completeness_counts": {"complete": 80},
            "review_status_counts": {"reviewable": 80},
            "source_evidence_counts_by_category": {"诈骗引流": 33},
            "skipped_counts": {"unsupported_group": 2},
            "warnings": ["sample warning"],
            "claim_boundary": "balanced external pack only",
        },
    )
    _write_text(data / "external_balanced_source_evidence_pack.jsonl", '{"id":"external"}\n')
    _write_json(
        data / "collection_phase_multi_source_evidence_pack_report.json",
        {
            "status": "completed",
            "record_count": 300,
            "target_groups": ["im_or_group", "social_or_forum"],
            "missing_required_fields": [],
            "completeness_counts": {"complete": 299, "partial": 1},
            "review_status_counts": {"reviewable": 300},
            "source_evidence_counts": {"has_source_evidence": 300},
            "source_evidence_counts_by_category": {"账号交易": 44},
            "claim_boundary": "joined pack only",
        },
    )
    _write_text(data / "collection_phase_multi_source_evidence_pack.jsonl", '{"id":"joined"}\n')
    _write_json(data / "source_smoke_report.json", {"status": "completed"})
    _write_json(data / "source_live_smoke_report.json", {"status": "completed"})
    _write_json(data / "eval_report.json", {"status": "completed", "primary_classification_f1": 0.11})


def test_summary_aggregates_current_artifacts_and_marks_stale_eval_report(tmp_path):
    _write_completed_acceptance_artifacts(tmp_path)

    summary = run_acceptance_gate.build_summary(
        root=tmp_path,
        command_results=[],
    )

    assert summary["status"] == "completed"
    source_paths = {source["path"] for source in summary["artifact_sources"].values()}
    assert "data/manual_heldout_eval_current.json" in source_paths
    assert "data/eval_manual_heldout_clue_recall_report.json" in source_paths
    assert "data/external_balanced_source_evidence_pack_report.json" in source_paths
    assert "data/external_balanced_source_evidence_pack.jsonl" in source_paths
    assert "data/collection_phase_multi_source_evidence_pack_report.json" in source_paths
    assert "data/collection_phase_multi_source_evidence_pack.jsonl" in source_paths
    assert "data/source_smoke_report.json" in source_paths
    assert "data/source_live_smoke_report.json" in source_paths
    assert "data/eval_report.json" not in source_paths
    assert summary["stale_artifacts"][0]["path"] == "data/eval_report.json"
    assert summary["stale_artifacts"][0]["status"] == "stale_not_authoritative"

    assert summary["classification"]["dataset"]["kind"] == "manual_heldout_public_authorized"
    assert summary["classification"]["profile"] == "fast"
    assert summary["classification"]["record_count"] == 42
    assert summary["classification"]["primary_classification_f1"] == 0.91
    assert summary["classification"]["secondary_classification_f1"] == 0.82
    assert summary["classification"]["hierarchical_classification_f1"] == 0.87
    assert summary["classification"]["entity_f1"] == 0.79
    assert summary["classification"]["false_positive_rate"] == 0.04
    assert summary["classification"]["classification_review_rate"] == 0.18
    assert summary["classification"]["review_load"]["review_required_count"] == 7

    assert summary["clues"]["dataset"]["kind"] == "manual_heldout_clue_gold"
    assert summary["clues"]["profile"] == "high_recall"
    assert summary["clues"]["clue_precision"] == 0.93
    assert summary["clues"]["clue_recall"] == 0.97
    assert summary["clues"]["clue_f1"] == 0.95
    assert summary["clues"]["object_clue_precision"] == 0.92
    assert summary["clues"]["object_clue_recall"] == 0.96
    assert summary["clues"]["object_clue_f1"] == 0.94
    assert summary["clues"]["evidence_chain_precision"] == 0.89
    assert summary["clues"]["evidence_chain_recall"] == 0.91
    assert summary["clues"]["evidence_reviewability_rate"] == 0.98
    assert summary["clues"]["duplicate_clue_rate"] == 0.02

    assert summary["evidence_pack"]["external_balanced"]["selected_count"] == 80
    assert summary["evidence_pack"]["external_balanced"]["per_group_target"] == 20
    assert summary["evidence_pack"]["external_balanced"]["target_groups"] == [
        "im_or_group",
        "social_or_forum",
        "vertical_or_technical",
    ]
    assert summary["evidence_pack"]["external_balanced"]["available_group_counts"]["im_or_group"] == 120
    assert summary["evidence_pack"]["external_balanced"]["eligible_group_counts"]["social_or_forum"] == 20
    assert summary["evidence_pack"]["external_balanced"]["selected_group_counts"]["im_or_group"] == 20
    assert summary["evidence_pack"]["external_balanced"]["source_counts"][0]["source_name"] == "forum"
    assert summary["evidence_pack"]["external_balanced"]["missing_required_fields"] == []
    assert summary["evidence_pack"]["external_balanced"]["completeness_counts"]["complete"] == 80
    assert summary["evidence_pack"]["external_balanced"]["skipped_counts"]["unsupported_group"] == 2
    assert summary["evidence_pack"]["external_balanced"]["warnings"] == ["sample warning"]
    assert summary["evidence_pack"]["joined_multi_source"]["record_count"] == 300
    assert summary["evidence_pack"]["joined_multi_source"]["source_evidence_counts"]["has_source_evidence"] == 300
    assert summary["evidence_pack"]["joined_multi_source"]["source_evidence_counts_by_category"]["账号交易"] == 44
    assert summary["evidence_pack"]["joined_multi_source"]["claim_boundary"] == "joined pack only"

    assert "offline_stable_demo" in summary["demo_paths"]
    assert "authorized_network_demo" in summary["demo_paths"]
    assert "--demo-sample --show summary --dry-run" in summary["demo_paths"]["offline_stable_demo"]["command"]
    assert "--enable-network" in summary["demo_paths"]["authorized_network_demo"]["command"]
    assert "credentials" in summary["demo_paths"]["credential_boundary"]
    assert "only final acceptance scope" in summary["claim_boundary"]


def test_failed_command_result_fails_summary_status(tmp_path):
    _write_completed_acceptance_artifacts(tmp_path)

    def fake_runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            3,
            stdout="command stdout",
            stderr="command stderr",
        )

    command_result = run_acceptance_gate.run_command(
        ["python", "-c", "raise SystemExit(3)"],
        runner=fake_runner,
    )
    summary = run_acceptance_gate.build_summary(
        root=tmp_path,
        command_results=[command_result],
    )

    assert command_result["status"] == "failed"
    assert command_result["returncode"] == 3
    assert command_result["stdout_excerpt"] == "command stdout"
    assert command_result["stderr_excerpt"] == "command stderr"
    assert summary["status"] == "failed"


def test_run_command_captures_output_with_utf8_replacement():
    seen_kwargs = {}

    def fake_runner(command, **kwargs):
        seen_kwargs.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = run_acceptance_gate.run_command(["python", "-c", "print('ok')"], runner=fake_runner)

    assert result["status"] == "passed"
    assert seen_kwargs["encoding"] == "utf-8"
    assert seen_kwargs["errors"] == "replace"


def test_failed_smoke_artifact_fails_summary_status(tmp_path):
    _write_completed_acceptance_artifacts(tmp_path)
    _write_json(tmp_path / "data" / "source_live_smoke_report.json", {"status": "failed", "error": "network"})

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert {
        "type": "report_not_completed",
        "name": "source_live_smoke_report",
        "path": "data/source_live_smoke_report.json",
        "status": "failed",
    } in summary["gate_failures"]


def test_empty_existing_smoke_artifact_fails_summary_status(tmp_path):
    _write_completed_acceptance_artifacts(tmp_path)
    _write_json(tmp_path / "data" / "source_smoke_report.json", {})

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert {
        "type": "report_not_completed",
        "name": "source_smoke_report",
        "path": "data/source_smoke_report.json",
        "status": None,
    } in summary["gate_failures"]


def test_malformed_required_artifact_writes_failure_summary(tmp_path):
    _write_completed_acceptance_artifacts(tmp_path)
    (tmp_path / "data" / "manual_heldout_eval_current.json").write_text("{not json", encoding="utf-8")

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert any(
        failure["type"] == "artifact_parse_error"
        and failure["path"] == "data/manual_heldout_eval_current.json"
        for failure in summary["gate_failures"]
    )


def test_main_skip_commands_writes_summary_and_exits_zero(tmp_path, monkeypatch):
    _write_completed_acceptance_artifacts(tmp_path)
    output = tmp_path / "data" / "final_acceptance_summary.json"
    monkeypatch.chdir(tmp_path)

    exit_code = run_acceptance_gate.main(["--skip-commands", "--output", str(output)])

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert saved["status"] == "completed"
    assert {result["status"] for result in saved["commands"]} == {"skipped"}
