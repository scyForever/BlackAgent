"""Development wrapper for the packaged BlackAgent CLI."""

from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from blackagent.interfaces.cli.main import (  # noqa: E402,F401
    DEFAULT_DEMO_QUERY,
    DEFAULT_LOCAL_CORPUS_LIMIT,
    DEFAULT_LOCAL_CORPUS_PATH,
    DEFAULT_SOURCE_CONFIG_CANDIDATES,
    DEMO_RECORDS,
    apply_runtime_overrides,
    discover_source_config_path,
    load_fixture_records,
    load_local_corpus_records,
    main,
    parse_args,
    policy_override_from_args,
    print_summary,
    records_from_args,
    run_agent,
)

__all__ = [
    "DEFAULT_DEMO_QUERY",
    "DEFAULT_LOCAL_CORPUS_LIMIT",
    "DEFAULT_LOCAL_CORPUS_PATH",
    "DEFAULT_SOURCE_CONFIG_CANDIDATES",
    "DEMO_RECORDS",
    "apply_runtime_overrides",
    "discover_source_config_path",
    "load_fixture_records",
    "load_local_corpus_records",
    "main",
    "parse_args",
    "policy_override_from_args",
    "print_summary",
    "records_from_args",
    "run_agent",
]


if __name__ == "__main__":
    raise SystemExit(main())
