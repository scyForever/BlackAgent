"""Configuration loading utilities for the BlackAgent MVP.

The MVP keeps runtime configuration externalized in ``config/config.yaml`` so the
FastAPI entrypoint and future workers can share one typed contract without
importing heavier pipeline modules at application import time.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class AppConfig(BaseModel):
    """Top-level application identity returned by health checks."""

    model_config = ConfigDict(extra="allow")

    name: str = "BlackAgent"
    mode: str = "controlled_exploration"
    year: int = 2026
    environment: str = "local"


class ApiConfig(BaseModel):
    """HTTP API route configuration."""

    model_config = ConfigDict(extra="allow")

    prefix: str = "/api/v1"

    @field_validator("prefix")
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        value = value.strip() or "/api/v1"
        return value if value.startswith("/") else f"/{value}"


class PipelineConfig(BaseModel):
    """Backbone pipeline knobs shared by API wiring and the orchestrator."""

    model_config = ConfigDict(extra="allow")

    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    max_input_chars: int = Field(default=4000, gt=0)
    default_legal_basis: str = "PUBLIC_COMPLIANT_DATA"


class SandboxConfig(BaseModel):
    """Controlled exploration sandbox budget defaults."""

    model_config = ConfigDict(extra="allow")

    max_rounds: int = Field(default=3, ge=1)
    max_tokens: int = Field(default=4096, ge=1)
    max_elapsed_ms: int = Field(default=25_000, ge=1)


class StorageConfig(BaseModel):
    """Storage backend selection for local, SQL, or service-backed mode."""

    model_config = ConfigDict(extra="allow")

    backend: str = "memory"
    review_queue: str = "memory"
    dsn: str | None = None
    auto_create_schema: bool = True
    connect_timeout_seconds: float = Field(default=5.0, gt=0)


class TaskConfig(BaseModel):
    """Background task execution backend."""

    model_config = ConfigDict(extra="allow")

    backend: str = "local"
    persist: bool = True
    max_workers: int = Field(default=2, ge=1)


class NetworkConfig(BaseModel):
    """Explicit opt-in controls for real HTTP(S) source collection."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_records_per_fetch: int = Field(default=100, ge=1)
    user_agent: str = "BlackAgent-HTTPFeedCollector/0.1"
    rate_limit_per_minute: int = Field(default=0, ge=0)
    retry_attempts: int = Field(default=0, ge=0)
    retry_backoff_seconds: float = Field(default=0.0, ge=0.0)
    retry_backoff_multiplier: float = Field(default=2.0, ge=1.0)
    retry_statuses: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])


class EnforcementConfig(BaseModel):
    """High-impact production enforcement controls."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    dry_run: bool = True
    require_human_approval: bool = True
    min_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    max_actions_per_run: int = Field(default=50, ge=1)
    allowed_actions: list[str] = Field(default_factory=lambda: ["ban", "block", "blacklist", "intercept"])
    allowed_target_types: list[str] = Field(
        default_factory=lambda: ["account", "domain", "url", "ip", "phone", "device", "merchant", "group"]
    )
    connector: str = "audit"
    webhook_url: str | None = None
    webhook_token: str | None = None
    require_production_token: bool = True
    production_safety_token: str | None = None


class LLMConfig(BaseModel):
    """OpenAI-compatible LLM gateway configuration."""

    model_config = ConfigDict(extra="allow")

    provider: str = "mock"
    enabled: bool = False
    base_url: str | None = None
    api_key: str | None = None
    model: str = "gpt-5.5"
    service_tier: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    dry_run: bool = True


class LabelConfig(BaseModel):
    """Location of the governed label schema."""

    model_config = ConfigDict(extra="allow")

    schema_path: str = "config/label_schema.json"


class PromptConfig(BaseModel):
    """Prompt registry paths used by classifier/extractor/exploration workers."""

    model_config = ConfigDict(extra="allow")

    classifier: str = "prompts/classifier_prompt_v1.yaml"
    extractor: str = "prompts/extractor_prompt_v1.yaml"
    exploration: str = "prompts/exploration_prompt_v1.yaml"


class Settings(BaseModel):
    """Typed settings object for the BlackAgent application."""

    model_config = ConfigDict(extra="allow")

    app: AppConfig = Field(default_factory=AppConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    tasks: TaskConfig = Field(default_factory=TaskConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    enforcement: EnforcementConfig = Field(default_factory=EnforcementConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    labels: LabelConfig = Field(default_factory=LabelConfig)
    prompts: PromptConfig = Field(default_factory=PromptConfig)


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` placeholders in YAML values."""

    if isinstance(value, str):
        placeholder = ENV_PLACEHOLDER_RE.match(value.strip())
        if placeholder and placeholder.group(1) not in os.environ:
            return None
        expanded = os.path.expandvars(value)
        if expanded == "":
            return None
        return expanded
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def resolve_project_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    """Resolve a project-relative path without changing the caller's CWD."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    """Read a YAML mapping and expand environment-variable placeholders."""

    config_path = resolve_project_path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as file_obj:
        loaded = yaml.safe_load(file_obj) or {}

    if not isinstance(loaded, dict):
        raise ValueError(f"Config root must be a mapping: {config_path}")

    return _expand_env(loaded)


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load and validate project settings from YAML."""

    raw = load_yaml_file(config_path or DEFAULT_CONFIG_PATH)
    return Settings.model_validate(raw)


@lru_cache(maxsize=8)
def get_settings(config_path: str | None = None) -> Settings:
    """Cached settings accessor for FastAPI dependencies."""

    return load_settings(config_path)


def load_label_schema(settings: Settings | None = None) -> dict[str, Any]:
    """Load the configured label schema JSON document."""

    active_settings = settings or get_settings()
    schema_path = resolve_project_path(active_settings.labels.schema_path)
    if not schema_path.exists():
        raise FileNotFoundError(f"Label schema not found: {schema_path}")
    with schema_path.open("r", encoding="utf-8") as file_obj:
        loaded = json.load(file_obj)
    if not isinstance(loaded, dict):
        raise ValueError(f"Label schema root must be a mapping: {schema_path}")
    return loaded
