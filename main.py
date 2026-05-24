"""FastAPI entrypoint for the BlackAgent MVP.

This module intentionally keeps orchestration dependencies behind delayed imports
so Worker A's API shell remains importable while other workers build schemas,
storage repositories, and the AgentOrchestrator in parallel.
"""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from hashlib import sha256
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.config_loader import PROJECT_ROOT, Settings, get_settings, resolve_project_path


class HealthResponse(BaseModel):
    status: str
    mode: str
    year: int


class PipelineRunRequest(BaseModel):
    """Input accepted by the MVP pipeline trigger.

    The endpoint accepts either a single text sample, inline fixture items, or a
    project-local JSON/JSONL fixture path. Extra fields are preserved for the
    future orchestrator instead of being rejected by Worker A's thin API layer.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    content_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("content_text", "text", "content"),
    )
    fixture_items: list[dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("fixture_items", "fixture_data", "items"),
    )
    fixture_path: str | None = Field(default=None, validation_alias=AliasChoices("fixture_path", "fixture"))
    source_type: str = "Manual"
    source_name: str = "api_request"
    source_url: str | None = None
    legal_basis: str | None = None
    dry_run: bool = True

    @model_validator(mode="after")
    def require_input(self) -> "PipelineRunRequest":
        if not self.content_text and not self.fixture_items and not self.fixture_path:
            raise ValueError("Provide content_text/text, fixture_items/items, or fixture_path/fixture.")
        return self


class PipelineRunResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    mode: str
    input_count: int
    standard_count: int = 0
    review_count: int = 0
    orchestrator_available: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ReviewTasksResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    count: int
    tasks: list[dict[str, Any]]
    source: str = "api_stub"


class ReviewDecisionRequest(BaseModel):
    """Human review workbench decision payload."""

    decision: str = Field(description="approved, misreport, uncertain, or escalate")
    reviewer: str = Field(default="system", min_length=1)
    notes: str | None = None
    edited_risk_type: str | None = None
    secondary_label: str | None = None
    corrected_entities: list[dict[str, Any]] = Field(default_factory=list)
    add_to_wordlist: bool = False

    @field_validator("decision")
    @classmethod
    def normalize_decision_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("decision must not be empty")
        return value


class ReviewDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    hypothesis_id: str
    decision: str
    review_state: dict[str, Any]
    audit_event: dict[str, Any]


class AuditEventsResponse(BaseModel):
    status: str
    count: int
    events: list[dict[str, Any]]


class AdvancedPipelineResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    mode: str
    input_count: int
    accepted_count: int
    dropped_count: int
    classification_count: int
    entity_count: int
    cluster_count: int
    risk_clue_count: int
    playbook_count: int
    strategy_count: int


class TaskSubmitResponse(BaseModel):
    status: str
    task_id: str
    task_status: str
    mode: str = "local_task_backend"


class TaskRunPendingResponse(BaseModel):
    status: str
    count: int
    tasks: list[dict[str, Any]]


class LLMChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    temperature: float = 0.0
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_messages(self) -> "LLMChatRequest":
        if not self.messages:
            raise ValueError("messages must not be empty")
        return self


class LLMChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    model: str
    content: str
    network_attempted: bool
    error: str | None = None
    parsed_json: dict[str, Any] | None = None


class BackendStatusResponse(BaseModel):
    status: str
    storage_backend: str
    storage_connected: bool
    storage_dsn: str | None = None
    task_backend: str
    network_enabled: bool
    network_allowed_domains: list[str]
    llm_provider: str
    llm_enabled: bool
    llm_dry_run: bool
    enforcement_enabled: bool
    enforcement_dry_run: bool
    enforcement_connector: str


class SourceCollectRequest(BaseModel):
    """Explicit request to fetch one authorized HTTP(S) intelligence feed."""

    model_config = ConfigDict(extra="allow")

    source_url: str
    source_name: str
    source_type: str = "THREAT_INTEL"
    legal_basis: str = "PUBLIC_COMPLIANT_DATA"
    feed_format: str = "auto"
    max_records: int | None = Field(default=None, ge=1)
    headers: dict[str, str] = Field(default_factory=dict)
    allowed_domains: list[str] = Field(default_factory=list)
    text_fields: list[str] = Field(default_factory=list)
    persist_raw: bool = True
    run_pipeline: bool = False

    @field_validator("feed_format")
    @classmethod
    def validate_feed_format(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"auto", "json", "jsonl", "csv", "txt", "html"}:
            raise ValueError("feed_format must be one of auto, json, jsonl, csv, txt, html")
        return normalized


class SourceCollectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    source_name: str
    fetched_count: int
    persisted_count: int
    network_attempted: bool
    raw_records: list[dict[str, Any]]
    pipeline_result: dict[str, Any] | None = None


class SourceBatchCollectRequest(BaseModel):
    """Batch request for multi-platform authorized source collection."""

    model_config = ConfigDict(extra="allow")

    source_config_path: str | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    persist_raw: bool = True
    run_pipeline: bool = False
    continue_on_error: bool = False

    @model_validator(mode="after")
    def require_batch_input(self) -> "SourceBatchCollectRequest":
        if not self.source_config_path and not self.sources:
            raise ValueError("Provide source_config_path or sources")
        return self


class SourceBatchItemResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_name: str
    source_url: str
    source_type: str
    fetched_count: int = 0
    network_attempted: bool = False
    raw_records: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class SourceBatchCollectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    source_count: int
    succeeded_count: int
    failed_count: int
    fetched_count: int
    persisted_count: int
    results: list[SourceBatchItemResult]
    pipeline_result: dict[str, Any] | None = None


class EnforcementExecuteRequest(BaseModel):
    """High-impact enforcement request; defaults cannot weaken configured gates."""

    actions: list[dict[str, Any]]
    approved: bool = False
    approval_id: str | None = None
    dry_run: bool | None = None
    production_safety_token: str | None = None

    @model_validator(mode="after")
    def require_actions(self) -> "EnforcementExecuteRequest":
        if not self.actions:
            raise ValueError("actions must not be empty")
        return self


class EnforcementExecuteResponse(BaseModel):
    status: str
    result_count: int
    results: list[dict[str, Any]]


def _read_fixture_items(fixture_path: str) -> list[dict[str, Any]]:
    """Read project-local JSON or JSONL fixture data for the API trigger."""

    path = resolve_project_path(fixture_path)
    if PROJECT_ROOT not in path.parents and path != PROJECT_ROOT:
        raise HTTPException(status_code=400, detail="fixture_path must stay inside the project directory")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"fixture_path not found: {fixture_path}")

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    try:
        if path.suffix.lower() == ".jsonl":
            items = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            loaded = json.loads(text)
            items = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid fixture JSON: {exc.msg}") from exc

    if not all(isinstance(item, dict) for item in items):
        raise HTTPException(status_code=400, detail="fixture data must contain JSON objects")
    return items


def _materialize_items(request: PipelineRunRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if request.fixture_items:
        items.extend(request.fixture_items)
    if request.fixture_path:
        items.extend(_read_fixture_items(request.fixture_path))
    if request.content_text:
        items.append(
            {
                "content_text": request.content_text,
                "source_type": request.source_type,
                "source_name": request.source_name,
                "source_url": request.source_url,
                "legal_basis": request.legal_basis,
            }
        )
    return items


def _load_orchestrator(settings: Settings) -> tuple[Any | None, str | None]:
    """Create the future AgentOrchestrator if another worker has provided it."""

    try:
        from src.agent.agent_orchestrator import AgentOrchestrator  # type: ignore
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("src.agent"):
            return None, f"orchestrator module unavailable: {exc.name}"
        return None, f"orchestrator import dependency unavailable: {exc.name}"
    except Exception as exc:  # Keep API import/use stable during parallel development.
        return None, f"orchestrator import failed: {exc.__class__.__name__}: {exc}"

    for kwargs in ({"settings": settings}, {"config": settings}, {}):
        try:
            return AgentOrchestrator(**kwargs), None
        except TypeError:
            continue
        except Exception as exc:
            return None, f"orchestrator initialization failed: {exc.__class__.__name__}: {exc}"
    return None, "orchestrator initialization signature is unsupported"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _invoke_orchestrator(orchestrator: Any, payload: dict[str, Any]) -> Any:
    """Call a likely orchestrator entrypoint without fixing Worker D's API yet."""

    for method_name in ("run_pipeline", "run", "process_batch", "process", "execute"):
        method = getattr(orchestrator, method_name, None)
        if not callable(method):
            continue
        if isinstance(payload.get("items"), list):
            try:
                return await _maybe_await(method(payload["items"]))
            except TypeError:
                pass
        try:
            return await _maybe_await(method(payload))
        except TypeError:
            continue
    raise HTTPException(status_code=501, detail="AgentOrchestrator has no supported run method")


def _normalize_pipeline_result(result: Any, input_count: int) -> PipelineRunResponse:
    if isinstance(result, BaseModel):
        result = result.model_dump(mode="json")
    elif hasattr(result, "model_dump"):
        result = result.model_dump()
    elif is_dataclass(result):
        result = asdict(result)
    if not isinstance(result, dict):
        return PipelineRunResponse(
            status="completed",
            mode="orchestrator",
            input_count=input_count,
            orchestrator_available=True,
            details={"result": result},
        )

    return PipelineRunResponse(
        status=str(result.get("status", "completed")),
        mode=str(result.get("mode", "orchestrator")),
        input_count=int(result.get("input_count", input_count)),
        standard_count=int(result.get("standard_count", result.get("entity_count", 0) or 0)),
        review_count=int(result.get("review_count", result.get("review_required_count", 0) or 0)),
        orchestrator_available=True,
        details={key: value for key, value in result.items() if key not in {"status", "mode", "input_count", "standard_count", "entity_count", "review_count", "review_required_count"}},
    )


def _stub_pipeline_response(items: list[dict[str, Any]], settings: Settings, reason: str | None) -> PipelineRunResponse:
    return PipelineRunResponse(
        status="accepted",
        mode=settings.app.mode,
        input_count=len(items),
        standard_count=0,
        review_count=0,
        orchestrator_available=False,
        details={
            "message": "Pipeline request accepted by API shell; AgentOrchestrator is not wired yet.",
            "reason": reason,
            "dry_run": True,
        },
    )


def _coerce_task(task: Any) -> dict[str, Any]:
    if isinstance(task, BaseModel):
        return task.model_dump(mode="json")
    if hasattr(task, "model_dump"):
        return task.model_dump()
    if is_dataclass(task):
        return asdict(task)
    if isinstance(task, dict):
        return task
    return {"value": task}


async def _load_review_tasks(settings: Settings, status: str | None = "PENDING") -> tuple[list[dict[str, Any]], str]:
    """Best-effort delayed wiring for future review repository/orchestrator APIs."""

    orchestrator, reason = _load_orchestrator(settings)
    if orchestrator is not None:
        for method_name in ("list_review_tasks", "get_review_tasks", "review_tasks"):
            method = getattr(orchestrator, method_name, None)
            if callable(method):
                try:
                    result = await _maybe_await(method(status=status))
                except TypeError:
                    result = await _maybe_await(method())
                if isinstance(result, dict) and "tasks" in result:
                    result = result["tasks"]
                if isinstance(result, list):
                    return [_coerce_task(task) for task in result], "orchestrator"
        repo = getattr(orchestrator, "review_repo", None)
        if repo is not None:
            for method_name in ("list", "list_tasks", "all"):
                method = getattr(repo, method_name, None)
                if callable(method):
                    try:
                        result = await _maybe_await(method(status=status))
                    except TypeError:
                        result = await _maybe_await(method())
                    if isinstance(result, list):
                        return [_coerce_task(task) for task in result], "orchestrator.review_repo"

    return [], reason or "api_stub"


def _normalize_status_filter(status: str | None) -> str | None:
    if status is None:
        return "PENDING"
    normalized = status.strip().upper()
    if normalized in {"", "ALL", "*"}:
        return None
    return normalized


def create_app(settings_override: Settings | None = None) -> FastAPI:
    settings = settings_override or get_settings()

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        yield
        sql_backend = getattr(app_instance.state, "sql_backend", None)
        if sql_backend is not None and hasattr(sql_backend, "close"):
            sql_backend.close()
            app_instance.state.sql_backend = None
            app_instance.state.sql_backend_initialized = False

    app = FastAPI(
        title=settings.app.name,
        version="0.1.0",
        description="BlackAgent MVP API shell with controlled-exploration wiring.",
        lifespan=lifespan,
    )
    app.state.orchestrator_initialized = False
    app.state.orchestrator = None
    app.state.orchestrator_reason = None
    app.state.phase_engine = None
    app.state.task_backend = None
    app.state.llm_gateway = None
    app.state.sql_backend = None
    app.state.sql_backend_initialized = False

    def get_orchestrator() -> tuple[Any | None, str | None]:
        if not app.state.orchestrator_initialized:
            orchestrator, reason = _load_orchestrator(settings)
            app.state.orchestrator = orchestrator
            app.state.orchestrator_reason = reason
            app.state.orchestrator_initialized = True
        return app.state.orchestrator, app.state.orchestrator_reason

    def get_phase_engine() -> Any:
        if app.state.phase_engine is None:
            from src.enhancement.engine import PhaseTwoThreeEngine

            app.state.phase_engine = PhaseTwoThreeEngine()
        return app.state.phase_engine

    def get_task_backend() -> Any:
        if app.state.task_backend is None:
            from src.backend import TaskBackend

            app.state.task_backend = TaskBackend(execution_mode="sync")
        return app.state.task_backend

    def get_llm_gateway() -> Any:
        if app.state.llm_gateway is None:
            from src.backend import LLMGateway

            app.state.llm_gateway = LLMGateway(
                base_url=settings.llm.base_url,
                api_key=settings.llm.api_key,
                model=settings.llm.model,
                service_tier=settings.llm.service_tier,
                dry_run=(settings.llm.dry_run or not settings.llm.enabled),
                mock=settings.llm.provider.lower() == "mock",
                timeout_seconds=settings.llm.timeout_seconds,
            )
        return app.state.llm_gateway

    def get_sql_backend() -> Any | None:
        if app.state.sql_backend_initialized:
            return app.state.sql_backend
        app.state.sql_backend_initialized = True
        if not settings.storage.dsn or settings.storage.backend.lower() not in {"sql", "sqlite", "postgres", "postgresql"}:
            app.state.sql_backend = None
            return None
        from storage import connect

        backend = connect(settings.storage.dsn)
        if settings.storage.auto_create_schema:
            backend.create_schema()
        app.state.sql_backend = backend
        return backend

    def persist_task(record: Any) -> None:
        sql_backend = get_sql_backend()
        if sql_backend is not None:
            payload = _coerce_task(record)
            sql_backend.save_task(payload, task_type=payload.get("name"), status=payload.get("status"))

    def persist_advanced_result(items: list[dict[str, Any]], result: Any, *, persist_raw_items: bool = True) -> None:
        sql_backend = get_sql_backend()
        if sql_backend is None:
            return
        if persist_raw_items:
            for item in items:
                sql_backend.save_raw(_raw_payload_for_storage(item))
        result_payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        for entity in result_payload.get("entities", []):
            sql_backend.save_entity(entity)
        for strategy in result_payload.get("strategies", []):
            sql_backend.append_audit(
                {
                    "event_id": str(uuid4()),
                    "event_type": "candidate_strategy_generated",
                    "actor": "advanced_pipeline",
                    "target_id": strategy.get("strategy_id"),
                    "payload": strategy,
                }
            )

    def collect_source_records(request: SourceCollectRequest) -> list[dict[str, Any]]:
        from src.collector import HTTPFeedCollector, HTTPFeedConfig
        from src.collector.base_collector import model_dump as dump_record

        max_records = min(
            request.max_records or settings.network.max_records_per_fetch,
            settings.network.max_records_per_fetch,
        )
        collector = HTTPFeedCollector(
            HTTPFeedConfig(
                source_url=request.source_url,
                source_name=request.source_name,
                source_type=request.source_type,
                legal_basis=request.legal_basis,
                feed_format=request.feed_format,
                max_records=max_records,
                timeout_seconds=settings.network.timeout_seconds,
                user_agent=settings.network.user_agent,
                allowed_domains=tuple(request.allowed_domains or settings.network.allowed_domains),
                headers=request.headers,
                text_fields=tuple(request.text_fields) if request.text_fields else HTTPFeedConfig.text_fields,
                network_enabled=settings.network.enabled,
            )
        )
        return [dump_record(record) for record in collector.collect()]

    def load_batch_source_requests(request: SourceBatchCollectRequest) -> list[SourceCollectRequest]:
        raw_sources: list[dict[str, Any]] = []
        if request.source_config_path:
            config_path = resolve_project_path(request.source_config_path)
            if PROJECT_ROOT not in config_path.parents and config_path != PROJECT_ROOT:
                raise HTTPException(status_code=400, detail="source_config_path must stay inside the project directory")
            from src.collector import SourceCatalogError, load_source_catalog

            try:
                raw_sources.extend(load_source_catalog(config_path))
            except (FileNotFoundError, SourceCatalogError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        raw_sources.extend(request.sources)

        normalized_requests: list[SourceCollectRequest] = []
        for raw_source in raw_sources:
            try:
                normalized_requests.append(
                    SourceCollectRequest.model_validate(
                        {
                            **raw_source,
                            "persist_raw": False,
                            "run_pipeline": False,
                        }
                    )
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"invalid source definition: {exc}") from exc
        return normalized_requests

    def persist_raw_records(records: list[dict[str, Any]]) -> int:
        sql_backend = get_sql_backend()
        if sql_backend is None:
            return 0
        for record in records:
            sql_backend.save_raw(record)
        return len(records)

    def append_audit_if_available(event: dict[str, Any]) -> None:
        sql_backend = get_sql_backend()
        if sql_backend is not None:
            sql_backend.append_audit(event)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="healthy", mode=settings.app.mode, year=settings.app.year)

    @app.get(f"{settings.api.prefix}/system/backend", response_model=BackendStatusResponse)
    def backend_status() -> BackendStatusResponse:
        storage_connected = get_sql_backend() is not None
        return BackendStatusResponse(
            status="ok",
            storage_backend=settings.storage.backend,
            storage_connected=storage_connected,
            storage_dsn=_safe_dsn(settings.storage.dsn),
            task_backend=settings.tasks.backend,
            network_enabled=settings.network.enabled,
            network_allowed_domains=settings.network.allowed_domains,
            llm_provider=settings.llm.provider,
            llm_enabled=settings.llm.enabled,
            llm_dry_run=settings.llm.dry_run or not settings.llm.enabled,
            enforcement_enabled=settings.enforcement.enabled,
            enforcement_dry_run=settings.enforcement.dry_run,
            enforcement_connector=settings.enforcement.connector,
        )

    @app.post(f"{settings.api.prefix}/sources/collect", response_model=SourceCollectResponse)
    async def collect_source(request: SourceCollectRequest) -> SourceCollectResponse:
        try:
            records = collect_source_records(request)
        except Exception as exc:
            from src.collector import NetworkCollectionDisabled, SourceAuthorizationError

            if isinstance(exc, NetworkCollectionDisabled):
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            if isinstance(exc, SourceAuthorizationError):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise

        persisted_count = persist_raw_records(records) if request.persist_raw else 0
        pipeline_result = None
        if request.run_pipeline:
            result = get_phase_engine().run(records)
            if request.persist_raw:
                persist_advanced_result(records, result, persist_raw_items=False)
            pipeline_result = result.model_dump()

        append_audit_if_available(
            {
                "event_id": str(uuid4()),
                "event_type": "source_collect_completed",
                "actor": "source_collect_api",
                "target_id": request.source_name,
                "payload": {
                    "source_name": request.source_name,
                    "source_url": request.source_url,
                    "fetched_count": len(records),
                    "persisted_count": persisted_count,
                    "run_pipeline": request.run_pipeline,
                },
            }
        )
        return SourceCollectResponse(
            status="completed",
            source_name=request.source_name,
            fetched_count=len(records),
            persisted_count=persisted_count,
            network_attempted=True,
            raw_records=records,
            pipeline_result=pipeline_result,
        )

    @app.post(f"{settings.api.prefix}/tasks/sources/collect", response_model=TaskSubmitResponse)
    async def submit_source_collect_task(request: SourceCollectRequest) -> TaskSubmitResponse:
        def run_task(payload: dict[str, Any]) -> dict[str, Any]:
            source_request = SourceCollectRequest.model_validate(payload)
            records = collect_source_records(source_request)
            persisted_count = persist_raw_records(records) if source_request.persist_raw else 0
            pipeline_result = None
            if source_request.run_pipeline:
                result = get_phase_engine().run(records)
                if source_request.persist_raw:
                    persist_advanced_result(records, result, persist_raw_items=False)
                pipeline_result = result.model_dump()
            return {
                "status": "completed",
                "source_name": source_request.source_name,
                "fetched_count": len(records),
                "persisted_count": persisted_count,
                "pipeline_result": pipeline_result,
            }

        task = get_task_backend().submit(
            "source_collect",
            request.model_dump(),
            handler=run_task,
            metadata={"api": f"{settings.api.prefix}/tasks/sources/collect"},
        )
        persist_task(task)
        return TaskSubmitResponse(status="accepted", task_id=task.task_id, task_status=task.status.value)

    @app.post(f"{settings.api.prefix}/sources/collect/batch", response_model=SourceBatchCollectResponse)
    async def collect_sources_batch(request: SourceBatchCollectRequest) -> SourceBatchCollectResponse:
        source_requests = load_batch_source_requests(request)
        results: list[SourceBatchItemResult] = []
        all_records: list[dict[str, Any]] = []
        succeeded_count = 0
        failed_count = 0

        for source_request in source_requests:
            try:
                records = collect_source_records(source_request)
                all_records.extend(records)
                results.append(
                    SourceBatchItemResult(
                        source_name=source_request.source_name,
                        source_url=source_request.source_url,
                        source_type=source_request.source_type,
                        fetched_count=len(records),
                        network_attempted=True,
                        raw_records=records,
                    )
                )
                succeeded_count += 1
            except Exception as exc:
                failed_count += 1
                results.append(
                    SourceBatchItemResult(
                        source_name=source_request.source_name,
                        source_url=source_request.source_url,
                        source_type=source_request.source_type,
                        network_attempted=True,
                        error=str(exc),
                    )
                )
                if not request.continue_on_error:
                    break

        persisted_count = persist_raw_records(all_records) if request.persist_raw else 0
        pipeline_result = None
        if request.run_pipeline and all_records:
            source_candidates = [
                {
                    "source_name": source_request.source_name,
                    "source_url": source_request.source_url,
                    "source_type": source_request.source_type,
                    "legal_basis": source_request.legal_basis,
                    "rate_limit_per_minute": (source_request.model_extra or {}).get("rate_limit_per_minute", 1),
                    "robots_allowed": (source_request.model_extra or {}).get("robots_allowed", True),
                    "terms_allow_security_research": (source_request.model_extra or {}).get("terms_allow_security_research", True),
                    "requires_login": (source_request.model_extra or {}).get("requires_login", False),
                    "has_written_authorization": (source_request.model_extra or {}).get("has_written_authorization", False),
                }
                for source_request in source_requests
            ]
            result = get_phase_engine().run(all_records, source_candidates=source_candidates)
            if request.persist_raw:
                persist_advanced_result(all_records, result, persist_raw_items=False)
            pipeline_result = result.model_dump()

        append_audit_if_available(
            {
                "event_id": str(uuid4()),
                "event_type": "batch_source_collect_completed",
                "actor": "source_collect_api",
                "target_id": "batch",
                "payload": {
                    "source_count": len(source_requests),
                    "succeeded_count": succeeded_count,
                    "failed_count": failed_count,
                    "fetched_count": len(all_records),
                    "persisted_count": persisted_count,
                    "run_pipeline": request.run_pipeline,
                },
            }
        )
        return SourceBatchCollectResponse(
            status="completed" if failed_count == 0 else "partial_failure",
            source_count=len(source_requests),
            succeeded_count=succeeded_count,
            failed_count=failed_count,
            fetched_count=len(all_records),
            persisted_count=persisted_count,
            results=results,
            pipeline_result=pipeline_result,
        )

    @app.post(f"{settings.api.prefix}/enforcement/execute", response_model=EnforcementExecuteResponse)
    async def execute_enforcement(request: EnforcementExecuteRequest) -> EnforcementExecuteResponse:
        from src.backend import EnforcementGateway, EnforcementPolicy, policy_with_request

        policy = policy_with_request(
            EnforcementPolicy.from_mapping(settings.enforcement),
            request_safety_token=request.production_safety_token,
            force_dry_run=request.dry_run,
        )
        action_payloads: list[dict[str, Any]] = []
        for action in request.actions:
            payload = dict(action)
            if request.approved:
                payload["human_approved"] = True
            if request.approval_id and not payload.get("approval_id"):
                payload["approval_id"] = request.approval_id
            action_payloads.append(payload)
        results = EnforcementGateway(policy).execute(action_payloads)
        result_payloads = [result.model_dump() for result in results]

        for result in result_payloads:
            append_audit_if_available(
                {
                    "event_id": str(uuid4()),
                    "event_type": "enforcement_decision",
                    "actor": "enforcement_gateway",
                    "target_id": result["action"].get("target_value"),
                    "payload": result,
                }
            )
        return EnforcementExecuteResponse(status="ok", result_count=len(result_payloads), results=result_payloads)

    @app.post(f"{settings.api.prefix}/pipeline/run", response_model=PipelineRunResponse)
    async def run_pipeline(request: PipelineRunRequest) -> PipelineRunResponse:
        items = _materialize_items(request)
        if not items:
            raise HTTPException(status_code=400, detail="No input items were provided.")

        payload = {
            "items": items,
            "source_type": request.source_type,
            "source_name": request.source_name,
            "source_url": request.source_url,
            "legal_basis": request.legal_basis or settings.pipeline.default_legal_basis,
            "dry_run": request.dry_run,
            "config": settings.model_dump(mode="json"),
        }

        orchestrator, reason = get_orchestrator()
        if orchestrator is None:
            return _stub_pipeline_response(items, settings, reason)

        result = await _invoke_orchestrator(orchestrator, payload)
        return _normalize_pipeline_result(result, len(items))

    @app.get(f"{settings.api.prefix}/review/tasks", response_model=ReviewTasksResponse)
    async def review_tasks(status: str | None = "PENDING") -> ReviewTasksResponse:
        status_filter = _normalize_status_filter(status)
        orchestrator, reason = get_orchestrator()
        if orchestrator is not None:
            for method_name in ("list_review_tasks", "get_review_tasks", "review_tasks"):
                method = getattr(orchestrator, method_name, None)
                if callable(method):
                    try:
                        result = await _maybe_await(method(status=status_filter))
                    except TypeError:
                        result = await _maybe_await(method())
                    if isinstance(result, dict) and "tasks" in result:
                        result = result["tasks"]
                    if isinstance(result, list):
                        return ReviewTasksResponse(status="ok", count=len(result), tasks=[_coerce_task(task) for task in result], source="orchestrator")
        tasks, source = await _load_review_tasks(settings, status=status_filter)
        if source == "api_stub" and reason:
            source = reason
        return ReviewTasksResponse(status="ok", count=len(tasks), tasks=tasks, source=source)

    @app.post(f"{settings.api.prefix}/review/tasks/{{hypothesis_id}}/decision", response_model=ReviewDecisionResponse)
    async def review_decision(hypothesis_id: str, request: ReviewDecisionRequest) -> ReviewDecisionResponse:
        orchestrator, reason = get_orchestrator()
        if orchestrator is None or not hasattr(orchestrator, "record_review_decision"):
            raise HTTPException(status_code=501, detail=reason or "review decision workflow is not wired")
        try:
            result = await _maybe_await(
                orchestrator.record_review_decision(
                    hypothesis_id,
                    decision=request.decision,
                    reviewer=request.reviewer,
                    notes=request.notes,
                    edited_risk_type=request.edited_risk_type,
                    secondary_label=request.secondary_label,
                    corrected_entities=request.corrected_entities,
                    add_to_wordlist=request.add_to_wordlist,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

        review_state = _coerce_task(result.get("review_state", {}))
        audit_event = _coerce_task(result.get("audit_event", {}))
        decision = str(review_state.get("decision") or request.decision).upper()
        return ReviewDecisionResponse(
            status="ok",
            hypothesis_id=hypothesis_id,
            decision=decision,
            review_state=review_state,
            audit_event=audit_event,
        )

    @app.get(f"{settings.api.prefix}/review/audit", response_model=AuditEventsResponse)
    async def review_audit(event_type: str | None = None) -> AuditEventsResponse:
        orchestrator, reason = get_orchestrator()
        if orchestrator is None or not hasattr(orchestrator, "list_audit_events"):
            raise HTTPException(status_code=501, detail=reason or "audit repository is not wired")
        events = await _maybe_await(orchestrator.list_audit_events(event_type=event_type))
        return AuditEventsResponse(status="ok", count=len(events), events=[_coerce_task(event) for event in events])

    @app.post(f"{settings.api.prefix}/pipeline/advanced/run", response_model=AdvancedPipelineResponse)
    async def run_advanced_pipeline(request: PipelineRunRequest) -> AdvancedPipelineResponse:
        items = _materialize_items(request)
        if not items:
            raise HTTPException(status_code=400, detail="No input items were provided.")
        extras = request.model_extra or {}
        result = get_phase_engine().run(
            items,
            prompt_text=extras.get("prompt_text"),
            source_candidates=extras.get("source_candidates") or (),
        )
        persist_advanced_result(items, result)
        return AdvancedPipelineResponse(**result.model_dump())

    @app.post(f"{settings.api.prefix}/tasks/pipeline/advanced", response_model=TaskSubmitResponse)
    async def submit_advanced_pipeline_task(request: PipelineRunRequest) -> TaskSubmitResponse:
        items = _materialize_items(request)
        if not items:
            raise HTTPException(status_code=400, detail="No input items were provided.")
        extras = request.model_extra or {}

        def run_task(payload: dict[str, Any]) -> dict[str, Any]:
            result = get_phase_engine().run(
                payload["items"],
                prompt_text=payload.get("prompt_text"),
                source_candidates=payload.get("source_candidates") or (),
            )
            persist_advanced_result(payload["items"], result)
            return result.model_dump()

        task = get_task_backend().submit(
            "advanced_pipeline",
            {
                "items": items,
                "prompt_text": extras.get("prompt_text"),
                "source_candidates": extras.get("source_candidates") or (),
            },
            handler=run_task,
            metadata={"api": f"{settings.api.prefix}/tasks/pipeline/advanced"},
        )
        persist_task(task)
        return TaskSubmitResponse(status="accepted", task_id=task.task_id, task_status=task.status.value)

    @app.post(f"{settings.api.prefix}/tasks/run-pending", response_model=TaskRunPendingResponse)
    async def run_pending_tasks(limit: int | None = None) -> TaskRunPendingResponse:
        records = get_task_backend().run_pending(limit=limit)
        for record in records:
            persist_task(record)
        return TaskRunPendingResponse(status="ok", count=len(records), tasks=[_coerce_task(record) for record in records])

    @app.get(f"{settings.api.prefix}/tasks/{{task_id}}")
    async def get_task(task_id: str) -> dict[str, Any]:
        record = get_task_backend().get(task_id)
        if record is None:
            sql_backend = get_sql_backend()
            persisted = sql_backend.get_task(task_id) if sql_backend is not None else None
            if persisted is None:
                raise HTTPException(status_code=404, detail=f"unknown task_id: {task_id}")
            return {"status": "ok", "task": persisted, "source": "sql"}
        return {"status": "ok", "task": _coerce_task(record), "source": "local"}

    @app.post(f"{settings.api.prefix}/llm/chat", response_model=LLMChatResponse)
    async def llm_chat(request: LLMChatRequest) -> LLMChatResponse:
        response = get_llm_gateway().chat(
            request.messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=request.response_format,
        )
        return LLMChatResponse(**response.model_dump())

    @app.get(f"{settings.api.prefix}/semantic/search")
    async def semantic_search(query: str, top_k: int = 5) -> dict[str, Any]:
        engine = get_phase_engine()
        results = engine.semantic_search(query, top_k=top_k)
        return {"status": "ok", "count": len(results), "results": results}

    return app


def _raw_payload_for_storage(item: dict[str, Any]) -> dict[str, Any]:
    content_text = str(item.get("content_text") or item.get("text") or item.get("raw_text") or item.get("content") or "")
    hash_id = str(item.get("hash_id") or sha256(content_text.encode("utf-8")).hexdigest())
    return {
        **item,
        "hash_id": hash_id,
        "trace_id": str(item.get("trace_id") or item.get("source_trace_id") or uuid4()),
        "source_type": str(item.get("source_type") or "Manual"),
        "source_name": str(item.get("source_name") or "api_request"),
        "legal_basis": str(item.get("legal_basis") or "PUBLIC_COMPLIANT_DATA"),
        "content_text": content_text,
    }


def _safe_dsn(dsn: str | None) -> str | None:
    if not dsn:
        return None
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" in rest and ":" in rest.split("@", 1)[0]:
        user_info, host = rest.split("@", 1)
        user = user_info.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return dsn


app = create_app()

