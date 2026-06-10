"""SQL-backed cron/queue runtime for bounded local BlackAgent collection."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from src.pipeline import OfflineClueBuildResult
from src.scheduling.layered_collection import (
    LAYER_CLUE_BUILD,
    LAYER_FAST,
    LAYER_SLOW,
    build_candidate_clues_from_raw_rows,
)
from storage.sql_backend import SQLBackend


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_CLUE_BUILD_DEDUPE_KEY = "job:build_candidate_clues:active"


@dataclass(frozen=True)
class ScheduleDefinition:
    schedule_name: str
    task_type: str
    layer: str
    task_payload: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    interval_seconds: int | None = None
    cron_expr: str | None = None
    priority: int = 50
    lease_seconds: int = 120
    max_attempts: int = 3
    dedupe_key: str | None = None


@dataclass(frozen=True)
class SchedulerSummary:
    schedule_count: int
    pending_jobs: int
    claimed_jobs: int
    failed_jobs: int
    succeeded_jobs: int
    pending_clue_batches: int
    schedules: list[dict[str, Any]]

    def model_dump(self) -> dict[str, Any]:
        return {
            "schedule_count": self.schedule_count,
            "pending_jobs": self.pending_jobs,
            "claimed_jobs": self.claimed_jobs,
            "failed_jobs": self.failed_jobs,
            "succeeded_jobs": self.succeeded_jobs,
            "pending_clue_batches": self.pending_clue_batches,
            "schedules": self.schedules,
        }


ExecutorFn = Callable[[dict[str, Any]], dict[str, Any]]
RunnerFn = Callable[[list[str]], dict[str, Any]]


class CronExpression:
    """Minimal five-field cron parser supporting '*', ranges, lists, and steps."""

    FIELD_RANGES = (
        (0, 59),  # minute
        (0, 23),  # hour
        (1, 31),  # day
        (1, 12),  # month
        (0, 7),  # weekday cron semantics: 0/7=Sunday, 1=Monday
    )

    def __init__(self, expression: str) -> None:
        fields = [part.strip() for part in str(expression or "").split() if part.strip()]
        if len(fields) != 5:
            raise ValueError("cron expression must contain 5 fields")
        self.expression = " ".join(fields)
        self._allowed = tuple(
            _parse_cron_field(field, minimum=minimum, maximum=maximum)
            for field, (minimum, maximum) in zip(fields, self.FIELD_RANGES, strict=True)
        )

    def next_after(self, value: datetime) -> datetime:
        candidate = value.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        max_iterations = 366 * 24 * 60 * 2
        for _ in range(max_iterations):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise RuntimeError(f"unable to resolve next run for cron expression: {self.expression}")

    def matches(self, value: datetime) -> bool:
        utc_value = value.astimezone(timezone.utc)
        return (
            utc_value.minute in self._allowed[0]
            and utc_value.hour in self._allowed[1]
            and utc_value.day in self._allowed[2]
            and utc_value.month in self._allowed[3]
            and ((utc_value.isoweekday() % 7) in self._allowed[4] or (utc_value.isoweekday() % 7 == 0 and 7 in self._allowed[4]))
        )


class CollectionQueueScheduler:
    """Bounded local cron/queue orchestration over the shared SQL backend."""

    def __init__(
        self,
        backend: SQLBackend,
        *,
        start_immediately: bool = True,
        default_worker_count: int = 3,
        claim_limit_per_worker: int = 2,
        max_claim_rounds: int = 6,
        retry_backoff_seconds: int = 45,
        clue_batch_limit: int = 500,
        runner: RunnerFn | None = None,
        task_executors: Mapping[str, ExecutorFn] | None = None,
    ) -> None:
        self.backend = backend
        self.start_immediately = start_immediately
        self.default_worker_count = max(1, int(default_worker_count))
        self.claim_limit_per_worker = max(1, int(claim_limit_per_worker))
        self.max_claim_rounds = max(1, int(max_claim_rounds))
        self.retry_backoff_seconds = max(0, int(retry_backoff_seconds))
        self.clue_batch_limit = max(1, int(clue_batch_limit))
        self.runner = runner or _run_subprocess
        self.task_executors: dict[str, ExecutorFn] = dict(task_executors or {})
        self._scheduler_db_path = _sqlite_path_from_dsn(self.backend.dsn)

    def default_schedules(
        self,
        *,
        public_catalog: str = "config/intel_sources.blackgray.yaml",
        x_config: str = "config/x_watch.example.yaml",
        telegram_config: str = "config/telegram_watch.example.yaml",
        fast_interval_seconds: int = 60,
        slow_interval_seconds: int = 600,
        clue_build_interval_seconds: int = 180,
        lease_seconds: int = 120,
        max_attempts: int = 3,
        cron_overrides: Mapping[str, str] | None = None,
    ) -> list[ScheduleDefinition]:
        cron_overrides = dict(cron_overrides or {})
        db_path = self._scheduler_db_path
        base = {
            "lease_seconds": max(1, int(lease_seconds)),
            "max_attempts": max(1, int(max_attempts)),
        }
        definitions = [
            ScheduleDefinition(
                schedule_name="fast_x_collect",
                task_type="collect_x_recent",
                layer=LAYER_FAST,
                task_payload={"db": db_path, "config": x_config},
                interval_seconds=max(1, int(fast_interval_seconds)),
                cron_expr=cron_overrides.get("fast_x_collect"),
                priority=96,
                dedupe_key="schedule:fast_x_collect",
                **base,
            ),
            ScheduleDefinition(
                schedule_name="fast_telegram_collect",
                task_type="collect_telegram_watch",
                layer=LAYER_FAST,
                task_payload={"db": db_path, "config": telegram_config},
                interval_seconds=max(1, int(fast_interval_seconds)),
                cron_expr=cron_overrides.get("fast_telegram_collect"),
                priority=100,
                dedupe_key="schedule:fast_telegram_collect",
                **base,
            ),
            ScheduleDefinition(
                schedule_name="slow_public_collect",
                task_type="collect_public_batch",
                layer=LAYER_SLOW,
                task_payload={"db": db_path, "catalog": public_catalog},
                interval_seconds=max(1, int(slow_interval_seconds)),
                cron_expr=cron_overrides.get("slow_public_collect"),
                priority=72,
                dedupe_key="schedule:slow_public_collect",
                **base,
            ),
            ScheduleDefinition(
                schedule_name="slow_public_hydration",
                task_type="hydrate_public_search",
                layer=LAYER_SLOW,
                task_payload={"db": db_path},
                interval_seconds=max(1, int(slow_interval_seconds)),
                cron_expr=cron_overrides.get("slow_public_hydration"),
                priority=68,
                dedupe_key="schedule:slow_public_hydration",
                **base,
            ),
            ScheduleDefinition(
                schedule_name="scheduled_clue_build",
                task_type="build_candidate_clues",
                layer=LAYER_CLUE_BUILD,
                task_payload={
                    "db": db_path,
                    "quality_profile": "high_precision",
                    "require_cross_source": True,
                    "require_evidence_chain": True,
                    "batch_limit": self.clue_batch_limit,
                },
                interval_seconds=max(1, int(clue_build_interval_seconds)),
                cron_expr=cron_overrides.get("scheduled_clue_build"),
                priority=88,
                dedupe_key=ACTIVE_CLUE_BUILD_DEDUPE_KEY,
                **base,
            ),
        ]
        return definitions

    def sync_schedules(self, definitions: Iterable[ScheduleDefinition], *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or _utc_now()
        saved: list[dict[str, Any]] = []
        for definition in definitions:
            existing = self.backend.get_schedule(definition.schedule_name)
            next_run_at = (
                existing.get("next_run_at")
                if existing is not None
                else self._initial_next_run_at(definition, current).isoformat()
            )
            payload = {
                "schedule_name": definition.schedule_name,
                "task_type": definition.task_type,
                "layer": definition.layer,
                "task_payload": dict(definition.task_payload),
                "enabled": definition.enabled,
                "interval_seconds": definition.interval_seconds,
                "cron_expr": definition.cron_expr,
                "priority": definition.priority,
                "lease_seconds": definition.lease_seconds,
                "max_attempts": definition.max_attempts,
                "dedupe_key": definition.dedupe_key,
                "next_run_at": next_run_at,
                "last_run_at": (existing or {}).get("last_run_at"),
                "last_enqueue_at": (existing or {}).get("last_enqueue_at"),
            }
            saved.append(self.backend.save_schedule(payload))
        return saved

    def status(self) -> SchedulerSummary:
        schedules = self.backend.list_schedules()
        return SchedulerSummary(
            schedule_count=len(schedules),
            pending_jobs=len(self.backend.list_queue_jobs(status="PENDING")),
            claimed_jobs=len(self.backend.list_queue_jobs(status="CLAIMED")),
            failed_jobs=len(self.backend.list_queue_jobs(status="FAILED")),
            succeeded_jobs=len(self.backend.list_queue_jobs(status="SUCCEEDED")),
            pending_clue_batches=self.backend.count_clue_batch_items(status="PENDING"),
            schedules=schedules,
        )

    def tick(self, *, now: datetime | None = None, limit: int | None = None) -> dict[str, Any]:
        current = now or _utc_now()
        due = self.backend.list_due_schedules(now=current.isoformat(), limit=limit)
        enqueued: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for schedule in due:
            dedupe_key = str(schedule.get("dedupe_key") or f"schedule:{schedule['schedule_name']}")
            if self.backend.has_active_queue_job(dedupe_key):
                skipped.append({"schedule_name": schedule["schedule_name"], "reason": "active_job_exists"})
            else:
                job = self.enqueue_job_from_schedule(schedule, now=current)
                enqueued.append(job)
            self._advance_schedule_after_tick(schedule, current)
        return {
            "status": "ok",
            "due_count": len(due),
            "enqueued_count": len(enqueued),
            "skipped_count": len(skipped),
            "due_schedules": [item.get("schedule_name") for item in due],
            "enqueued_jobs": enqueued,
            "skipped": skipped,
        }

    def enqueue_job_from_schedule(self, schedule: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        current = now or _utc_now()
        dedupe_key = str(schedule.get("dedupe_key") or f"schedule:{schedule['schedule_name']}")
        payload = {
            "schedule_name": schedule["schedule_name"],
            "task_type": schedule["task_type"],
            "layer": schedule["layer"],
            "priority": int(schedule.get("priority") or 50),
            "status": "PENDING",
            "dedupe_key": dedupe_key,
            "attempt_count": 0,
            "max_attempts": int(schedule.get("max_attempts") or 3),
            "available_at": current.isoformat(),
            "lease_seconds": int(schedule.get("lease_seconds") or 120),
            "task_payload": dict(schedule.get("task_payload") or {}),
        }
        return self.backend.save_queue_job(payload)

    def enqueue_unique_job(
        self,
        *,
        task_type: str,
        layer: str,
        dedupe_key: str,
        task_payload: Mapping[str, Any] | None = None,
        priority: int = 50,
        max_attempts: int = 3,
        lease_seconds: int = 120,
        schedule_name: str | None = None,
    ) -> dict[str, Any] | None:
        if self.backend.has_active_queue_job(dedupe_key):
            return None
        return self.backend.save_queue_job(
            {
                "schedule_name": schedule_name,
                "task_type": task_type,
                "layer": layer,
                "priority": priority,
                "status": "PENDING",
                "dedupe_key": dedupe_key,
                "attempt_count": 0,
                "max_attempts": max_attempts,
                "available_at": _utc_now().isoformat(),
                "lease_seconds": lease_seconds,
                "task_payload": dict(task_payload or {}),
            }
        )

    def run_workers(
        self,
        *,
        worker_count: int | None = None,
        claim_limit: int | None = None,
        max_rounds: int | None = None,
        layers: Iterable[str] = (),
    ) -> dict[str, Any]:
        worker_count = max(1, int(worker_count or self.default_worker_count))
        claim_limit = max(1, int(claim_limit or self.claim_limit_per_worker))
        max_rounds = max(1, int(max_rounds or self.max_claim_rounds))
        normalized_layers = [str(item).strip() for item in layers if str(item).strip()]

        executed: list[dict[str, Any]] = []
        completed_count = 0
        failed_count = 0
        claimed_count = 0

        for round_index in range(max_rounds):
            round_claimed = 0
            for worker_index in range(worker_count):
                worker_id = f"local-worker-{worker_index + 1}"
                claimed = self.backend.claim_queue_jobs(
                    worker_id=worker_id,
                    limit=claim_limit,
                    layers=tuple(normalized_layers),
                )
                if not claimed:
                    continue
                round_claimed += len(claimed)
                claimed_count += len(claimed)
                for job in claimed:
                    try:
                        result = self.execute_job(job)
                        completed_count += 1
                        executed.append({"job_id": job["job_id"], "status": "SUCCEEDED", "worker_id": worker_id, "result": result})
                    except Exception as exc:  # noqa: BLE001 - queue runtime must record arbitrary task failures
                        failed_count += 1
                        self.backend.fail_queue_job(
                            job["job_id"],
                            error=f"{exc.__class__.__name__}: {exc}",
                            retry_backoff_seconds=self.retry_backoff_seconds,
                        )
                        self._persist_task_history(
                            job,
                            status="FAILED" if int(job.get("attempt_count") or 0) >= int(job.get("max_attempts") or 3) else "RETRY_PENDING",
                            result=None,
                            error=f"{exc.__class__.__name__}: {exc}",
                        )
                        executed.append(
                            {
                                "job_id": job["job_id"],
                                "status": "FAILED",
                                "worker_id": worker_id,
                                "error": f"{exc.__class__.__name__}: {exc}",
                            }
                        )
            if round_claimed == 0:
                break
        return {
            "status": "ok",
            "claimed_count": claimed_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "executed": executed,
        }

    def execute_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        task_type = str(job.get("task_type") or "")
        executor = self.task_executors.get(task_type)
        if executor is not None:
            result = executor(dict(job))
        else:
            result = self._dispatch_builtin(dict(job))
        self.backend.complete_queue_job(str(job["job_id"]), result=result)
        self._persist_task_history(job, status="SUCCEEDED", result=result, error=None)
        return result

    def _dispatch_builtin(self, job: dict[str, Any]) -> dict[str, Any]:
        task_type = str(job.get("task_type") or "")
        payload = dict(job.get("task_payload") or {})
        if task_type == "collect_x_recent":
            return self._execute_collection_job(
                job,
                [
                    sys.executable,
                    "scripts/x_recent_search_collector.py",
                    "--config",
                    str(payload.get("config") or "config/x_watch.example.yaml"),
                    "--db",
                    self._scheduler_db_path,
                ],
            )
        if task_type == "collect_telegram_watch":
            command = [
                sys.executable,
                "scripts/telegram_telethon_collector.py",
                "--config",
                str(payload.get("config") or "config/telegram_watch.example.yaml"),
                "--db",
                self._scheduler_db_path,
                "--once",
            ]
            for payload_name, flag_name in (
                ("username_limit", "--username-limit"),
                ("search_limit", "--search-limit"),
                ("history_limit", "--history-limit"),
            ):
                value = payload.get(payload_name)
                if value:
                    command.extend([flag_name, str(value)])
            return self._execute_collection_job(job, command)
        if task_type == "collect_public_batch":
            return self._execute_collection_job(
                job,
                [
                    sys.executable,
                    "scripts/collect_public_sources.py",
                    "--catalog",
                    str(payload.get("catalog") or "config/intel_sources.blackgray.yaml"),
                    "--db",
                    self._scheduler_db_path,
                ],
            )
        if task_type == "hydrate_public_search":
            return self._execute_collection_job(
                job,
                [
                    sys.executable,
                    "scripts/hydrate_public_search_results.py",
                    "--db",
                    self._scheduler_db_path,
                ],
            )
        if task_type == "build_candidate_clues":
            return self._execute_clue_build_job(job)
        raise KeyError(f"unsupported task_type: {task_type}")

    def _execute_collection_job(self, job: dict[str, Any], command: list[str]) -> dict[str, Any]:
        before_keys = {str(row.get("hash_id") or "") for row in self.backend.list_raw() if str(row.get("hash_id") or "")}
        result = self.runner(command)
        after_rows = self.backend.list_raw()
        new_rows = [row for row in after_rows if str(row.get("hash_id") or "") and str(row.get("hash_id") or "") not in before_keys]
        pending_added = self.backend.add_clue_batch_items(new_rows, source_job_id=str(job["job_id"]))
        followup_job_id = None
        if pending_added > 0:
            followup = self.enqueue_unique_job(
                task_type="build_candidate_clues",
                layer=LAYER_CLUE_BUILD,
                dedupe_key=ACTIVE_CLUE_BUILD_DEDUPE_KEY,
                task_payload={
                    "db": self._scheduler_db_path,
                    "quality_profile": "high_precision",
                    "require_cross_source": True,
                    "require_evidence_chain": True,
                    "batch_limit": self.clue_batch_limit,
                },
                priority=90,
                max_attempts=3,
                lease_seconds=120,
                schedule_name="followup_clue_build",
            )
            followup_job_id = (followup or {}).get("job_id")
        return {
            "command": command,
            "subprocess": result,
            "new_raw_count": len(new_rows),
            "pending_clue_batch_added": pending_added,
            "followup_clue_job_id": followup_job_id,
        }

    def _execute_clue_build_job(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(job.get("task_payload") or {})
        batch_limit = max(1, int(payload.get("batch_limit") or self.clue_batch_limit))
        claimed_items = self.backend.claim_clue_batch_items(job_id=str(job["job_id"]), limit=batch_limit)
        if not claimed_items:
            return {"status": "skipped", "reason": "no_pending_clue_batch_items", "claimed_count": 0}

        raw_keys = [str(item.get("raw_key") or "") for item in claimed_items if str(item.get("raw_key") or "")]
        rows = self.backend.list_raw_by_hash_ids(raw_keys)
        try:
            result: OfflineClueBuildResult = build_candidate_clues_from_raw_rows(
                rows,
                quality_profile=str(payload.get("quality_profile") or "high_precision"),
                require_cross_source=bool(payload.get("require_cross_source", True)),
                require_evidence_chain=bool(payload.get("require_evidence_chain", True)),
            )
            for clue in result.clues:
                self.backend.save_clue(clue)
            completed = self.backend.complete_clue_batch_items(job_id=str(job["job_id"]), raw_keys=raw_keys)
        except Exception:
            self.backend.release_clue_batch_items(job_id=str(job["job_id"]), raw_keys=raw_keys)
            raise

        followup_job_id = None
        if self.backend.count_clue_batch_items(status="PENDING") > 0:
            followup = self.enqueue_unique_job(
                task_type="build_candidate_clues",
                layer=LAYER_CLUE_BUILD,
                dedupe_key=ACTIVE_CLUE_BUILD_DEDUPE_KEY,
                task_payload=payload,
                priority=int(job.get("priority") or 88),
                max_attempts=int(job.get("max_attempts") or 3),
                lease_seconds=int(job.get("lease_seconds") or 120),
                schedule_name="followup_clue_build",
            )
            followup_job_id = (followup or {}).get("job_id")
        return {
            "status": result.status,
            "input_count": result.input_count,
            "saved_clue_count": result.saved_clue_count,
            "high_quality_count": result.high_quality_count,
            "candidate_count": result.candidate_count,
            "completed_batch_count": completed,
            "remaining_pending_clue_batches": self.backend.count_clue_batch_items(status="PENDING"),
            "followup_clue_job_id": followup_job_id,
            "execution_summary": result.execution_summary,
        }

    def _advance_schedule_after_tick(self, schedule: Mapping[str, Any], current: datetime) -> None:
        next_run_at = self._compute_next_run(
            interval_seconds=schedule.get("interval_seconds"),
            cron_expr=schedule.get("cron_expr"),
            anchor=current,
        )
        payload = dict(schedule)
        payload["last_enqueue_at"] = current.isoformat()
        payload["next_run_at"] = next_run_at.isoformat()
        payload["updated_at"] = current.isoformat()
        self.backend.save_schedule(payload)

    def _initial_next_run_at(self, definition: ScheduleDefinition, current: datetime) -> datetime:
        if self.start_immediately:
            return current
        return self._compute_next_run(
            interval_seconds=definition.interval_seconds,
            cron_expr=definition.cron_expr,
            anchor=current,
        )

    def _compute_next_run(self, *, interval_seconds: Any, cron_expr: Any, anchor: datetime) -> datetime:
        if cron_expr:
            return CronExpression(str(cron_expr)).next_after(anchor)
        return anchor + timedelta(seconds=max(1, int(interval_seconds or 60)))

    def _persist_task_history(self, job: Mapping[str, Any], *, status: str, result: Any | None, error: str | None) -> None:
        payload = {
            "task_id": str(job["job_id"]),
            "task_type": job.get("task_type"),
            "status": status,
            "schedule_name": job.get("schedule_name"),
            "layer": job.get("layer"),
            "dedupe_key": job.get("dedupe_key"),
            "attempt_count": job.get("attempt_count"),
            "max_attempts": job.get("max_attempts"),
            "result": result,
            "error": error,
        }
        self.backend.save_task(payload, task_type=payload.get("task_type"), status=status)


def _run_subprocess(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603,S607 - explicit local script invocation
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    parsed_output = None
    if stdout:
        try:
            parsed_output = json.loads(stdout)
        except json.JSONDecodeError:
            parsed_output = None
    if completed.returncode != 0:
        raise RuntimeError(f"subprocess_failed[{completed.returncode}]: {stderr or stdout or command}")
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "parsed_output": parsed_output,
    }


def _parse_cron_field(expression: str, *, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in expression.split(","):
        token = part.strip()
        if not token:
            continue
        if token == "*":
            values.update(range(minimum, maximum + 1))
            continue
        if "/" in token:
            base, step_text = token.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError(f"invalid cron step: {token}")
            if base in {"", "*"}:
                start, end = minimum, maximum
            elif "-" in base:
                start_text, end_text = base.split("-", 1)
                start, end = int(start_text), int(end_text)
            else:
                start = end = int(base)
            values.update(range(start, end + 1, step))
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            values.update(range(int(start_text), int(end_text) + 1))
            continue
        values.add(int(token))

    if not values:
        raise ValueError(f"cron field produced no values: {expression}")
    if min(values) < minimum or max(values) > maximum:
        raise ValueError(f"cron field out of range: {expression}")
    return values


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_path_from_dsn(dsn: str) -> str:
    if not str(dsn).startswith("sqlite:///"):
        raise ValueError("collection queue scheduler currently requires sqlite:/// DSN")
    path = str(dsn)[len("sqlite:///") :]
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


__all__ = [
    "ACTIVE_CLUE_BUILD_DEDUPE_KEY",
    "CollectionQueueScheduler",
    "CronExpression",
    "ScheduleDefinition",
    "SchedulerSummary",
]
