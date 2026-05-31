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

    def classifier_policy(self) -> dict[str, Any]:
        payload = self._load("taxonomy").get("classifier_policy", {})
        return dict(payload) if isinstance(payload, dict) else {}

    def primary_terms_by_label(self) -> dict[str, tuple[str, ...]]:
        output: dict[str, tuple[str, ...]] = {}
        for _key, spec in self.load_taxonomy().items():
            if not isinstance(spec, dict):
                continue
            label = str(spec.get("name") or _key)
            terms = [str(item) for item in spec.get("primary_terms", []) if str(item).strip()]
            if terms:
                output[label] = tuple(terms)
        return output

    def promotion_markers_by_label(self) -> dict[str, tuple[str, ...]]:
        output: dict[str, tuple[str, ...]] = {}
        for _key, spec in self.load_taxonomy().items():
            if not isinstance(spec, dict):
                continue
            label = str(spec.get("name") or _key)
            markers = [str(item) for item in spec.get("promotion_markers", []) if str(item).strip()]
            if markers:
                output[label] = tuple(markers)
        return output

    def theme_priors(self) -> dict[str, tuple[str, int]]:
        output: dict[str, tuple[str, int]] = {}
        raw = self._load("taxonomy").get("theme_priors", {})
        if not isinstance(raw, dict):
            return output
        for theme, value in raw.items():
            if isinstance(value, dict):
                category = str(value.get("risk_category") or value.get("category") or "").strip()
                bonus = int(value.get("bonus") or 1)
            elif isinstance(value, list) and value:
                category = str(value[0]).strip()
                bonus = int(value[1] if len(value) > 1 else 1)
            else:
                continue
            if category:
                output[str(theme)] = (category, bonus)
        return output

    def secondary_rules(self) -> dict[str, dict[str, tuple[str, ...]]]:
        output: dict[str, dict[str, tuple[str, ...]]] = {}
        raw = self._load("taxonomy").get("secondary_labels", {})
        if not isinstance(raw, dict):
            return output
        for category, labels in raw.items():
            if not isinstance(labels, dict):
                continue
            output[str(category)] = {}
            for label, spec in labels.items():
                if isinstance(spec, dict):
                    terms = spec.get("terms", [])
                else:
                    terms = spec
                if isinstance(terms, list):
                    output[str(category)][str(label)] = tuple(str(item) for item in terms if str(item).strip())
        return output

    def labels(self) -> tuple[str, ...]:
        """Configured primary risk labels in taxonomy order."""

        labels: list[str] = []
        for key, spec in self.load_taxonomy().items():
            if not isinstance(spec, dict):
                continue
            label = str(spec.get("name") or key).strip()
            if label:
                labels.append(label)
        return tuple(dict.fromkeys(labels))

    def defensive_markers(self) -> tuple[str, ...]:
        polarity = self.load_context_polarity()
        markers = polarity.get("defensive_markers", [])
        return tuple(str(item) for item in markers if str(item).strip()) if isinstance(markers, list) else ()

    def context_markers(self, name: str) -> tuple[str, ...]:
        """Return a named marker list from ``context_polarity.yaml``."""

        polarity = self.load_context_polarity()
        markers = polarity.get(name, [])
        return tuple(str(item) for item in markers if str(item).strip()) if isinstance(markers, list) else ()

    def risk_marker_sets(self) -> dict[str, tuple[str, ...]]:
        """Configured high-risk marker sets for cleaner risk scoring.

        Primary taxonomy terms are emitted under the primary label; secondary
        label terms are emitted under their own label so labels such as
        ``接码注册`` and ``跑分代付`` remain visible in cleaned-record metadata
        without hardcoding those labels in the cleaner.
        """

        marker_sets: dict[str, list[str]] = {}
        for label, terms in self.primary_terms_by_label().items():
            marker_sets.setdefault(label, []).extend(terms)
        for _category, labels in self.secondary_rules().items():
            for secondary_label, terms in labels.items():
                marker_sets.setdefault(secondary_label, []).extend(terms)
        return {label: tuple(dict.fromkeys(terms)) for label, terms in marker_sets.items() if terms}

    def risk_hint_sets(self) -> dict[str, tuple[str, ...]]:
        """Configured theme hints used when collectors provide matched themes."""

        hints: dict[str, list[str]] = {}
        for theme, (category, _bonus) in self.theme_priors().items():
            hints.setdefault(category, []).append(theme)
        for _category, labels in self.secondary_rules().items():
            for secondary_label, terms in labels.items():
                hints.setdefault(secondary_label, []).extend(terms[:3])
        return {label: tuple(dict.fromkeys(values)) for label, values in hints.items() if values}

    def entity_pattern_specs(self, *, scopes: set[str] | None = None) -> list[dict[str, Any]]:
        """Return normalized entity pattern specs, optionally filtered by scope."""

        specs: list[dict[str, Any]] = []
        for name, spec in self.load_entity_patterns().items():
            if not isinstance(spec, dict):
                continue
            scope = str(spec.get("scope") or "advanced").strip().lower()
            if scopes is not None and scope not in scopes:
                continue
            raw_patterns = spec.get("patterns") if isinstance(spec.get("patterns"), list) else [spec.get("pattern")]
            patterns = [str(item) for item in raw_patterns if str(item or "").strip()]
            terms = [str(item) for item in (spec.get("terms") or []) if str(item).strip()] if isinstance(spec.get("terms"), list) else []
            specs.append(
                {
                    "name": str(name),
                    "scope": scope,
                    "entity_type": str(spec.get("entity_type") or name),
                    "patterns": patterns,
                    "terms": terms,
                    "method": str(spec.get("method") or f"configured_entity_pattern:{name}"),
                    "normalized_prefix": spec.get("normalized_prefix"),
                }
            )
        return specs

    def entity_terms(self, entity_type: str, *, scopes: set[str] | None = None) -> tuple[str, ...]:
        terms: list[str] = []
        for spec in self.entity_pattern_specs(scopes=scopes):
            if spec.get("entity_type") != entity_type:
                continue
            terms.extend(str(item) for item in spec.get("terms", []) if str(item).strip())
        return tuple(dict.fromkeys(terms))

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
