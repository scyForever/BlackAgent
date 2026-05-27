"""Run the SQL-backed BlackAgent collection queue/cron system for bounded cycles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_settings, resolve_project_path
from src.scheduling import CollectionQueueScheduler
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded local cron/queue runner for BlackAgent collection.")
    parser.add_argument("--config", default="config/config.yaml", help="Project settings YAML path")
    parser.add_argument("--db", default="", help="SQLite DB path override for scheduler/raw/clue state")
    parser.add_argument("--public-catalog", default="config/intel_sources.blackgray.yaml", help="Public source catalog path")
    parser.add_argument("--x-config", default="config/x_watch.example.yaml", help="X collector config path")
    parser.add_argument("--telegram-config", default="config/telegram_watch.example.yaml", help="Telegram collector config path")
    parser.add_argument("--worker-count", type=int, default=0, help="Logical worker count (0 = config default)")
    parser.add_argument("--claim-limit", type=int, default=0, help="Jobs claimed per worker per round (0 = config default)")
    parser.add_argument("--max-rounds", type=int, default=0, help="Max queue-claim rounds per cycle (0 = config default)")
    parser.add_argument("--cycles", type=int, default=1, help="How many bounded tick+worker cycles to run")
    parser.add_argument("--status-only", action="store_true", help="Only print current scheduler status")
    parser.add_argument("--bootstrap-only", action="store_true", help="Sync schedule definitions and exit")
    return parser.parse_args()


def sqlite_dsn_from_path(path: str) -> str:
    resolved = resolve_project_path(path)
    return f"sqlite:///{resolved.as_posix()}"


def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    dsn = args.db and sqlite_dsn_from_path(args.db)
    if not dsn:
        dsn = settings.scheduler.dsn or settings.storage.dsn or sqlite_dsn_from_path(settings.scheduler.default_db_path)

    backend = connect(dsn)
    backend.create_schema()
    scheduler = CollectionQueueScheduler(
        backend,
        start_immediately=settings.scheduler.start_immediately,
        default_worker_count=settings.scheduler.worker_count,
        claim_limit_per_worker=settings.scheduler.claim_limit_per_worker,
        max_claim_rounds=settings.scheduler.max_claim_rounds,
        retry_backoff_seconds=settings.scheduler.retry_backoff_seconds,
        clue_batch_limit=settings.scheduler.clue_batch_limit,
    )

    schedules = scheduler.sync_schedules(
        scheduler.default_schedules(
            public_catalog=args.public_catalog,
            x_config=args.x_config,
            telegram_config=args.telegram_config,
            fast_interval_seconds=settings.scheduler.fast_interval_seconds,
            slow_interval_seconds=settings.scheduler.slow_interval_seconds,
            clue_build_interval_seconds=settings.scheduler.clue_build_interval_seconds,
            lease_seconds=settings.scheduler.lease_seconds,
            max_attempts=settings.scheduler.max_attempts,
            cron_overrides=settings.scheduler.cron_overrides,
        )
    )

    if args.status_only:
        print(json.dumps({"status": "ok", **scheduler.status().model_dump()}, ensure_ascii=False, indent=2))
        backend.close()
        return 0

    if args.bootstrap_only:
        print(
            json.dumps(
                {"status": "bootstrapped", "schedule_count": len(schedules), "schedules": schedules},
                ensure_ascii=False,
                indent=2,
            )
        )
        backend.close()
        return 0

    cycles = max(1, int(args.cycles))
    cycle_summaries: list[dict[str, object]] = []
    for _ in range(cycles):
        tick_result = scheduler.tick()
        worker_result = scheduler.run_workers(
            worker_count=args.worker_count or settings.scheduler.worker_count,
            claim_limit=args.claim_limit or settings.scheduler.claim_limit_per_worker,
            max_rounds=args.max_rounds or settings.scheduler.max_claim_rounds,
        )
        cycle_summaries.append(
            {
                "tick": tick_result,
                "workers": worker_result,
                "status": scheduler.status().model_dump(),
            }
        )

    summary = {
        "status": "completed",
        "dsn": dsn,
        "cycle_count": cycles,
        "cycles": cycle_summaries,
        "final_status": scheduler.status().model_dump(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
