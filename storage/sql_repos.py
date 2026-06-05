"""Repository adapters that narrow business dependencies on SQLBackend."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4


class RawSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        payload = _normalize_mapping(record)
        payload.setdefault("hash_id", str(payload.get("trace_id") or payload.get("source_trace_id") or uuid4()))
        return self.backend.save_raw(payload)

    def list(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.backend.list_raw(limit=limit)

    def list_by_hash_ids(self, hash_ids: list[str]) -> list[dict[str, Any]]:
        return self.backend.list_raw_by_hash_ids(hash_ids)


class CleanedSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, record: Mapping[str, Any] | Any, *, commit: bool = True) -> dict[str, Any]:
        return self.backend.save_cleaned(record, commit=commit)

    def list(self, *, risk_level: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return self.backend.list_cleaned(risk_level=risk_level, limit=limit)

    def clear(self) -> None:
        self.backend.clear_cleaned()


class ReviewSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, hypothesis: Mapping[str, Any] | Any, *, state: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
        return self.backend.save_review(hypothesis, state=state, **fields)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.backend.list_review(status=status)


class AuditSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def append(self, event: Mapping[str, Any] | Any) -> dict[str, Any]:
        return self.backend.append_audit(event)

    def list(self, event_type: str | None = None) -> list[dict[str, Any]]:
        return self.backend.list_audit(event_type=event_type)


class ClueSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, clue: Mapping[str, Any] | Any) -> dict[str, Any]:
        return self.backend.save_clue(clue)

    def list(self, *, risk_category: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return self.backend.list_clues(risk_category=risk_category, limit=limit)


class EntitySQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, entity: Mapping[str, Any] | Any) -> dict[str, Any]:
        return self.backend.save_entity(entity)

    def list(self, source_trace_id: str | None = None) -> list[dict[str, Any]]:
        return self.backend.list_entities(source_trace_id=source_trace_id)


class SchedulerSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    @property
    def dsn(self) -> str:
        return str(getattr(self.backend, "dsn", ""))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)


class TaskSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, task: Mapping[str, Any] | str, **fields: Any) -> dict[str, Any]:
        return self.backend.save_task(task, **fields)

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.backend.get_task(task_id)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.backend.list_tasks(status=status)


class QueueSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, job: Mapping[str, Any] | Any) -> dict[str, Any]:
        return self.backend.save_queue_job(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.backend.get_queue_job(job_id)

    def list(self, **filters: Any) -> list[dict[str, Any]]:
        return self.backend.list_queue_jobs(**filters)

    def claim(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.backend.claim_queue_jobs(**kwargs)

    def complete(self, job_id: str, *, result: Any | None = None, now: str | None = None) -> dict[str, Any]:
        return self.backend.complete_queue_job(job_id, result=result, now=now)

    def fail(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.backend.fail_queue_job(job_id, **kwargs)


class ClueBatchSQLRepo:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def add_items(self, *args: Any, **kwargs: Any) -> int:
        return self.backend.add_clue_batch_items(*args, **kwargs)

    def get(self, raw_key: str) -> dict[str, Any] | None:
        return self.backend.get_clue_batch_item(raw_key)

    def list(self, **filters: Any) -> list[dict[str, Any]]:
        return self.backend.list_clue_batch_items(**filters)

    def count(self, *, status: str = "PENDING") -> int:
        return self.backend.count_clue_batch_items(status=status)

    def claim(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.backend.claim_clue_batch_items(**kwargs)

    def complete(self, **kwargs: Any) -> int:
        return self.backend.complete_clue_batch_items(**kwargs)

    def release(self, **kwargs: Any) -> int:
        return self.backend.release_clue_batch_items(**kwargs)


def sql_repositories(backend: Any) -> dict[str, Any]:
    return {
        "raw": RawSQLRepo(backend),
        "cleaned": CleanedSQLRepo(backend),
        "review": ReviewSQLRepo(backend),
        "audit": AuditSQLRepo(backend),
        "clue": ClueSQLRepo(backend),
        "entity": EntitySQLRepo(backend),
        "task": TaskSQLRepo(backend),
        "queue": QueueSQLRepo(backend),
        "clue_batch": ClueBatchSQLRepo(backend),
        "scheduler": SchedulerSQLRepo(backend),
    }


def _normalize_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


__all__ = [
    "AuditSQLRepo",
    "CleanedSQLRepo",
    "ClueBatchSQLRepo",
    "ClueSQLRepo",
    "EntitySQLRepo",
    "QueueSQLRepo",
    "RawSQLRepo",
    "ReviewSQLRepo",
    "SchedulerSQLRepo",
    "TaskSQLRepo",
    "sql_repositories",
]
