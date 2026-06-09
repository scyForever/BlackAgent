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
SOURCE_SMOKE_REPORT = "data/source_smoke_report.json"
SOURCE_LIVE_SMOKE_REPORT = "data/source_live_smoke_report.json"
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
            "--hydrated",
            "data/acceptance_direct_final3_hydrated_pages.jsonl",
            "--output",
            JOINED_EVIDENCE_JSONL,
            "--report-out",
            JOINED_EVIDENCE_REPORT,
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
    source_smoke_exists = (root_path / SOURCE_SMOKE_REPORT).exists()
    source_live_smoke_exists = (root_path / SOURCE_LIVE_SMOKE_REPORT).exists()
    source_smoke_report = load_json(root_path / SOURCE_SMOKE_REPORT) if source_smoke_exists else {}
    source_live_smoke_report = (
        load_json(root_path / SOURCE_LIVE_SMOKE_REPORT)
        if source_live_smoke_exists
        else {}
    )

    artifact_sources = {
        "manual_heldout": artifact_metadata(root_path, MANUAL_HELDOUT_REPORT),
        "clue_recall": artifact_metadata(root_path, CLUE_RECALL_REPORT),
        "external_balanced_pack_report": artifact_metadata(root_path, EXTERNAL_BALANCED_REPORT),
        "external_balanced_pack_jsonl": artifact_metadata(root_path, EXTERNAL_BALANCED_JSONL),
        "joined_evidence_pack_report": artifact_metadata(root_path, JOINED_EVIDENCE_REPORT),
        "joined_evidence_pack_jsonl": artifact_metadata(root_path, JOINED_EVIDENCE_JSONL),
    }
    if source_smoke_exists:
        artifact_sources["source_smoke_report"] = artifact_metadata(root_path, SOURCE_SMOKE_REPORT)
    if source_live_smoke_exists:
        artifact_sources["source_live_smoke_report"] = artifact_metadata(root_path, SOURCE_LIVE_SMOKE_REPORT)

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
    report_sources = {
        "manual_heldout": (MANUAL_HELDOUT_REPORT, manual),
        "clue_recall": (CLUE_RECALL_REPORT, clues),
        "external_balanced_pack_report": (EXTERNAL_BALANCED_REPORT, external_report),
        "joined_evidence_pack_report": (JOINED_EVIDENCE_REPORT, joined_report),
    }
    if source_smoke_exists:
        report_sources["source_smoke_report"] = (SOURCE_SMOKE_REPORT, source_smoke_report)
    if source_live_smoke_exists:
        report_sources["source_live_smoke_report"] = (SOURCE_LIVE_SMOKE_REPORT, source_live_smoke_report)
    gate_failures = collect_gate_failures(summary, report_sources)
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
