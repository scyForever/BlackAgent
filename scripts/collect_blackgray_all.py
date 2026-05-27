"""Unified black/gray raw collection entrypoint across public/X/Telegram sources."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_yaml_file, resolve_project_path
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


def main() -> int:
    args = parse_args()
    db_path = resolve_project_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and db_path.exists():
        db_path.unlink()

    results: list[dict[str, object]] = []
    exit_code = 0

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
        results.append(result)
        exit_code = max(exit_code, int(result["returncode"]))

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
            results.append(hydration_result)
            exit_code = max(exit_code, int(hydration_result["returncode"]))

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
            result = {
                "collector": "x",
                "skipped": True,
                "reason": "missing_x_credentials",
            }
        result.setdefault("collector", "x")
        results.append(result)
        exit_code = max(exit_code, int(result.get("returncode", 0) or 0))

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
            result = {
                "collector": "telegram",
                "skipped": True,
                "reason": "missing_telegram_credentials",
            }
        result.setdefault("collector", "telegram")
        results.append(result)
        exit_code = max(exit_code, int(result.get("returncode", 0) or 0))

    summary = {
        "status": "completed" if exit_code == 0 else "partial_failure",
        "db_path": str(db_path),
        "results": [
            {
                "collector": item.get("collector"),
                "returncode": item.get("returncode", 0),
                "skipped": item.get("skipped", False),
                "reason": item.get("reason"),
                "parsed_output": item.get("parsed_output"),
                "stderr": item.get("stderr"),
            }
            for item in results
        ],
        "db_summary": summarize_db(db_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
