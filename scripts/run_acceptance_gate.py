from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = "data/final_acceptance_summary.json"
MANUAL_HELDOUT_REPORT = "data/manual_heldout_eval_current.json"
CLUE_RECALL_REPORT = "data/eval_manual_heldout_clue_recall_report.json"
EXTERNAL_BALANCED_REPORT = "data/external_balanced_source_evidence_pack_report.json"
EXTERNAL_BALANCED_JSONL = "data/external_balanced_source_evidence_pack.jsonl"
JOINED_EVIDENCE_REPORT = "data/collection_phase_multi_source_evidence_pack_report.json"
JOINED_EVIDENCE_JSONL = "data/collection_phase_multi_source_evidence_pack.jsonl"
CLUE_EVIDENCE_INDEX = "data/collection_phase_multi_source_clue_evidence_index.json"
JOINED_CLUES_JSONL = "data/collection_phase_multi_source_clues.jsonl"
JOINED_GRAPH_RELATIONS_JSONL = "data/collection_phase_multi_source_graph_relations.jsonl"
SCALE_BENCHMARK_REPORT = "data/scale_benchmark_report.json"
OCR_HARDSET_REPORT = "data/ocr_hardset_report.json"
SOURCE_SMOKE_REPORT = "data/source_smoke_report.json"
SOURCE_LIVE_SMOKE_REPORT = "data/source_live_smoke_report.json"
AUTHORIZED_SOURCE_RERUN_REPORT = "data/authorized_source_rerun_pack_report.json"
AUTHORIZED_SOURCE_RERUN_JSONL = "data/authorized_source_rerun_pack.jsonl"
STALE_EVAL_REPORT = "data/eval_report.json"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


configure_stdio()


COMMANDS = [
    (
        "unit_tests",
        [sys.executable, "-m", "pytest", "-q"],
        "skip_unit_tests",
    ),
    (
        "demo_cli",
        [
            sys.executable,
            "scripts/run_agent_cli.py",
            "--demo-sample",
            "--show",
            "summary",
            "--dry-run",
        ],
        "skip_demo_cli",
    ),
    (
        "manual_heldout_eval",
        [
            sys.executable,
            "scripts/evaluate_pipeline.py",
            "--gold",
            "tests/evaluation/manual_heldout_classification.jsonl",
            "--entities-gold",
            "tests/evaluation/manual_heldout_classification.jsonl",
            "--classification-granularity",
            "auto",
            "--dataset-kind",
            "manual_heldout_public_authorized",
            "--profile",
            "fast",
            "--max-hard-negative-fpr",
            "0.1",
            "--max-classification-review-rate",
            "0.25",
            "--output",
            MANUAL_HELDOUT_REPORT,
        ],
        "skip_manual_heldout_eval",
    ),
    (
        "clue_recall_eval",
        [
            sys.executable,
            "scripts/evaluate_pipeline.py",
            "--gold",
            "tests/evaluation/manual_heldout_classification.jsonl",
            "--entities-gold",
            "tests/evaluation/manual_heldout_classification.jsonl",
            "--clues-gold",
            "tests/evaluation/manual_heldout_clues.jsonl",
            "--classification-granularity",
            "auto",
            "--dataset-kind",
            "manual_heldout_clue_gold",
            "--profile",
            "high_recall",
            "--min-clue-recall",
            "0.95",
            "--min-object-clue-recall",
            "0.95",
            "--max-clue-overgeneration-ratio",
            "1.05",
            "--output",
            CLUE_RECALL_REPORT,
        ],
        "skip_clue_recall_eval",
    ),
    (
        "evidence_pack",
        [
            sys.executable,
            "scripts/build_acceptance_evidence_pack.py",
            "--acceptance-pack",
            "data/collection_phase_multi_source_acceptance_pack.jsonl",
            "--cleaned",
            "data/acceptance_direct_final3_cleaned_corpus.jsonl",
            "--classifications",
            "data/acceptance_direct_final3_raw_classifications.jsonl",
            "--entities",
            "data/acceptance_direct_final3_raw_entities.jsonl",
            "--clues",
            JOINED_CLUES_JSONL,
            "--graph-relations",
            JOINED_GRAPH_RELATIONS_JSONL,
            "--hydrated",
            "data/acceptance_direct_final3_hydrated_pages.jsonl",
            "--output",
            JOINED_EVIDENCE_JSONL,
            "--report-out",
            JOINED_EVIDENCE_REPORT,
            "--clue-index-output",
            CLUE_EVIDENCE_INDEX,
        ],
        "skip_evidence_pack",
    ),
    (
        "network_smoke",
        [
            sys.executable,
            "scripts/run_live_source_smoke.py",
            "--output",
            SOURCE_LIVE_SMOKE_REPORT,
        ],
        "skip_network_smoke",
    ),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run and aggregate the final BlackAgent acceptance gate.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-commands", action="store_true")
    parser.add_argument("--skip-unit-tests", action="store_true")
    parser.add_argument("--skip-demo-cli", action="store_true")
    parser.add_argument("--skip-manual-heldout-eval", action="store_true")
    parser.add_argument("--skip-clue-recall-eval", action="store_true")
    parser.add_argument("--skip-evidence-pack", action="store_true")
    parser.add_argument("--skip-network-smoke", action="store_true")
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "status": "artifact_load_failed",
            "_artifact_error_type": type(exc).__name__,
            "_artifact_error": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "status": "artifact_load_failed",
            "_artifact_error_type": "InvalidJsonRoot",
            "_artifact_error": "expected JSON object at document root",
        }
    return payload


def artifact_metadata(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    metadata: dict[str, Any] = {
        "path": relative_path,
        "exists": path.exists(),
    }
    if not path.exists():
        return metadata
    stat = path.stat()
    metadata.update(
        {
            "size_bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "sha256": sha256_file(path),
        }
    )
    return metadata


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    command: list[str],
    *,
    name: str | None = None,
    runner=subprocess.run,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = runner(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        returncode = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except Exception as exc:  # pragma: no cover - defensive for operator runs
        returncode = 1
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "name": name,
        "command": " ".join(command),
        "returncode": returncode,
        "status": "passed" if returncode == 0 else "failed",
        "elapsed_ms": elapsed_ms,
        "stdout_excerpt": excerpt(stdout),
        "stderr_excerpt": excerpt(stderr),
    }


def run_gate_commands(args: argparse.Namespace, *, runner=subprocess.run, cwd: str | Path | None = None) -> list[dict[str, Any]]:
    results = []
    for name, command, skip_attr in COMMANDS:
        if args.skip_commands or getattr(args, skip_attr):
            results.append(
                {
                    "name": name,
                    "command": " ".join(command),
                    "status": "skipped",
                    "returncode": None,
                    "elapsed_ms": 0,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "",
                }
            )
            continue
        results.append(run_command(command, name=name, runner=runner, cwd=cwd))
    return results


def excerpt(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def build_summary(
    *,
    root: str | Path = ".",
    command_results: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root_path = Path(root)
    commands = command_results or []

    manual = load_json(root_path / MANUAL_HELDOUT_REPORT)
    clues = load_json(root_path / CLUE_RECALL_REPORT)
    external_report = load_json(root_path / EXTERNAL_BALANCED_REPORT)
    joined_report = load_json(root_path / JOINED_EVIDENCE_REPORT)
    clue_evidence_index_exists = (root_path / CLUE_EVIDENCE_INDEX).exists()
    scale_benchmark_exists = (root_path / SCALE_BENCHMARK_REPORT).exists()
    ocr_hardset_exists = (root_path / OCR_HARDSET_REPORT).exists()
    source_smoke_exists = (root_path / SOURCE_SMOKE_REPORT).exists()
    source_live_smoke_exists = (root_path / SOURCE_LIVE_SMOKE_REPORT).exists()
    authorized_source_rerun_report_exists = (root_path / AUTHORIZED_SOURCE_RERUN_REPORT).exists()
    authorized_source_rerun_jsonl_exists = (root_path / AUTHORIZED_SOURCE_RERUN_JSONL).exists()
    source_smoke_report = load_json(root_path / SOURCE_SMOKE_REPORT) if source_smoke_exists else {}
    source_live_smoke_report = (
        load_json(root_path / SOURCE_LIVE_SMOKE_REPORT)
        if source_live_smoke_exists
        else {}
    )
    authorized_source_rerun_report = (
        load_json(root_path / AUTHORIZED_SOURCE_RERUN_REPORT)
        if authorized_source_rerun_report_exists
        else {}
    )
    clue_evidence_index = load_json(root_path / CLUE_EVIDENCE_INDEX) if clue_evidence_index_exists else {}
    scale_benchmark_report = load_json(root_path / SCALE_BENCHMARK_REPORT) if scale_benchmark_exists else {}
    ocr_hardset_report = load_json(root_path / OCR_HARDSET_REPORT) if ocr_hardset_exists else {}

    artifact_sources = {
        "manual_heldout": artifact_metadata(root_path, MANUAL_HELDOUT_REPORT),
        "clue_recall": artifact_metadata(root_path, CLUE_RECALL_REPORT),
        "external_balanced_pack_report": artifact_metadata(root_path, EXTERNAL_BALANCED_REPORT),
        "external_balanced_pack_jsonl": artifact_metadata(root_path, EXTERNAL_BALANCED_JSONL),
        "joined_evidence_pack_report": artifact_metadata(root_path, JOINED_EVIDENCE_REPORT),
        "joined_evidence_pack_jsonl": artifact_metadata(root_path, JOINED_EVIDENCE_JSONL),
    }
    if clue_evidence_index_exists:
        artifact_sources["clue_evidence_index"] = artifact_metadata(root_path, CLUE_EVIDENCE_INDEX)
    if scale_benchmark_exists:
        artifact_sources["scale_benchmark_report"] = artifact_metadata(root_path, SCALE_BENCHMARK_REPORT)
    if ocr_hardset_exists:
        artifact_sources["ocr_hardset_report"] = artifact_metadata(root_path, OCR_HARDSET_REPORT)
    if source_smoke_exists:
        artifact_sources["source_smoke_report"] = artifact_metadata(root_path, SOURCE_SMOKE_REPORT)
    if source_live_smoke_exists:
        artifact_sources["source_live_smoke_report"] = artifact_metadata(root_path, SOURCE_LIVE_SMOKE_REPORT)
    authorized_source_rerun_pair_seen = authorized_source_rerun_report_exists or authorized_source_rerun_jsonl_exists
    authorized_source_rerun_pair_complete = authorized_source_rerun_report_exists and authorized_source_rerun_jsonl_exists
    if authorized_source_rerun_pair_seen:
        artifact_sources["authorized_source_rerun_pack_report"] = artifact_metadata(
            root_path,
            AUTHORIZED_SOURCE_RERUN_REPORT,
        )
        artifact_sources["authorized_source_rerun_pack_jsonl"] = artifact_metadata(
            root_path,
            AUTHORIZED_SOURCE_RERUN_JSONL,
        )

    stale_artifacts = []
    if (root_path / STALE_EVAL_REPORT).exists():
        stale_artifacts.append(
            {
                **artifact_metadata(root_path, STALE_EVAL_REPORT),
                "status": "stale_not_authoritative",
                "reason": (
                    "Superseded by current manual held-out and clue recall reports; "
                    "must not be used as the final acceptance scope."
                ),
            }
        )

    summary = {
        "status": "completed",
        "run_type": "final_acceptance_gate",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "artifact_sources": artifact_sources,
        "classification": summarize_classification(manual),
        "clues": summarize_clues(clues),
        "evidence_pack": {
            "external_balanced": summarize_evidence_pack(external_report),
            "joined_multi_source": summarize_evidence_pack(joined_report),
        },
        "demo_paths": demo_paths(),
        "stale_artifacts": stale_artifacts,
        "commands": commands,
        "claim_boundary": (
            "This summary is the only final acceptance scope for the current delivery artifacts. "
            "It does not claim online production generalization or live X/TG coverage unless "
            "credentialed live artifacts are attached and listed as authoritative sources."
        ),
    }
    if clue_evidence_index_exists:
        summary["clue_evidence_index"] = summarize_clue_evidence_index(clue_evidence_index)
    if scale_benchmark_exists:
        summary["scale_benchmark"] = summarize_scale_benchmark(scale_benchmark_report)
    if ocr_hardset_exists:
        summary["ocr_hardset"] = summarize_ocr_hardset(ocr_hardset_report)
    report_sources = {
        "manual_heldout": (MANUAL_HELDOUT_REPORT, manual),
        "clue_recall": (CLUE_RECALL_REPORT, clues),
        "external_balanced_pack_report": (EXTERNAL_BALANCED_REPORT, external_report),
        "joined_evidence_pack_report": (JOINED_EVIDENCE_REPORT, joined_report),
    }
    if clue_evidence_index_exists:
        report_sources["clue_evidence_index"] = (CLUE_EVIDENCE_INDEX, summary["clue_evidence_index"])
    if scale_benchmark_exists:
        report_sources["scale_benchmark_report"] = (SCALE_BENCHMARK_REPORT, summary["scale_benchmark"])
    if ocr_hardset_exists:
        report_sources["ocr_hardset_report"] = (OCR_HARDSET_REPORT, summary["ocr_hardset"])
    if source_smoke_exists:
        report_sources["source_smoke_report"] = (SOURCE_SMOKE_REPORT, source_smoke_report)
    if source_live_smoke_exists:
        report_sources["source_live_smoke_report"] = (SOURCE_LIVE_SMOKE_REPORT, source_live_smoke_report)
    if authorized_source_rerun_report_exists:
        summary["authorized_source_rerun"] = {
            **summarize_authorized_source_rerun(authorized_source_rerun_report),
            "artifact_pair_complete": authorized_source_rerun_pair_complete,
            "artifact_paths": {
                "report": AUTHORIZED_SOURCE_RERUN_REPORT,
                "jsonl": AUTHORIZED_SOURCE_RERUN_JSONL,
            },
        }
        report_sources["authorized_source_rerun_pack_report"] = (
            AUTHORIZED_SOURCE_RERUN_REPORT,
            authorized_source_rerun_report,
        )
    gate_failures = collect_gate_failures(summary, report_sources)
    if authorized_source_rerun_pair_seen and not authorized_source_rerun_pair_complete:
        gate_failures.append(
            {
                "type": "authorized_source_rerun_artifact_pair_incomplete",
                "name": "authorized_source_rerun_pack",
                "report_path": AUTHORIZED_SOURCE_RERUN_REPORT,
                "jsonl_path": AUTHORIZED_SOURCE_RERUN_JSONL,
                "report_exists": authorized_source_rerun_report_exists,
                "jsonl_exists": authorized_source_rerun_jsonl_exists,
            }
        )
    if (
        authorized_source_rerun_pair_complete
        and authorized_source_rerun_report.get("status") == "completed"
    ):
        jsonl_validation = validate_jsonl_object_rows(
            root_path / AUTHORIZED_SOURCE_RERUN_JSONL,
            report=authorized_source_rerun_report,
        )
        if jsonl_validation.get("status") != "valid":
            gate_failures.append(
                {
                    "type": "authorized_source_rerun_jsonl_invalid",
                    "name": "authorized_source_rerun_pack_jsonl",
                    "path": AUTHORIZED_SOURCE_RERUN_JSONL,
                    "reason": jsonl_validation.get("reason"),
                }
            )
    summary["gate_failures"] = gate_failures
    if gate_failures:
        summary["status"] = "failed"
    return summary


def summarize_classification(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset": report.get("dataset"),
        "profile": report.get("profile"),
        "record_count": report.get("record_count") or nested(report, "dataset", "record_count"),
        "primary_classification_f1": first_present(
            report.get("primary_classification_f1"),
            nested(report, "classification", "primary", "f1"),
        ),
        "secondary_classification_f1": first_present(
            report.get("secondary_classification_f1"),
            nested(report, "classification", "secondary", "f1"),
        ),
        "hierarchical_classification_f1": first_present(
            report.get("hierarchical_classification_f1"),
            nested(report, "classification", "hierarchical", "f1"),
            nested(report, "classification", "overall", "f1"),
        ),
        "entity_f1": first_present(report.get("entity_f1"), nested(report, "entity", "overall", "f1")),
        "false_positive_rate": first_present(
            report.get("false_positive_rate"),
            nested(report, "classification", "false_positive_rate"),
        ),
        "classification_review_rate": report.get("classification_review_rate"),
        "review_load": first_present(
            report.get("classification_review_load"),
            nested(report, "classification", "review_load"),
        ),
    }


def summarize_clues(report: dict[str, Any]) -> dict[str, Any]:
    object_eval = nested(report, "clue", "object_clue_eval") or {}
    object_overall = object_eval.get("overall") or {}
    return {
        "dataset": report.get("dataset"),
        "profile": report.get("profile"),
        "clue_precision": first_present(report.get("clue_precision"), nested(report, "clue", "overall", "precision")),
        "clue_recall": first_present(report.get("clue_recall"), nested(report, "clue", "overall", "recall")),
        "clue_f1": first_present(report.get("clue_f1"), nested(report, "clue", "overall", "f1")),
        "object_clue_precision": object_overall.get("precision"),
        "object_clue_recall": object_overall.get("recall"),
        "object_clue_f1": object_overall.get("f1"),
        "evidence_chain_precision": object_eval.get("evidence_chain_precision"),
        "evidence_chain_recall": object_eval.get("evidence_chain_recall"),
        "evidence_reviewability_rate": object_eval.get("evidence_reviewability_rate"),
        "duplicate_clue_rate": first_present(
            nested(report, "clue", "duplicate_clue_rate"),
            object_eval.get("duplicate_clue_rate"),
        ),
    }


def summarize_evidence_pack(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report.get("status"),
        "selected_count": report.get("selected_count"),
        "record_count": report.get("record_count"),
        "per_group_target": report.get("per_group_target"),
        "target_groups": report.get("target_groups"),
        "available_group_counts": report.get("available_group_counts"),
        "eligible_group_counts": report.get("eligible_group_counts"),
        "selected_group_counts": report.get("selected_group_counts"),
        "source_counts": report.get("source_counts"),
        "missing_required_fields": report.get("missing_required_fields"),
        "completeness_counts": report.get("completeness_counts"),
        "review_status_counts": report.get("review_status_counts"),
        "source_evidence_counts": report.get("source_evidence_counts"),
        "source_evidence_counts_by_category": report.get("source_evidence_counts_by_category"),
        "skipped_counts": report.get("skipped_counts"),
        "warnings": report.get("warnings"),
        "claim_boundary": report.get("claim_boundary"),
    }


def summarize_authorized_source_rerun(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report.get("status"),
        "row_count": report.get("row_count"),
        "real_external_row_count": report.get("real_external_row_count"),
        "loopback_row_count": report.get("loopback_row_count"),
        "credential_boundary": report.get("credential_boundary"),
        "source_coverage": report.get("source_coverage"),
        "snapshot_coverage": report.get("snapshot_coverage"),
        "failure_summary": report.get("failure_summary"),
        "collection_window": report.get("collection_window"),
        "claim_boundary": report.get("claim_boundary"),
    }


def summarize_clue_evidence_index(index: dict[str, Any]) -> dict[str, Any]:
    report = embedded_report(index, "report")
    rows_value = index.get("rows")
    rows = rows_value if isinstance(rows_value, list) else []
    actual_answer_chain_card_count = _clue_index_answer_chain_card_count(rows)
    invalid_reason = ""
    if report.get("_artifact_error"):
        invalid_reason = "invalid_embedded_report"
    elif not isinstance(rows_value, list):
        invalid_reason = "invalid_rows"
    elif any(not isinstance(row, dict) for row in rows):
        invalid_reason = "invalid_row"
    elif any(not isinstance(row.get("answer_chain"), list) for row in rows):
        invalid_reason = "invalid_answer_chain"
    elif any(
        not isinstance(card, dict)
        for row in rows
        for card in row.get("answer_chain", [])
    ):
        invalid_reason = "invalid_answer_chain_card"
    elif _int_or_none(report.get("high_quality_clue_count")) is None:
        invalid_reason = "invalid_high_quality_clue_count"
    elif _int_or_none(report.get("indexed_clue_count")) is None:
        invalid_reason = "invalid_indexed_clue_count"
    elif _int_or_none(report.get("answer_chain_card_count")) is None:
        invalid_reason = "invalid_answer_chain_card_count"
    elif _int_or_none(report.get("missing_evidence_trace_count")) is None:
        invalid_reason = "invalid_missing_evidence_trace_count"
    elif _int_or_none(report.get("indexed_clue_count")) > len(rows):
        invalid_reason = "indexed_clue_count_exceeds_rows"
    elif _int_or_none(report.get("answer_chain_card_count")) != actual_answer_chain_card_count:
        invalid_reason = "answer_chain_card_count_mismatch"
    status = "invalid" if invalid_reason else report.get("status")
    summary = {
        "status": status,
        "row_count": len(rows),
        "high_quality_clue_count": report.get("high_quality_clue_count"),
        "indexed_clue_count": report.get("indexed_clue_count"),
        "answer_chain_card_count": report.get("answer_chain_card_count"),
        "missing_evidence_trace_count": report.get("missing_evidence_trace_count"),
        "claim_boundary": report.get("claim_boundary"),
    }
    if report.get("_artifact_error"):
        summary["_artifact_error_type"] = report.get("_artifact_error_type")
        summary["_artifact_error"] = report.get("_artifact_error")
    elif invalid_reason:
        summary["invalid_reason"] = invalid_reason
    return summary


def _clue_index_answer_chain_card_count(rows: list[Any]) -> int:
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        answer_chain = row.get("answer_chain")
        if not isinstance(answer_chain, list):
            continue
        count += sum(1 for card in answer_chain if isinstance(card, dict))
    return count


def summarize_scale_benchmark(report: dict[str, Any]) -> dict[str, Any]:
    scenarios = report.get("scenarios") if isinstance(report.get("scenarios"), list) else []
    parsed_sample_sizes = [
        _int_or_none(scenario.get("sample_size"))
        for scenario in scenarios
        if isinstance(scenario, dict)
    ]
    sample_sizes = [sample_size for sample_size in parsed_sample_sizes if sample_size is not None]
    has_invalid_sample_size = len(sample_sizes) != len(parsed_sample_sizes)
    cost_values = [
        parsed
        for scenario in scenarios
        if isinstance(scenario, dict)
        for parsed in [_float_or_none(scenario.get("estimated_llm_cost_usd"))]
    ]
    has_invalid_cost = any(parsed is None for parsed in cost_values)
    is_invalid = has_invalid_cost or has_invalid_sample_size
    total_cost = None if has_invalid_cost else round(sum(cost_values), 8)
    return {
        "status": "invalid" if is_invalid else report.get("status"),
        "run_type": report.get("run_type"),
        "profile": report.get("profile"),
        "batch_size": report.get("batch_size"),
        "default_defense_scales": report.get("default_defense_scales"),
        "scenario_count": len(scenarios),
        "sample_sizes": sample_sizes,
        "max_sample_size": max(sample_sizes) if sample_sizes else None,
        "estimated_llm_cost_usd_total": total_cost,
        "claim_boundary": report.get("claim_boundary"),
    }


def summarize_ocr_hardset(report: dict[str, Any]) -> dict[str, Any]:
    invalid_reason = ""
    if report.get("_artifact_error"):
        invalid_reason = "artifact_load_failed"
    elif report.get("status") == "completed" and _int_or_none(report.get("record_count")) is None:
        invalid_reason = "invalid_record_count"
    elif report.get("status") == "completed" and not isinstance(report.get("ocr_quality_metrics"), dict):
        invalid_reason = "invalid_ocr_quality_metrics"
    elif report.get("status") == "completed" and not isinstance(report.get("image_kind_coverage"), dict):
        invalid_reason = "invalid_image_kind_coverage"
    elif report.get("status") == "completed" and not isinstance(report.get("real_scene_assessment"), dict):
        invalid_reason = "invalid_real_scene_assessment"
    status = "invalid" if invalid_reason and invalid_reason != "artifact_load_failed" else report.get("status")
    if report.get("_artifact_error"):
        status = report.get("status")
    return {
        "status": status,
        "run_type": report.get("run_type"),
        "record_count": report.get("record_count"),
        "ocr_quality_metrics": report.get("ocr_quality_metrics"),
        "image_kind_coverage": report.get("image_kind_coverage"),
        "real_scene_assessment": report.get("real_scene_assessment"),
        "claim_boundary": report.get("claim_boundary"),
        **({"invalid_reason": invalid_reason} if invalid_reason and invalid_reason != "artifact_load_failed" else {}),
        **({"_artifact_error_type": report.get("_artifact_error_type"), "_artifact_error": report.get("_artifact_error")} if report.get("_artifact_error") else {}),
    }


def embedded_report(artifact: dict[str, Any], key: str) -> dict[str, Any]:
    if artifact.get("_artifact_error"):
        return artifact
    candidate = artifact.get(key)
    if isinstance(candidate, dict):
        return candidate
    if key in artifact:
        return {
            "status": "invalid",
            "_artifact_error_type": "InvalidEmbeddedReport",
            "_artifact_error": f"expected object at {key}",
        }
    return artifact


def validate_jsonl_object_rows(path: Path, *, report: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return {"status": "invalid", "reason": "missing_jsonl"}
    rows: list[dict[str, Any]] = []
    saw_non_empty_line = False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            saw_non_empty_line = True
            payload = json.loads(line)
            if not isinstance(payload, dict) or not payload:
                return {"status": "invalid", "reason": "non_object_jsonl_row"}
            rows.append(payload)
    except json.JSONDecodeError:
        return {"status": "invalid", "reason": "malformed_jsonl"}
    if not saw_non_empty_line:
        return {"status": "invalid", "reason": "empty_jsonl"}
    object_count = len(rows)
    if object_count == 0:
        return {"status": "invalid", "reason": "no_json_object_rows"}
    if report:
        if report.get("status") == "completed":
            report_shape_error = _authorized_rerun_completed_report_shape_error(report)
            if report_shape_error:
                return {"status": "invalid", "reason": report_shape_error}
        row_count = _int_or_none(report.get("row_count"))
        if row_count is not None and object_count != row_count:
            return {"status": "invalid", "reason": "row_count_mismatch", "object_row_count": object_count}
        real_external_count = sum(1 for row in rows if row.get("is_real_external_source") is True)
        expected_real_external_count = _int_or_none(report.get("real_external_row_count"))
        if expected_real_external_count is not None and real_external_count != expected_real_external_count:
            return {
                "status": "invalid",
                "reason": "real_external_row_count_mismatch",
                "real_external_row_count": real_external_count,
            }
        if any(
            row.get("is_real_external_source") is True
            and not _is_claimable_authorized_rerun_jsonl_row(row)
            for row in rows
        ):
            return {"status": "invalid", "reason": "real_external_row_without_claimable_evidence"}
        if report.get("status") == "completed" and not any(_is_claimable_authorized_rerun_jsonl_row(row) for row in rows):
            return {"status": "invalid", "reason": "completed_report_without_claimable_rows"}
        covered_group_mismatch = _authorized_rerun_group_mismatch(rows, report)
        if covered_group_mismatch:
            return {
                "status": "invalid",
                "reason": "covered_group_count_mismatch",
                **covered_group_mismatch,
            }
    return {"status": "valid", "object_row_count": object_count}


def _authorized_rerun_completed_report_shape_error(report: dict[str, Any]) -> str:
    if _int_or_none(report.get("row_count")) is None:
        return "missing_report_row_count"
    if _int_or_none(report.get("real_external_row_count")) is None:
        return "missing_report_real_external_row_count"
    covered_groups = nested(report, "source_coverage", "covered_groups")
    if not isinstance(covered_groups, dict):
        return "missing_report_covered_groups"
    for value in covered_groups.values():
        if _int_or_none(value) is None:
            return "missing_report_covered_groups"
    return ""


def _is_claimable_authorized_rerun_jsonl_row(row: dict[str, Any]) -> bool:
    return (
        row.get("is_real_external_source") is True
        and bool(row.get("capture_snapshot_uri"))
        and bool(row.get("raw_payload_uri"))
        and bool(row.get("source_groups"))
    )


def _authorized_rerun_group_mismatch(rows: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    checked_groups = (
        "real_telegram",
        "public_account_or_article",
        "secondhand_market",
        "crowdsourcing_platform",
        "im_or_group",
        "social_or_forum",
        "vertical_or_technical",
    )
    covered_groups = nested(report, "source_coverage", "covered_groups") or {}
    if not isinstance(covered_groups, dict):
        return {}
    actual_counts = {group: 0 for group in checked_groups}
    for row in rows:
        if row.get("is_real_external_source") is not True:
            continue
        groups = _source_groups_from_jsonl_row(row)
        for group in checked_groups:
            if group in groups:
                actual_counts[group] += 1
    for group in checked_groups:
        expected = _int_or_none(covered_groups.get(group))
        if expected is None or expected == 0:
            continue
        if actual_counts.get(group, 0) != expected:
            return {
                "group": group,
                "expected_count": expected,
                "actual_count": actual_counts.get(group, 0),
            }
    return {}


def _source_groups_from_jsonl_row(row: dict[str, Any]) -> set[str]:
    groups = row.get("source_groups")
    if isinstance(groups, str):
        return {item.strip() for item in groups.split(",") if item.strip()}
    if isinstance(groups, list):
        return {str(item).strip() for item in groups if str(item).strip()}
    return set()


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def demo_paths() -> dict[str, Any]:
    return {
        "offline_stable_demo": {
            "mode": "local_corpus_and_evidence_pack",
            "command": "python scripts/run_agent_cli.py --demo-sample --show summary --dry-run",
        },
        "authorized_network_demo": {
            "mode": "authorized_live_sources_when_configured",
            "command": (
                "python scripts/run_agent_cli.py --query \"诈骗引流线索\" --enable-network "
                "--routing-profile high_recall "
                "--source-config-path config/intel_sources.acceptance_telegramnav_live.yaml "
                "--max-sources 4 --show summary"
            ),
        },
        "credential_boundary": (
            "If X/TG credentials are missing, source-specific collectors are skipped/not claimed; "
            "live coverage is claimed only when credentialed live artifacts are attached."
        ),
    }


def collect_gate_failures(
    summary: dict[str, Any],
    report_sources: dict[str, tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for name, source in summary["artifact_sources"].items():
        if not source.get("exists"):
            failures.append(
                {
                    "type": "missing_artifact",
                    "name": name,
                    "path": source.get("path"),
                }
            )
    for name, path_and_report in report_sources.items():
        path, report = path_and_report
        if report.get("_artifact_error"):
            failures.append(
                {
                    "type": "artifact_parse_error",
                    "name": name,
                    "path": path,
                    "error_type": report.get("_artifact_error_type"),
                    "error": report.get("_artifact_error"),
                }
            )
            continue
        if report.get("status") != "completed":
            failures.append(
                {
                    "type": "report_not_completed",
                    "name": name,
                    "path": path,
                    "status": report.get("status"),
                }
            )
    for result in summary["commands"]:
        if result.get("status") not in {"passed", "skipped"} or result.get("returncode") not in {0, None}:
            failures.append(
                {
                    "type": "command_failed",
                    "name": result.get("name"),
                    "command": result.get("command"),
                    "status": result.get("status"),
                    "returncode": result.get("returncode"),
                }
            )
    return failures


def nested(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def write_summary(summary: dict[str, Any], output: str | Path) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(".")
    command_results = run_gate_commands(args, cwd=root)
    summary = build_summary(root=root, command_results=command_results)
    write_summary(summary, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
