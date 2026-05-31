"""Load versioned risk, entity, slang, polarity, and clue rules from config."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuleRegistry:
    """Central registry for deterministic rules that should not be hardcoded."""

    DEFAULT_FILES = {
        "taxonomy": "config/risk_taxonomy.yaml",
        "entity_patterns": "config/entity_patterns.yaml",
        "slang_dictionary": "config/slang_dictionary.yaml",
        "context_polarity": "config/context_polarity.yaml",
        "clue_generation": "config/clue_generation_rules.yaml",
        "model_stage_policy": "config/model_stage_policy.yaml",
    }

    def __init__(self, *, root: str | Path | None = None, files: dict[str, str] | None = None) -> None:
        self.root = Path(root or PROJECT_ROOT)
        self.files = {**self.DEFAULT_FILES, **dict(files or {})}
        self._cache: dict[str, dict[str, Any]] = {}

    def load_taxonomy(self) -> dict[str, Any]:
        return self._load("taxonomy").get("risk_taxonomy", {})

    def load_entity_patterns(self) -> dict[str, Any]:
        return self._load("entity_patterns").get("entity_patterns", {})

    def load_slang_dictionary(self) -> dict[str, str]:
        payload = self._load("slang_dictionary").get("slang_dictionary", {})
        return {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}

    def load_context_polarity(self) -> dict[str, Any]:
        return self._load("context_polarity").get("context_polarity", {})

    def load_clue_generation_rules(self) -> dict[str, Any]:
        return self._load("clue_generation").get("clue_generation_rules", {})

    def load_model_stage_policy(self) -> dict[str, Any]:
        return self._load("model_stage_policy").get("model_stage_policy", {})

    def version_hash(self) -> str:
        payload = {
            name: self._load(name)
            for name in sorted(self.files)
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def _load(self, name: str) -> dict[str, Any]:
        if name in self._cache:
            return self._cache[name]
        raw_path = self.files[name]
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            self._cache[name] = {}
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            loaded = {}
        self._cache[name] = loaded
        return loaded


__all__ = ["RuleRegistry"]
