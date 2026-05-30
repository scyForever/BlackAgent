"""Dependency container for local runtime wiring."""

from __future__ import annotations

from typing import Any

from src.agent import InvestigationOrchestrator
from src.application import InvestigationService, ReportService, ReviewService, TaskService
from src.backend import LLMGateway, TaskBackend
from src.config_loader import Settings
from src.enhancement.engine import PhaseTwoThreeEngine
from src.pipeline import OfflineClueBuilder
from storage import InMemoryClueRepo, connect


class RuntimeContainer:
    """Lazy dependency container used by CLI and in-process runtime code."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.clue_repo = InMemoryClueRepo()
        self._phase_engine: Any | None = None
        self._llm_gateway: Any | None = None
        self._orchestrator: Any | None = None
        self._offline_clue_builder: Any | None = None
        self._task_backend: Any | None = None
        self._sql_backend: Any | None = None
        self._sql_backend_initialized = False
        self._investigation_service: InvestigationService | None = None
        self._task_service: TaskService | None = None
        self._review_service: ReviewService | None = None
        self._report_service: ReportService | None = None

    def phase_engine(self) -> PhaseTwoThreeEngine:
        if self._phase_engine is None:
            self._phase_engine = PhaseTwoThreeEngine()
        return self._phase_engine

    def llm_gateway(self) -> LLMGateway:
        if self._llm_gateway is None:
            self._llm_gateway = LLMGateway(
                base_url=self.settings.llm.base_url,
                api_key=self.settings.llm.api_key,
                model=self.settings.llm.model,
                service_tier=self.settings.llm.service_tier,
                dry_run=(self.settings.llm.dry_run or not self.settings.llm.enabled),
                mock=self.settings.llm.provider.lower() == "mock",
                timeout_seconds=self.settings.llm.timeout_seconds,
                auth_header=self.settings.llm.auth_header,
                max_tokens_param=self.settings.llm.max_tokens_param,
                response_format_supported=self.settings.llm.response_format_supported,
                extra_body=self.settings.llm.extra_body,
            )
        return self._llm_gateway

    def investigation_orchestrator(self) -> InvestigationOrchestrator:
        if self._orchestrator is None:
            self._orchestrator = InvestigationOrchestrator(
                llm_gateway=self.llm_gateway(),
                phase_engine=self.phase_engine(),
                clue_repo=self.clue_repo,
                investigation_config=self.settings.investigation,
                routing_profiles=self.settings.routing_profiles,
            )
        return self._orchestrator

    def offline_clue_builder(self) -> OfflineClueBuilder:
        if self._offline_clue_builder is None:
            self._offline_clue_builder = OfflineClueBuilder(
                phase_engine=self.phase_engine(),
                clue_repo=self.clue_repo,
            )
        return self._offline_clue_builder

    def task_backend(self) -> TaskBackend:
        if self._task_backend is None:
            self._task_backend = TaskBackend(execution_mode="sync")
        return self._task_backend

    def sql_backend(self) -> Any | None:
        if self._sql_backend_initialized:
            return self._sql_backend
        self._sql_backend_initialized = True
        if not self.settings.storage.dsn or self.settings.storage.backend.lower() not in {
            "sql",
            "sqlite",
            "postgres",
            "postgresql",
        }:
            self._sql_backend = None
            return None
        backend = connect(self.settings.storage.dsn)
        if self.settings.storage.auto_create_schema:
            backend.create_schema()
        self._sql_backend = backend
        return backend

    def investigation_service(self) -> InvestigationService:
        if self._investigation_service is None:
            self._investigation_service = InvestigationService(self.investigation_orchestrator())
        return self._investigation_service

    def task_service(self) -> TaskService:
        if self._task_service is None:
            self._task_service = TaskService(self.task_backend())
        return self._task_service

    def review_service(self) -> ReviewService:
        if self._review_service is None:
            self._review_service = ReviewService(self.investigation_orchestrator())
        return self._review_service

    def report_service(self) -> ReportService:
        if self._report_service is None:
            self._report_service = ReportService()
        return self._report_service

    def close(self) -> None:
        if self._sql_backend is not None and hasattr(self._sql_backend, "close"):
            self._sql_backend.close()
        self._sql_backend = None
        self._sql_backend_initialized = False


__all__ = ["RuntimeContainer"]
