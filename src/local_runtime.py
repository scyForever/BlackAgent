"""In-process BlackAgent runtime with no public HTTP API surface.

The agent now runs through Python calls, CLI scripts, and local task helpers.
This module keeps the former service wiring reusable without exposing web
routes or requiring an ASGI server.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from hashlib import sha256
from typing import Any, Iterable, Mapping
from uuid import uuid4

from src.config_loader import PROJECT_ROOT, Settings, get_settings, resolve_project_path
from src.collector.source_metadata import classify_collection_failure
from src.infra import RuntimeContainer
from storage import InMemoryClueRepo, connect


def make_llm_gateway(settings: Settings) -> Any:
    """Build the configured OpenAI-compatible LLM gateway."""

    from src.backend import LLMGateway

    return LLMGateway(
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key,
        model=settings.llm.model,
        service_tier=settings.llm.service_tier,
        dry_run=(settings.llm.dry_run or not settings.llm.enabled),
        mock=settings.llm.provider.lower() == "mock",
        timeout_seconds=settings.llm.timeout_seconds,
        auth_header=settings.llm.auth_header,
        max_tokens_param=settings.llm.max_tokens_param,
        response_format_supported=settings.llm.response_format_supported,
        extra_body=settings.llm.extra_body,
    )


def read_project_fixture_items(fixture_path: str) -> list[dict[str, Any]]:
    """Read project-local JSON or JSONL fixture data."""

    path = resolve_project_path(fixture_path)
    if PROJECT_ROOT not in path.parents and path != PROJECT_ROOT:
        raise ValueError("fixture_path must stay inside the project directory")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"fixture_path not found: {fixture_path}")

    import json

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    if path.suffix.lower() == ".jsonl":
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        loaded = json.loads(text)
        items = loaded if isinstance(loaded, list) else [loaded]
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("fixture data must contain JSON objects")
    return [dict(item) for item in items]


def load_source_requests(
    *,
    source_config_path: str | None = None,
    sources: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Load source definitions from a catalog and/or inline source records."""

    raw_sources: list[dict[str, Any]] = []
    if source_config_path:
        config_path = resolve_project_path(source_config_path)
        if PROJECT_ROOT not in config_path.parents and config_path != PROJECT_ROOT:
            raise ValueError("source_config_path must stay inside the project directory")
        from src.collector import load_source_catalog

        raw_sources.extend(load_source_catalog(config_path))
    raw_sources.extend(dict(source) for source in sources)
    return [_normalize_source_request(source) for source in raw_sources]


def collect_source_records(settings: Settings, source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fetch one authorized HTTP(S) feed through the internal collector."""

    from src.collector import HTTPFeedCollector, HTTPFeedConfig
    from src.collector.base_collector import model_dump as dump_record

    request = _normalize_source_request(source)
    max_records = min(
        int(request.get("max_records") or settings.network.max_records_per_fetch),
        settings.network.max_records_per_fetch,
    )
    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url=str(request["source_url"]),
            source_name=str(request["source_name"]),
            source_type=str(request.get("source_type") or "THREAT_INTEL"),
            platform=str(request.get("platform") or ""),
            legal_basis=str(request.get("legal_basis") or "PUBLIC_COMPLIANT_DATA"),
            feed_format=str(request.get("feed_format") or "auto"),
            max_records=max_records,
            timeout_seconds=settings.network.timeout_seconds,
            user_agent=settings.network.user_agent,
            allowed_domains=tuple(request.get("allowed_domains") or settings.network.allowed_domains),
            headers=dict(request.get("headers") or {}),
            include_keywords=tuple(request.get("include_keywords") or ()),
            exclude_keywords=tuple(request.get("exclude_keywords") or ()),
            include_themes=tuple(request.get("include_themes") or ()),
            exclude_themes=tuple(request.get("exclude_themes") or ()),
            search_query=request.get("search_query"),
            query_theme=request.get("query_theme"),
            query_term=request.get("query_term"),
            query_term_stage=request.get("query_term_stage"),
            query_variant_index=request.get("query_variant_index"),
            min_keyword_hits=int(request.get("min_keyword_hits") or 1),
            rate_limit_per_minute=(
                int(request["rate_limit_per_minute"])
                if request.get("rate_limit_per_minute") is not None
                else settings.network.rate_limit_per_minute
            ),
            retry_attempts=(
                int(request["retry_attempts"])
                if request.get("retry_attempts") is not None
                else settings.network.retry_attempts
            ),
            retry_backoff_seconds=(
                float(request["retry_backoff_seconds"])
                if request.get("retry_backoff_seconds") is not None
                else settings.network.retry_backoff_seconds
            ),
            retry_backoff_multiplier=(
                float(request["retry_backoff_multiplier"])
                if request.get("retry_backoff_multiplier") is not None
                else settings.network.retry_backoff_multiplier
            ),
            retry_statuses=tuple(request.get("retry_statuses") or settings.network.retry_statuses),
            source_access_type=request.get("source_access_type"),
            text_fields=tuple(request.get("text_fields") or HTTPFeedConfig.text_fields),
            network_enabled=settings.network.enabled,
        )
    )
    return [dump_record(record) for record in collector.collect()]


class LocalAgentRuntime:
    """Reusable in-process runtime for CLI scripts and tests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.container = RuntimeContainer(self.settings)
        self.phase_engine: Any | None = None
        self.investigation_orchestrator: Any | None = None
        self.clue_repo = self.container.clue_repo
        self.offline_clue_builder: Any | None = None
        self.task_backend: Any | None = None
        self.llm_gateway: Any | None = None
        self.sql_backend: Any | None = None
        self.sql_backend_initialized = False
        self.scheduler_backend: Any | None = None
        self.scheduler_backend_initialized = False
        self.collection_scheduler: Any | None = None

    def close(self) -> None:
        self.container.close()
        for attr in ("sql_backend", "scheduler_backend"):
            backend = getattr(self, attr, None)
            if backend is not None and hasattr(backend, "close"):
                backend.close()
            setattr(self, attr, None)
        self.sql_backend_initialized = False
        self.scheduler_backend_initialized = False

    def backend_status(self) -> dict[str, Any]:
        storage_connected = self.get_sql_backend() is not None
        return {
            "status": "ok",
            "storage_backend": self.settings.storage.backend,
            "storage_connected": storage_connected,
            "storage_dsn": _safe_dsn(self.settings.storage.dsn),
            "task_backend": self.settings.tasks.backend,
            "network_enabled": self.settings.network.enabled,
            "network_allowed_domains": list(self.settings.network.allowed_domains),
            "llm_provider": self.settings.llm.provider,
            "llm_enabled": self.settings.llm.enabled,
            "llm_dry_run": self.settings.llm.dry_run or not self.settings.llm.enabled,
            "enforcement_enabled": self.settings.enforcement.enabled,
            "enforcement_dry_run": self.settings.enforcement.dry_run,
            "enforcement_connector": self.settings.enforcement.connector,
        }

    def get_phase_engine(self) -> Any:
        if self.phase_engine is None:
            self.phase_engine = self.container.phase_engine()
        return self.phase_engine

    def get_llm_gateway(self) -> Any:
        if self.llm_gateway is None:
            self.llm_gateway = self.container.llm_gateway()
        return self.llm_gateway

    def get_investigation_orchestrator(self) -> Any:
        if self.investigation_orchestrator is None:
            self.investigation_orchestrator = self.container.investigation_orchestrator()
        return self.investigation_orchestrator

    def get_offline_clue_builder(self) -> Any:
        if self.offline_clue_builder is None:
            self.offline_clue_builder = self.container.offline_clue_builder()
        return self.offline_clue_builder

    def get_task_backend(self) -> Any:
        if self.task_backend is None:
            self.task_backend = self.container.task_backend()
        return self.task_backend

    def get_sql_backend(self) -> Any | None:
        if self.sql_backend_initialized:
            return self.sql_backend
        self.sql_backend_initialized = True
        if not self.settings.storage.dsn or self.settings.storage.backend.lower() not in {
            "sql",
            "sqlite",
            "postgres",
            "postgresql",
        }:
            self.sql_backend = None
            return None
        backend = connect(self.settings.storage.dsn)
        if self.settings.storage.auto_create_schema:
            backend.create_schema()
        self.sql_backend = backend
        return backend

    def get_scheduler_backend(self) -> Any:
        if self.scheduler_backend_initialized:
            return self.scheduler_backend
        self.scheduler_backend_initialized = True
        backend = connect(self._scheduler_dsn())
        backend.create_schema()
        self.scheduler_backend = backend
        return backend

    def get_collection_scheduler(self) -> Any:
        if self.collection_scheduler is None:
            from src.scheduling import CollectionQueueScheduler

            self.collection_scheduler = CollectionQueueScheduler(
                self.get_scheduler_backend(),
                start_immediately=self.settings.scheduler.start_immediately,
                default_worker_count=self.settings.scheduler.worker_count,
                claim_limit_per_worker=self.settings.scheduler.claim_limit_per_worker,
                max_claim_rounds=self.settings.scheduler.max_claim_rounds,
                retry_backoff_seconds=self.settings.scheduler.retry_backoff_seconds,
                clue_batch_limit=self.settings.scheduler.clue_batch_limit,
            )
        return self.collection_scheduler

    def collect_source(
        self,
        source: Mapping[str, Any],
        *,
        persist_raw: bool = True,
        run_pipeline: bool = False,
    ) -> dict[str, Any]:
        request = _normalize_source_request(source)
        records = collect_source_records(self.settings, request)
        persisted_count = self.persist_raw_records(records) if persist_raw else 0
        pipeline_result = None
        if run_pipeline:
            result = self.get_phase_engine().run(records)
            if persist_raw:
                self.persist_advanced_result(records, result, persist_raw_items=False)
            pipeline_result = result.model_dump()
        self.append_audit_if_available(
            {
                "event_id": str(uuid4()),
                "event_type": "source_collect_completed",
                "actor": "source_collect_local",
                "target_id": request["source_name"],
                "payload": {
                    "source_name": request["source_name"],
                    "source_url": request["source_url"],
                    "fetched_count": len(records),
                    "persisted_count": persisted_count,
                    "run_pipeline": run_pipeline,
                },
            }
        )
        return {
            "status": "completed",
            "source_name": request["source_name"],
            "fetched_count": len(records),
            "persisted_count": persisted_count,
            "network_attempted": True,
            "raw_records": records,
            "pipeline_result": pipeline_result,
        }

    def collect_sources_batch(
        self,
        *,
        source_config_path: str | None = None,
        sources: Iterable[Mapping[str, Any]] = (),
        persist_raw: bool = True,
        run_pipeline: bool = False,
        continue_on_error: bool = False,
    ) -> dict[str, Any]:
        source_requests = load_source_requests(source_config_path=source_config_path, sources=sources)
        results: list[dict[str, Any]] = []
        all_records: list[dict[str, Any]] = []
        succeeded_count = 0
        failed_count = 0
        for source_request in source_requests:
            try:
                records = collect_source_records(self.settings, source_request)
                all_records.extend(records)
                results.append(
                    {
                        "source_name": source_request["source_name"],
                        "source_url": source_request["source_url"],
                        "source_type": source_request.get("source_type") or "THREAT_INTEL",
                        "source_access_type": source_request.get("source_access_type"),
                        "source_class": source_request.get("source_class"),
                        "fetched_count": len(records),
                        "network_attempted": True,
                        "raw_records": records,
                        "error": None,
                        "failure_reason": None,
                    }
                )
                succeeded_count += 1
            except Exception as exc:  # noqa: BLE001 - batch mode reports per-source failures.
                failure_reason = classify_collection_failure(exc)
                failed_count += 1
                results.append(
                    {
                        "source_name": source_request.get("source_name"),
                        "source_url": source_request.get("source_url"),
                        "source_type": source_request.get("source_type") or "THREAT_INTEL",
                        "source_access_type": source_request.get("source_access_type"),
                        "source_class": source_request.get("source_class"),
                        "fetched_count": 0,
                        "network_attempted": True,
                        "raw_records": [],
                        "error": str(exc),
                        "failure_reason": failure_reason,
                    }
                )
                if not continue_on_error:
                    break

        persisted_count = self.persist_raw_records(all_records) if persist_raw else 0
        pipeline_result = None
        if run_pipeline and all_records:
            source_candidates = [_source_candidate(source_request) for source_request in source_requests]
            result = self.get_phase_engine().run(all_records, source_candidates=source_candidates)
            if persist_raw:
                self.persist_advanced_result(all_records, result, persist_raw_items=False)
            pipeline_result = result.model_dump()
        self.append_audit_if_available(
            {
                "event_id": str(uuid4()),
                "event_type": "batch_source_collect_completed",
                "actor": "source_collect_local",
                "target_id": "batch",
                "payload": {
                    "source_count": len(source_requests),
                    "succeeded_count": succeeded_count,
                    "failed_count": failed_count,
                    "fetched_count": len(all_records),
                    "persisted_count": persisted_count,
                    "run_pipeline": run_pipeline,
                },
            }
        )
        return {
            "status": "completed" if failed_count == 0 else "partial_failure",
            "source_count": len(source_requests),
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "fetched_count": len(all_records),
            "persisted_count": persisted_count,
            "results": results,
            "pipeline_result": pipeline_result,
        }

    def run_advanced_pipeline(
        self,
        items: Iterable[Mapping[str, Any]],
        *,
        prompt_text: str | None = None,
        source_candidates: Iterable[Mapping[str, Any]] = (),
        persist: bool = True,
    ) -> dict[str, Any]:
        records = [dict(item) for item in items]
        if not records:
            raise ValueError("No input items were provided.")
        result = self.get_phase_engine().run(records, prompt_text=prompt_text, source_candidates=source_candidates)
        if persist:
            self.persist_advanced_result(records, result)
        return result.model_dump()

    def run_investigation(
        self,
        query: str,
        *,
        fixture_items: Iterable[Mapping[str, Any]] = (),
        fixture_path: str | None = None,
        source_config_path: str | None = None,
        sources: Iterable[Mapping[str, Any]] = (),
        max_sources: int | None = None,
        time_range_hours: int | None = None,
        source_types: Iterable[str] = (),
        risk_types: Iterable[str] = (),
        min_quality_score: float | None = None,
        routing_profile: str | None = None,
        policy_override: Any | None = None,
    ) -> dict[str, Any]:
        available_sources = load_source_requests(source_config_path=source_config_path, sources=sources)
        records = [dict(item) for item in fixture_items]
        if fixture_path:
            records.extend(read_project_fixture_items(fixture_path))

        def collect_for_investigation(source_payload: dict[str, Any]) -> list[dict[str, Any]]:
            return collect_source_records(self.settings, source_payload)

        result = self.container.investigation_service().run(
            query,
            records=records,
            available_sources=available_sources,
            collect_source_records=(collect_for_investigation if available_sources else None),
            max_sources=max_sources,
            retrieval_filters={
                "time_range_hours": time_range_hours,
                "source_types": list(source_types),
                "risk_types": list(risk_types),
                "min_quality_score": min_quality_score,
            },
            max_concurrent_sources=self.settings.network.max_concurrent_sources,
            routing_profile=routing_profile,
            policy_override=policy_override,
        )
        for clue in [*result.high_quality_clues, *result.candidate_clues]:
            self.clue_repo.save(clue)
        return result.model_dump()

    def build_clues(
        self,
        items: Iterable[Mapping[str, Any]],
        *,
        fixture_path: str | None = None,
        prompt_text: str | None = None,
        source_candidates: Iterable[Mapping[str, Any]] = (),
        quality_profile: str = "balanced",
        require_cross_source: bool = False,
        require_evidence_chain: bool = True,
        persist_sql: bool = True,
    ) -> dict[str, Any]:
        records = [dict(item) for item in items]
        if fixture_path:
            records.extend(read_project_fixture_items(fixture_path))
        if not records:
            raise ValueError("Provide fixture_items or fixture_path")
        result = self.get_offline_clue_builder().build(
            records,
            prompt_text=prompt_text,
            source_candidates=source_candidates,
            quality_profile=quality_profile,
            require_cross_source=require_cross_source,
            require_evidence_chain=require_evidence_chain,
        )
        if persist_sql:
            sql_backend = self.get_sql_backend()
            if sql_backend is not None:
                for clue in result.clues:
                    sql_backend.save_clue(clue)
        return result.model_dump()

    def submit_advanced_pipeline_task(
        self,
        items: Iterable[Mapping[str, Any]],
        *,
        prompt_text: str | None = None,
        source_candidates: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        records = [dict(item) for item in items]
        if not records:
            raise ValueError("No input items were provided.")

        def run_task(payload: dict[str, Any]) -> dict[str, Any]:
            return self.run_advanced_pipeline(
                payload["items"],
                prompt_text=payload.get("prompt_text"),
                source_candidates=payload.get("source_candidates") or (),
            )

        task = self.get_task_backend().submit(
            "advanced_pipeline",
            {"items": records, "prompt_text": prompt_text, "source_candidates": list(source_candidates)},
            handler=run_task,
            metadata={"entrypoint": "local_runtime.submit_advanced_pipeline_task"},
        )
        self.persist_task(task)
        return {"status": "accepted", "task_id": task.task_id, "task_status": task.status.value}

    def submit_build_clues_task(
        self,
        items: Iterable[Mapping[str, Any]],
        *,
        prompt_text: str | None = None,
        source_candidates: Iterable[Mapping[str, Any]] = (),
        quality_profile: str = "balanced",
        require_cross_source: bool = False,
        require_evidence_chain: bool = True,
    ) -> dict[str, Any]:
        records = [dict(item) for item in items]

        def run_task(payload: dict[str, Any]) -> dict[str, Any]:
            return self.build_clues(
                payload["items"],
                prompt_text=payload.get("prompt_text"),
                source_candidates=payload.get("source_candidates") or (),
                quality_profile=payload.get("quality_profile") or "balanced",
                require_cross_source=bool(payload.get("require_cross_source", False)),
                require_evidence_chain=bool(payload.get("require_evidence_chain", True)),
            )

        task = self.get_task_backend().submit(
            "offline_clue_build",
            {
                "items": records,
                "prompt_text": prompt_text,
                "source_candidates": list(source_candidates),
                "quality_profile": quality_profile,
                "require_cross_source": require_cross_source,
                "require_evidence_chain": require_evidence_chain,
            },
            handler=run_task,
            metadata={"entrypoint": "local_runtime.submit_build_clues_task"},
        )
        self.persist_task(task)
        return {"status": "accepted", "task_id": task.task_id, "task_status": task.status.value}

    def run_pending_tasks(self, limit: int | None = None) -> dict[str, Any]:
        records = self.get_task_backend().run_pending(limit=limit)
        for record in records:
            self.persist_task(record)
        return {"status": "ok", "count": len(records), "tasks": [_coerce_task(record) for record in records]}

    def get_task(self, task_id: str) -> dict[str, Any]:
        record = self.get_task_backend().get(task_id)
        if record is not None:
            return {"status": "ok", "task": _coerce_task(record), "source": "local"}
        sql_backend = self.get_sql_backend()
        persisted = sql_backend.get_task(task_id) if sql_backend is not None else None
        if persisted is None:
            raise KeyError(f"unknown task_id: {task_id}")
        return {"status": "ok", "task": persisted, "source": "sql"}

    def llm_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not messages:
            raise ValueError("messages must not be empty")
        response = self.get_llm_gateway().chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        return response.model_dump()

    def execute_enforcement(
        self,
        actions: Iterable[Mapping[str, Any]],
        *,
        approved: bool = False,
        approval_id: str | None = None,
        dry_run: bool | None = None,
        production_safety_token: str | None = None,
    ) -> dict[str, Any]:
        from src.backend import EnforcementGateway, EnforcementPolicy, policy_with_request

        action_payloads = []
        for action in actions:
            payload = dict(action)
            if approved:
                payload["human_approved"] = True
            if approval_id and not payload.get("approval_id"):
                payload["approval_id"] = approval_id
            action_payloads.append(payload)
        if not action_payloads:
            raise ValueError("actions must not be empty")
        policy = policy_with_request(
            EnforcementPolicy.from_mapping(self.settings.enforcement),
            request_safety_token=production_safety_token,
            force_dry_run=dry_run,
        )
        result_payloads = [result.model_dump() for result in EnforcementGateway(policy).execute(action_payloads)]
        for result in result_payloads:
            self.append_audit_if_available(
                {
                    "event_id": str(uuid4()),
                    "event_type": "enforcement_decision",
                    "actor": "enforcement_gateway",
                    "target_id": result["action"].get("target_value"),
                    "payload": result,
                }
            )
        return {"status": "ok", "result_count": len(result_payloads), "results": result_payloads}

    def semantic_search(self, query: str, top_k: int = 5) -> dict[str, Any]:
        results = self.get_phase_engine().semantic_search(query, top_k=top_k)
        return {"status": "ok", "count": len(results), "results": results}

    def scheduler_bootstrap(self) -> dict[str, Any]:
        scheduler = self.get_collection_scheduler()
        schedules = scheduler.sync_schedules(self._default_schedules())
        return {"status": "ok", "schedule_count": len(schedules), "schedules": schedules}

    def scheduler_status(self) -> dict[str, Any]:
        return {"status": "ok", **self.get_collection_scheduler().status().model_dump()}

    def scheduler_tick(self) -> dict[str, Any]:
        return self.get_collection_scheduler().tick()

    def scheduler_run_workers(
        self,
        *,
        worker_count: int = 0,
        claim_limit: int = 0,
        max_rounds: int = 0,
        layers: Iterable[str] = (),
    ) -> dict[str, Any]:
        return self.get_collection_scheduler().run_workers(
            worker_count=worker_count or self.settings.scheduler.worker_count,
            claim_limit=claim_limit or self.settings.scheduler.claim_limit_per_worker,
            max_rounds=max_rounds or self.settings.scheduler.max_claim_rounds,
            layers=list(layers),
        )

    def scheduler_cycle(
        self,
        *,
        worker_count: int = 0,
        claim_limit: int = 0,
        max_rounds: int = 0,
        layers: Iterable[str] = (),
    ) -> dict[str, Any]:
        scheduler = self.get_collection_scheduler()
        if not scheduler.status().schedule_count:
            scheduler.sync_schedules(self._default_schedules())
        tick = scheduler.tick()
        workers = self.scheduler_run_workers(
            worker_count=worker_count,
            claim_limit=claim_limit,
            max_rounds=max_rounds,
            layers=layers,
        )
        return {"status": "ok", "tick": tick, "workers": workers, "scheduler": scheduler.status().model_dump()}

    def persist_task(self, record: Any) -> None:
        sql_backend = self.get_sql_backend()
        if sql_backend is not None:
            payload = _coerce_task(record)
            sql_backend.save_task(payload, task_type=payload.get("name"), status=payload.get("status"))

    def persist_advanced_result(
        self,
        items: list[dict[str, Any]],
        result: Any,
        *,
        persist_raw_items: bool = True,
    ) -> None:
        if persist_raw_items:
            sql_backend = self.get_sql_backend()
            if sql_backend is not None:
                for item in items:
                    sql_backend.save_raw(_raw_payload_for_storage(item))
        result_payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        for clue in result_payload.get("risk_clues", []):
            self.clue_repo.save(clue)
        sql_backend = self.get_sql_backend()
        if sql_backend is None:
            return
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
        for clue in result_payload.get("risk_clues", []):
            sql_backend.save_clue(clue)

    def persist_raw_records(self, records: list[dict[str, Any]]) -> int:
        sql_backend = self.get_sql_backend()
        if sql_backend is None:
            return 0
        for record in records:
            sql_backend.save_raw(record)
        return len(records)

    def append_audit_if_available(self, event: dict[str, Any]) -> None:
        sql_backend = self.get_sql_backend()
        if sql_backend is not None:
            sql_backend.append_audit(event)

    def _scheduler_dsn(self) -> str:
        if self.settings.scheduler.dsn:
            return self.settings.scheduler.dsn
        if self.settings.storage.dsn and self.settings.storage.backend.lower() in {"sql", "sqlite"}:
            return self.settings.storage.dsn
        fallback = resolve_project_path(self.settings.scheduler.default_db_path)
        return f"sqlite:///{fallback.as_posix()}"

    def _default_schedules(self) -> list[dict[str, Any]]:
        return self.get_collection_scheduler().default_schedules(
            public_catalog="config/intel_sources.blackgray.yaml",
            x_config="config/x_watch.example.yaml",
            telegram_config="config/telegram_watch.example.yaml",
            fast_interval_seconds=self.settings.scheduler.fast_interval_seconds,
            slow_interval_seconds=self.settings.scheduler.slow_interval_seconds,
            clue_build_interval_seconds=self.settings.scheduler.clue_build_interval_seconds,
            lease_seconds=self.settings.scheduler.lease_seconds,
            max_attempts=self.settings.scheduler.max_attempts,
            cron_overrides=self.settings.scheduler.cron_overrides,
        )


def _normalize_source_request(source: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(source)
    if not payload.get("source_url"):
        raise ValueError("source_url is required")
    payload.setdefault("source_name", "local-source")
    payload.setdefault("source_type", "THREAT_INTEL")
    payload.setdefault("legal_basis", "PUBLIC_COMPLIANT_DATA")
    payload.setdefault("feed_format", "auto")
    payload.setdefault("headers", {})
    payload.setdefault("allowed_domains", [])
    payload.setdefault("text_fields", [])
    payload.setdefault("source_access_type", None)
    payload.setdefault("source_class", None)
    payload.setdefault("include_keywords", [])
    payload.setdefault("exclude_keywords", [])
    payload.setdefault("include_themes", [])
    payload.setdefault("exclude_themes", [])
    payload.setdefault("min_keyword_hits", 1)
    return payload


def _source_candidate(source_request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_name": source_request.get("source_name"),
        "source_url": source_request.get("source_url"),
        "source_type": source_request.get("source_type"),
        "legal_basis": source_request.get("legal_basis"),
        "rate_limit_per_minute": source_request.get("rate_limit_per_minute", 1),
        "robots_allowed": source_request.get("robots_allowed", True),
        "terms_allow_security_research": source_request.get("terms_allow_security_research", True),
        "requires_login": source_request.get("requires_login", False),
        "has_written_authorization": source_request.get("has_written_authorization", False),
    }


def _coerce_task(task: Any) -> dict[str, Any]:
    if hasattr(task, "model_dump"):
        return task.model_dump()
    if is_dataclass(task):
        return asdict(task)
    if isinstance(task, dict):
        return task
    return {"value": task}


def _raw_payload_for_storage(item: dict[str, Any]) -> dict[str, Any]:
    content_text = str(item.get("content_text") or item.get("text") or item.get("raw_text") or item.get("content") or "")
    hash_id = str(item.get("hash_id") or sha256(content_text.encode("utf-8")).hexdigest())
    return {
        **item,
        "hash_id": hash_id,
        "trace_id": str(item.get("trace_id") or item.get("source_trace_id") or uuid4()),
        "source_type": str(item.get("source_type") or "Manual"),
        "source_name": str(item.get("source_name") or "local_runtime"),
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


__all__ = [
    "LocalAgentRuntime",
    "collect_source_records",
    "load_source_requests",
    "make_llm_gateway",
    "read_project_fixture_items",
]
