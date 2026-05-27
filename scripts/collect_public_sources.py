"""Run BlackAgent batch collection against a source catalog and persist raw data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import create_app
from src.config_loader import PROJECT_ROOT as APP_PROJECT_ROOT, Settings, resolve_project_path
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect raw intelligence from a batch source catalog.")
    parser.add_argument(
        "--catalog",
        default="config/intel_sources.public.yaml",
        help="Project-relative source catalog YAML path (default: config/intel_sources.public.yaml)",
    )
    parser.add_argument(
        "--db",
        default="data/blackagent_public_sources.db",
        help="SQLite output path, project-relative or absolute (default: data/blackagent_public_sources.db)",
    )
    parser.add_argument("--timeout-seconds", type=float, default=25.0, help="Network timeout per source request")
    parser.add_argument("--max-records", type=int, default=20, help="Max records fetched per source")
    parser.add_argument(
        "--rate-limit-per-minute",
        type=int,
        default=8,
        help="Per-host request budget for batch public search collection (default: 8)",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help="Retry count for retryable HTTP statuses like 429 (default: 3)",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=8.0,
        help="Initial backoff seconds before retrying a throttled source (default: 8)",
    )
    parser.add_argument(
        "--retry-backoff-multiplier",
        type=float,
        default=2.0,
        help="Exponential multiplier for retry backoff (default: 2.0)",
    )
    parser.add_argument("--run-pipeline", action="store_true", help="Also run the Phase II/III pipeline after raw collection")
    parser.add_argument("--fresh", action="store_true", help="Delete the target SQLite file before collecting")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog_path = resolve_project_path(args.catalog)
    db_path = resolve_project_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and db_path.exists():
        db_path.unlink()

    settings = Settings(
        network={
            "enabled": True,
            "timeout_seconds": args.timeout_seconds,
            "max_records_per_fetch": args.max_records,
            "rate_limit_per_minute": args.rate_limit_per_minute,
            "retry_attempts": args.retry_attempts,
            "retry_backoff_seconds": args.retry_backoff_seconds,
            "retry_backoff_multiplier": args.retry_backoff_multiplier,
        },
        storage={
            "backend": "sql",
            "dsn": f"sqlite:///{db_path.as_posix()}",
            "auto_create_schema": True,
        },
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/sources/collect/batch",
            json={
                "source_config_path": str(catalog_path.relative_to(APP_PROJECT_ROOT)),
                "persist_raw": True,
                "run_pipeline": args.run_pipeline,
                "continue_on_error": True,
            },
        )
        payload = response.json()

    backend = connect(settings.storage.dsn)
    backend.create_schema()
    raw_rows = backend.list_raw()
    entity_rows = backend.list_entities()
    audit_rows = backend.list_audit()
    backend.close()

    summary = {
        "http_status": response.status_code,
        "catalog_path": str(catalog_path),
        "db_path": str(db_path),
        "status": payload.get("status"),
        "source_count": payload.get("source_count"),
        "succeeded_count": payload.get("succeeded_count"),
        "failed_count": payload.get("failed_count"),
        "fetched_count": payload.get("fetched_count"),
        "persisted_count": payload.get("persisted_count"),
        "stored_raw_count": len(raw_rows),
        "stored_entity_count": len(entity_rows),
        "audit_event_count": len(audit_rows),
        "sources": [
            {
                "source_name": item.get("source_name"),
                "fetched_count": item.get("fetched_count"),
                "error": item.get("error"),
                "sample": (item.get("raw_records") or [{}])[0].get("content_text", "")[:240],
                "matched_keywords": (item.get("raw_records") or [{}])[0].get("matched_keywords", []),
            }
            for item in payload.get("results", [])
        ],
    }
    if payload.get("pipeline_result"):
        summary["pipeline_result"] = {
            "risk_clue_count": payload["pipeline_result"].get("risk_clue_count"),
            "playbook_count": payload["pipeline_result"].get("playbook_count"),
            "strategy_count": payload["pipeline_result"].get("strategy_count"),
        }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
