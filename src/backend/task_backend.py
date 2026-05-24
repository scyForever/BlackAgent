"""Small local task backend used before a real queue is introduced.

The adapter keeps the production-facing contract deliberately narrow:

``submit`` -> enqueue a task, ``get``/``list`` -> inspect state,
``run_pending`` -> execute pending work deterministically in the current
process.  A daemon-thread mode is also provided for local service deployments,
but tests can use the synchronous queue without timing races.
"""

from __future__ import annotations

import copy
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock, Thread
from typing import Any, Callable, Mapping
from uuid import uuid4


TaskHandler = Callable[[dict[str, Any]], Any]


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    """Lifecycle states for local backend jobs."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class TaskError:
    """Failure information persisted on a failed task."""

    error_type: str
    message: str
    traceback: str

    def model_dump(self) -> dict[str, str]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "traceback": self.traceback,
        }


@dataclass
class TaskRecord:
    """Serializable task state returned by the backend."""

    task_id: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    result: Any | None = None
    error: TaskError | None = None
    attempts: int = 0
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "payload": copy.deepcopy(self.payload),
            "metadata": copy.deepcopy(self.metadata),
            "status": self.status.value,
            "result": copy.deepcopy(self.result),
            "error": self.error.model_dump() if self.error else None,
            "attempts": self.attempts,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TaskBackend:
    """In-process task backend with a queue-shaped API.

    Parameters
    ----------
    handlers:
        Optional task-name registry.  A handler receives the task payload and
        returns a JSON-serializable result.
    execution_mode:
        ``"sync"`` keeps tasks pending until ``run_pending`` is called.
        ``"threaded"`` starts each submitted task in a local daemon thread.
    auto_start:
        Convenience flag equivalent to threaded execution for submitted tasks.
    """

    def __init__(
        self,
        handlers: Mapping[str, TaskHandler] | None = None,
        *,
        execution_mode: str = "sync",
        auto_start: bool = False,
    ) -> None:
        normalized_mode = execution_mode.lower().strip()
        if normalized_mode not in {"sync", "threaded"}:
            raise ValueError("execution_mode must be 'sync' or 'threaded'")

        self.execution_mode = normalized_mode
        self.auto_start = auto_start
        self._handlers: dict[str, TaskHandler] = dict(handlers or {})
        self._task_handlers: dict[str, TaskHandler] = {}
        self._tasks: dict[str, TaskRecord] = {}
        self._order: list[str] = []
        self._threads: dict[str, Thread] = {}
        self._lock = RLock()

    def register(self, name: str, handler: TaskHandler) -> None:
        """Register or replace a named task handler."""

        if not name:
            raise ValueError("task name must be non-empty")
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            self._handlers[name] = handler

    def submit(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        handler: TaskHandler | None = None,
        metadata: Mapping[str, Any] | None = None,
        task_id: str | None = None,
    ) -> TaskRecord:
        """Create a pending task and optionally attach a one-off handler."""

        if not name:
            raise ValueError("task name must be non-empty")
        if handler is not None and not callable(handler):
            raise TypeError("handler must be callable")

        record = TaskRecord(
            task_id=task_id or str(uuid4()),
            name=name,
            payload=dict(payload or {}),
            metadata=dict(metadata or {}),
        )

        with self._lock:
            if record.task_id in self._tasks:
                raise ValueError(f"duplicate task_id: {record.task_id}")
            self._tasks[record.task_id] = record
            self._order.append(record.task_id)
            if handler is not None:
                self._task_handlers[record.task_id] = handler

        if self.auto_start or self.execution_mode == "threaded":
            self._start_thread(record.task_id)

        return self._snapshot(record.task_id)

    def get(self, task_id: str) -> TaskRecord | None:
        """Return a copy of one task state, or ``None`` when unknown."""

        with self._lock:
            if task_id not in self._tasks:
                return None
            return self._snapshot(task_id)

    def list(self, status: TaskStatus | str | None = None) -> list[TaskRecord]:
        """List task states in submit order, optionally filtered by status."""

        normalized_status = self._normalize_status(status) if status is not None else None
        with self._lock:
            task_ids = list(self._order)
        records = [self._snapshot(task_id) for task_id in task_ids]
        if normalized_status is not None:
            records = [record for record in records if record.status == normalized_status]
        return records

    list_tasks = list

    def run_pending(self, *, limit: int | None = None) -> list[TaskRecord]:
        """Run pending tasks synchronously and return their final states."""

        with self._lock:
            pending_ids = [
                task_id
                for task_id in self._order
                if self._tasks[task_id].status == TaskStatus.PENDING
            ]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            pending_ids = pending_ids[:limit]

        return [self._run_one(task_id) for task_id in pending_ids]

    def _start_thread(self, task_id: str) -> None:
        thread = Thread(target=self._run_one, args=(task_id,), name=f"blackagent-task-{task_id}", daemon=True)
        with self._lock:
            self._threads[task_id] = thread
        thread.start()

    def _run_one(self, task_id: str) -> TaskRecord:
        with self._lock:
            record = self._tasks[task_id]
            if record.status != TaskStatus.PENDING:
                return self._snapshot(task_id)
            record.status = TaskStatus.RUNNING
            record.started_at = utc_now()
            record.finished_at = None
            record.attempts += 1
            record.error = None

        try:
            handler = self._resolve_handler(task_id)
            result = handler(copy.deepcopy(record.payload))
        except Exception as exc:  # noqa: BLE001 - backend must record arbitrary task failures.
            error = TaskError(
                error_type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
            )
            with self._lock:
                record = self._tasks[task_id]
                record.status = TaskStatus.FAILED
                record.error = error
                record.result = None
                record.finished_at = utc_now()
            return self._snapshot(task_id)

        with self._lock:
            record = self._tasks[task_id]
            record.status = TaskStatus.SUCCEEDED
            record.result = result
            record.error = None
            record.finished_at = utc_now()
        return self._snapshot(task_id)

    def _resolve_handler(self, task_id: str) -> TaskHandler:
        with self._lock:
            record = self._tasks[task_id]
            handler = self._task_handlers.get(task_id) or self._handlers.get(record.name)
        if handler is None:
            raise LookupError(f"no handler registered for task '{record.name}'")
        return handler

    def _snapshot(self, task_id: str) -> TaskRecord:
        return copy.deepcopy(self._tasks[task_id])

    @staticmethod
    def _normalize_status(status: TaskStatus | str) -> TaskStatus:
        if isinstance(status, TaskStatus):
            return status
        try:
            return TaskStatus(str(status).upper())
        except ValueError as exc:
            raise ValueError(f"unknown task status: {status}") from exc


__all__ = ["TaskBackend", "TaskError", "TaskHandler", "TaskRecord", "TaskStatus"]
