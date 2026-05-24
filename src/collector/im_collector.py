"""Mock compliant intelligence collector for local JSONL fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .base_collector import build_raw_intelligence


DEFAULT_FIXTURE_PATH = Path("tests/fixtures/sample_raw.jsonl")


class MockCollector:
    """Stream RawIntelligence objects from a local JSONL fixture.

    The MVP collector intentionally performs no network IO and makes no attempt
    to bypass access controls.  It only converts authorized local fixture rows
    into the shared RawIntelligence data contract.
    """

    def __init__(self, jsonl_path: str | Path = DEFAULT_FIXTURE_PATH, *, encoding: str = "utf-8") -> None:
        self.jsonl_path = Path(jsonl_path)
        self.encoding = encoding

    def stream(self) -> Iterable[Any]:
        if not self.jsonl_path.exists():
            raise FileNotFoundError(f"MockCollector fixture not found: {self.jsonl_path}")

        with self.jsonl_path.open("r", encoding=self.encoding) as handle:
            for line_number, line in enumerate(handle, start=1):
                raw_line = line.strip()
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at {self.jsonl_path}:{line_number}: {exc}") from exc
                if not isinstance(payload, Mapping):
                    raise ValueError(f"JSONL row must be an object at {self.jsonl_path}:{line_number}")
                yield build_raw_intelligence(payload)

    def collect(self) -> list[Any]:
        return list(self.stream())

    def read_all(self) -> list[Any]:
        return self.collect()

    def __iter__(self) -> Iterable[Any]:
        return self.stream()


__all__ = ["DEFAULT_FIXTURE_PATH", "MockCollector"]

