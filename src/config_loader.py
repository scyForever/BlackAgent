"""Configuration loading utilities for the BlackAgent local runtime."""

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
ENV_OVERRIDE_MAP: dict[str, tuple[str, ...]] = {
    "BLACKAGENT_LLM_PROVIDER": ("llm", "provider"),
    "BLACKAGENT_LLM_ENABLED": ("llm", "enabled"),
    "BLACKAGENT_LLM_BASE_URL": ("llm", "base_url"),
    "BLACKAGENT_LLM_API_KEY": ("llm", "api_key"),
    "BLACKAGENT_LLM_MODEL": ("llm", "model"),
    "BLACKAGENT_LLM_SERVICE_TIER": ("llm", "service_tier"),
    "BLACKAGENT_LLM_TIMEOUT_SECONDS": ("llm", "timeout_seconds"),
    "BLACKAGENT_LLM_DRY_RUN": ("llm", "dry_run"),
    "BLACKAGENT_LLM_AUTH_HEADER": ("llm", "auth_header"),
    "BLACKAGENT_LLM_MAX_TOKENS_PARAM": ("llm", "max_tokens_param"),
    "BLACKAGENT_LLM_RESPONSE_FORMAT_SUPPORTED": ("llm", "response_format_supported"),
}


class AppConfig(BaseModel):
    """Top-level application identity returned by health checks."""

    model_config = ConfigDict(extra="allow")

    name: str = "BlackAgent"
    mode: str = "llm_driven_investigation"
    year: int = 2026
    environment: str = "local"


class PipelineConfig(BaseModel):
    """Local intelligence-processing knobs."""

    model_config = ConfigDict(extra="allow")

    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    max_input_chars: int = Field(default=4000, gt=0)
    default_legal_basis: str = "PUBLIC_COMPLIANT_DATA"


class SandboxConfig(BaseModel):
    """Bounded analysis budget defaults for local safety utilities."""

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


class SchedulerConfig(BaseModel):
    """Bounded cron/queue orchestration for local collection workers."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    dsn: str | None = None
    bootstrap_on_start: bool = False
    start_immediately: bool = True
    worker_count: int = Field(default=3, ge=1)
    claim_limit_per_worker: int = Field(default=2, ge=1)
    max_claim_rounds: int = Field(default=6, ge=1)
    fast_interval_seconds: int = Field(default=60, ge=1)
    slow_interval_seconds: int = Field(default=600, ge=1)
    clue_build_interval_seconds: int = Field(default=180, ge=1)
    lease_seconds: int = Field(default=120, ge=1)
    retry_backoff_seconds: int = Field(default=45, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    clue_batch_limit: int = Field(default=500, ge=1)
    cron_overrides: dict[str, str] = Field(default_factory=dict)
    default_db_path: str = "data/blackagent_scheduler.db"


class NetworkConfig(BaseModel):
    """Explicit opt-in controls for real HTTP(S) source collection."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_records_per_fetch: int = Field(default=100, ge=1)
    user_agent: str = "BlackAgent-HTTPFeedCollector/0.1"
    max_concurrent_sources: int = Field(default=2, ge=1)
    rate_limit_per_minute: int = Field(default=0, ge=0)
    retry_attempts: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=1.0, ge=0.0)
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
    auth_header: str = "authorization"
    max_tokens_param: str = "max_tokens"
    response_format_supported: bool = True
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("auth_header")
    @classmethod
    def normalize_auth_header(cls, value: str) -> str:
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"bearer", "authorization"}:
            return "authorization"
        if normalized == "api-key":
            return "api-key"
        raise ValueError("auth_header must be one of authorization, bearer, api-key")

    @field_validator("max_tokens_param")
    @classmethod
    def normalize_max_tokens_param(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("max_tokens_param must be max_tokens or max_completion_tokens")
        return normalized


class InvestigationConfig(BaseModel):
    """Hybrid investigation routing and observability policy."""

    model_config = ConfigDict(extra="allow")

    live_collection_enabled: bool = True
    short_window_hours: int = Field(default=48, ge=1)
    balanced_min_pool_high_quality_count: int = Field(default=1, ge=0)
    high_precision_min_pool_high_quality_count: int = Field(default=2, ge=0)
    evidence_chain_min_pool_high_quality_count: int = Field(default=1, ge=0)
    min_cross_source_count: int = Field(default=2, ge=1)
    max_live_sources_when_pool_hit: int = Field(default=2, ge=1)
    retrieval_score_threshold_for_pool_merge: float = Field(default=0.0, ge=0.0)
    telemetry_enabled: bool = True


class InvestigationPolicyOverride(BaseModel):
    """Request-scoped routing/budget overrides for one investigation run."""

    model_config = ConfigDict(extra="forbid")

    live_collection_enabled: bool | None = None
    short_window_hours: int | None = Field(default=None, ge=1)
    balanced_min_pool_high_quality_count: int | None = Field(default=None, ge=0)
    high_precision_min_pool_high_quality_count: int | None = Field(default=None, ge=0)
    evidence_chain_min_pool_high_quality_count: int | None = Field(default=None, ge=0)
    min_cross_source_count: int | None = Field(default=None, ge=1)
    max_live_sources_when_pool_hit: int | None = Field(default=None, ge=1)
    retrieval_score_threshold_for_pool_merge: float | None = Field(default=None, ge=0.0)
    telemetry_enabled: bool | None = None
    minimum_quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    require_cross_source: bool | None = None
    require_evidence_chain: bool | None = None
    max_sources: int | None = Field(default=None, ge=1)
    max_raw_records: int | None = Field(default=None, ge=1)
    max_candidate_clues: int | None = Field(default=None, ge=1)
    max_llm_refine_clues: int | None = Field(default=None, ge=1)
    max_elapsed_seconds: int | None = Field(default=None, ge=1)


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
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    tasks: TaskConfig = Field(default_factory=TaskConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    enforcement: EnforcementConfig = Field(default_factory=EnforcementConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    investigation: InvestigationConfig = Field(default_factory=InvestigationConfig)
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


def load_project_env_file(path: str | Path | None = None) -> None:
    """Load project ``.env`` values into ``os.environ`` without overriding live env.

    The project intentionally avoids adding a hard dependency on python-dotenv.
    This tiny loader supports the simple ``KEY=value`` form used by local
    integration credentials and keeps existing shell-provided values authoritative.
    """

    env_path = resolve_project_path(path or PROJECT_ROOT / ".env")
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as file_obj:
        for raw_line in file_obj:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def _set_nested(raw: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor = raw
    for key in path[:-1]:
        nested = cursor.get(key)
        if not isinstance(nested, dict):
            nested = {}
            cursor[key] = nested
        cursor = nested
    cursor[path[-1]] = value


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply direct runtime env overrides for settings that are often secret-bound."""

    for env_name, config_path in ENV_OVERRIDE_MAP.items():
        value = os.getenv(env_name)
        if value is not None:
            _set_nested(raw, config_path, value)

    extra_body = os.getenv("BLACKAGENT_LLM_EXTRA_BODY")
    if extra_body:
        try:
            parsed_extra_body = json.loads(extra_body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"BLACKAGENT_LLM_EXTRA_BODY must be valid JSON: {exc}") from exc
        if not isinstance(parsed_extra_body, dict):
            raise ValueError("BLACKAGENT_LLM_EXTRA_BODY must be a JSON object")
        _set_nested(raw, ("llm", "extra_body"), parsed_extra_body)
    return raw


def resolve_project_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    """Resolve a project-relative path without changing the caller's CWD."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    """Read a YAML mapping and expand environment-variable placeholders."""

    load_project_env_file()
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
    raw = _apply_env_overrides(raw)
    return Settings.model_validate(raw)


@lru_cache(maxsize=8)
def get_settings(config_path: str | None = None) -> Settings:
    """Cached settings accessor for the local runtime."""

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
