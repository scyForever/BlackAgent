"""Unified black/gray raw collection entrypoint across public/X/Telegram sources."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_yaml_file, resolve_project_path
from src.scheduling.layered_collection import (
    LAYER_CLUE_BUILD,
    LAYER_FAST,
    LAYER_SLOW,
    LayeredIntervalConfig,
    LayeredRunPlanner,
    PendingClueBatch,
    build_candidate_clues_from_raw_rows,
    should_run_clue_build,
)
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect black/gray raw data from public/X/Telegram sources.")
    parser.add_argument("--db", default="data/blackagent_blackgray_all.db", help="Unified SQLite output path")
    parser.add_argument(
        "--public-catalog",
        default="config/intel_sources.blackgray.yaml",
        help="Source catalog for public/web search collection",
    )
    parser.add_argument("--x-config", default="config/x_watch.example.yaml", help="X collector config path")
    parser.add_argument(
        "--telegram-config",
        default="config/telegram_watch.example.yaml",
        help="Telegram collector config path",
    )
    parser.add_argument("--fresh", action="store_true", help="Delete unified DB before collecting")
    parser.add_argument("--skip-public", action="store_true", help="Skip public/web collection")
    parser.add_argument("--skip-hydration", action="store_true", help="Skip direct-page hydration for search-discovered rows")
    parser.add_argument("--skip-x", action="store_true", help="Skip X native collection")
    parser.add_argument("--skip-telegram", action="store_true", help="Skip Telegram native collection")
    parser.add_argument("--watch", action="store_true", help="Run layered collection in a long-lived watch loop")
    parser.add_argument("--fast-interval-seconds", type=int, default=60, help="Polling interval for Telegram/IM/X fast sources")
    parser.add_argument("--slow-interval-seconds", type=int, default=600, help="Polling interval for forum/page/hydration slow sources")
    parser.add_argument("--clue-build-interval-seconds", type=int, default=180, help="Retry interval for pending clue-build batches")
    parser.add_argument("--run-clue-build", action="store_true", help="Batch-build candidate clues from newly collected raw rows")
    parser.add_argument("--clue-quality-profile", default="high_precision", help="Quality profile for batched clue build")
    parser.add_argument("--clue-require-cross-source", action="store_true", help="Require cross-source evidence in batched clue build")
    parser.add_argument("--clue-require-evidence-chain", action="store_true", default=True, help="Require evidence chain in batched clue build")
    parser.add_argument("--clue-batch-min-records", type=int, default=1, help="Minimum pending raw rows before batched clue build runs")
    parser.add_argument("--max-watch-ticks", type=int, default=0, help="Optional max executed watch ticks before exit (0 = run forever)")
    parser.add_argument("--idle-sleep-seconds", type=float, default=1.0, help="Sleep duration when no watch layer is due")
    return parser.parse_args()


def run_child(command: list[str]) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603,S607 - explicit local script invocation
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = completed.stdout.strip()
    error = completed.stderr.strip()
    parsed_output = None
    if output:
        try:
            parsed_output = json.loads(output)
        except json.JSONDecodeError:
            parsed_output = None
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": output,
        "stderr": error,
        "parsed_output": parsed_output,
    }


def has_x_credentials(config_path: str) -> bool:
    cfg = load_yaml_file(resolve_project_path(config_path))
    bearer = str((cfg.get("x") or {}).get("bearer_token") or "").strip()
    return bool(bearer)


def has_telegram_credentials(config_path: str) -> bool:
    cfg = load_yaml_file(resolve_project_path(config_path))
    telegram_cfg = cfg.get("telegram") or {}
    return bool(telegram_cfg.get("api_id") and telegram_cfg.get("api_hash"))


def summarize_db(db_path: Path) -> dict[str, object]:
    if not db_path.exists():
        return {"db_exists": False, "raw_count": 0, "per_source": []}
    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    rows = backend.list_raw()
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("source_name") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    backend.close()
    return {
        "db_exists": True,
        "raw_count": len(rows),
        "per_source": [{"source_name": name, "count": counts[name]} for name in sorted(counts)],
    }


def load_raw_rows(db_path: Path) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    rows = backend.list_raw()
    backend.close()
    return rows


def build_trace_id_set(rows: list[dict[str, object]]) -> set[str]:
    return {
        str(row.get("trace_id") or row.get("hash_id") or "").strip()
        for row in rows
        if str(row.get("trace_id") or row.get("hash_id") or "").strip()
    }


def collect_new_raw_rows(db_path: Path, known_trace_ids: set[str]) -> list[dict[str, object]]:
    rows = load_raw_rows(db_path)
    fresh: list[dict[str, object]] = []
    current_ids: set[str] = set()
    for row in rows:
        trace_id = str(row.get("trace_id") or row.get("hash_id") or "").strip()
        if not trace_id:
            continue
        current_ids.add(trace_id)
        if trace_id not in known_trace_ids:
            fresh.append(row)
    known_trace_ids.clear()
    known_trace_ids.update(current_ids)
    return fresh


def run_clue_build_batch(
    *,
    db_path: Path,
    pending_batch: PendingClueBatch,
    quality_profile: str,
    require_cross_source: bool,
    require_evidence_chain: bool,
    min_records: int,
) -> dict[str, object]:
    if pending_batch.count() < max(1, int(min_records)):
        return {
            "status": "skipped",
            "reason": "insufficient_pending_records",
            "pending_count": pending_batch.count(),
        }
    rows = pending_batch.drain()
    result = build_candidate_clues_from_raw_rows(
        rows,
        quality_profile=quality_profile,
        require_cross_source=require_cross_source,
        require_evidence_chain=require_evidence_chain,
    )
    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    for clue in result.clues:
        backend.save_clue(clue)
    clue_count = len(backend.list_clues())
    backend.close()
    return {
        "status": result.status,
        "input_count": result.input_count,
        "saved_clue_count": result.saved_clue_count,
        "high_quality_count": result.high_quality_count,
        "candidate_count": result.candidate_count,
        "db_clue_count": clue_count,
        "execution_summary": result.execution_summary,
    }


def run_fast_layer(args: argparse.Namespace, db_path: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    if not args.skip_x:
        if has_x_credentials(args.x_config):
            result = run_child(
                [
                    sys.executable,
                    "scripts/x_recent_search_collector.py",
                    "--config",
                    args.x_config,
                    "--db",
                    str(db_path),
                    *(["--fresh-state"] if args.fresh else []),
                ]
            )
        else:
            result = {"collector": "x", "skipped": True, "reason": "missing_x_credentials"}
        result.setdefault("collector", "x")
        result["layer"] = LAYER_FAST
        results.append(result)

    if not args.skip_telegram:
        if has_telegram_credentials(args.telegram_config):
            result = run_child(
                [
                    sys.executable,
                    "scripts/telegram_telethon_collector.py",
                    "--config",
                    args.telegram_config,
                    "--db",
                    str(db_path),
                    "--once",
                    *(["--fresh-state"] if args.fresh else []),
                ]
            )
        else:
            result = {"collector": "telegram", "skipped": True, "reason": "missing_telegram_credentials"}
        result.setdefault("collector", "telegram")
        result["layer"] = LAYER_FAST
        results.append(result)
    return results


def run_slow_layer(args: argparse.Namespace, db_path: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    if not args.skip_public:
        result = run_child(
            [
                sys.executable,
                "scripts/collect_public_sources.py",
                "--catalog",
                args.public_catalog,
                "--db",
                str(db_path),
            ]
        )
        result["collector"] = "public"
        result["layer"] = LAYER_SLOW
        results.append(result)

        if not args.skip_hydration:
            hydration_result = run_child(
                [
                    sys.executable,
                    "scripts/hydrate_public_search_results.py",
                    "--db",
                    str(db_path),
                ]
            )
            hydration_result["collector"] = "public_hydration"
            hydration_result["layer"] = LAYER_SLOW
            results.append(hydration_result)
    return results


def summarize_results(results: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "collector": item.get("collector"),
            "layer": item.get("layer"),
            "returncode": item.get("returncode", 0),
            "skipped": item.get("skipped", False),
            "reason": item.get("reason"),
            "parsed_output": item.get("parsed_output"),
            "stderr": item.get("stderr"),
        }
        for item in results
    ]


def executed_returncode(results: list[dict[str, object]]) -> int:
    return max((int(item.get("returncode", 0) or 0) for item in results), default=0)


def main() -> int:
    args = parse_args()
    db_path = resolve_project_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and db_path.exists():
        db_path.unlink()

    known_trace_ids = build_trace_id_set(load_raw_rows(db_path))
    pending_batch = PendingClueBatch()
    results: list[dict[str, object]] = []
    exit_code = 0
    clue_build_runs: list[dict[str, object]] = []

    if not args.watch:
        fast_results = run_fast_layer(args, db_path)
        slow_results = run_slow_layer(args, db_path)
        results.extend(fast_results)
        results.extend(slow_results)
        exit_code = max(exit_code, executed_returncode(fast_results + slow_results))
        pending_batch.add_rows(collect_new_raw_rows(db_path, known_trace_ids))
        if args.run_clue_build:
            clue_result = run_clue_build_batch(
                db_path=db_path,
                pending_batch=pending_batch,
                quality_profile=args.clue_quality_profile,
                require_cross_source=bool(args.clue_require_cross_source),
                require_evidence_chain=bool(args.clue_require_evidence_chain),
                min_records=args.clue_batch_min_records,
            )
            clue_result["layer"] = LAYER_CLUE_BUILD
            clue_result["trigger"] = "one_shot_post_collection"
            clue_build_runs.append(clue_result)
    else:
        planner = LayeredRunPlanner(
            LayeredIntervalConfig(
                fast_interval_seconds=args.fast_interval_seconds,
                slow_interval_seconds=args.slow_interval_seconds,
                clue_build_interval_seconds=args.clue_build_interval_seconds,
            )
        )
        executed_ticks = 0
        while args.max_watch_ticks <= 0 or executed_ticks < args.max_watch_ticks:
            now = datetime.now(timezone.utc)
            due_layers = [layer for layer in planner.due_layers(now=now) if layer in {LAYER_FAST, LAYER_SLOW}]
            collection_layer_ran = False

            if LAYER_FAST in due_layers:
                fast_results = run_fast_layer(args, db_path)
                results.extend(fast_results)
                exit_code = max(exit_code, executed_returncode(fast_results))
                pending_batch.add_rows(collect_new_raw_rows(db_path, known_trace_ids))
                planner.mark_ran(LAYER_FAST, when=now)
                collection_layer_ran = True

            if LAYER_SLOW in due_layers:
                slow_results = run_slow_layer(args, db_path)
                results.extend(slow_results)
                exit_code = max(exit_code, executed_returncode(slow_results))
                pending_batch.add_rows(collect_new_raw_rows(db_path, known_trace_ids))
                planner.mark_ran(LAYER_SLOW, when=now)
                collection_layer_ran = True

            clue_due = planner.is_due(LAYER_CLUE_BUILD, now=now)
            if args.run_clue_build and should_run_clue_build(
                pending_count=pending_batch.count(),
                collection_layer_ran=collection_layer_ran,
                clue_layer_due=clue_due,
            ):
                clue_result = run_clue_build_batch(
                    db_path=db_path,
                    pending_batch=pending_batch,
                    quality_profile=args.clue_quality_profile,
                    require_cross_source=bool(args.clue_require_cross_source),
                    require_evidence_chain=bool(args.clue_require_evidence_chain),
                    min_records=args.clue_batch_min_records,
                )
                clue_result["layer"] = LAYER_CLUE_BUILD
                clue_result["trigger"] = "post_collection_batch" if collection_layer_ran else "scheduled_retry"
                clue_build_runs.append(clue_result)
                planner.mark_ran(LAYER_CLUE_BUILD, when=now)

            if due_layers:
                executed_ticks += 1
                continue

            time.sleep(max(0.1, float(args.idle_sleep_seconds)))

    summary = {
        "status": "completed" if exit_code == 0 else "partial_failure",
        "db_path": str(db_path),
        "watch_mode": bool(args.watch),
        "layer_intervals": {
            "fast_interval_seconds": int(args.fast_interval_seconds),
            "slow_interval_seconds": int(args.slow_interval_seconds),
            "clue_build_interval_seconds": int(args.clue_build_interval_seconds),
        },
        "results": summarize_results(results),
        "clue_build_runs": clue_build_runs,
        "pending_clue_batch_count": pending_batch.count(),
        "db_summary": summarize_db(db_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
