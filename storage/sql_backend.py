"""SQL persistence adapter for BlackAgent storage contracts.

The adapter is intentionally dependency-light:
- ``sqlite:///...`` works with the Python standard library.
- ``postgresql://...`` is accepted only when optional ``psycopg`` is installed.

Rows keep a small set of indexed columns plus the complete JSON payload so the
adapter can persist current Pydantic contracts without forcing a wider schema
migration on the rest of the codebase.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID, uuid4


SUPPORTED_SCHEMES = ("sqlite:///", "postgresql://", "postgres://")


class SQLBackend:
    """Small SQL-backed persistence adapter for local and future service use."""

    def __init__(self, dsn: str, connection: Any, *, dialect: str) -> None:
        self.dsn = dsn
        self.connection = connection
        self.dialect = dialect
        self._placeholder = "%s" if dialect == "postgresql" else "?"
        self._lock = RLock()

    def create_schema(self) -> None:
        """Create all tables required by the persistence slice."""

        statements = [
            """
            CREATE TABLE IF NOT EXISTS raw_records (
                hash_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL UNIQUE,
                source_type TEXT,
                source_name TEXT,
                legal_basis TEXT,
                content_text TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_records_trace_id ON raw_records(trace_id)",
            """
            CREATE TABLE IF NOT EXISTS review_tasks (
                hypothesis_id TEXT PRIMARY KEY,
                source_trace_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks(status)",
            "CREATE INDEX IF NOT EXISTS idx_review_tasks_source_trace_id ON review_tasks(source_trace_id)",
            """
            CREATE TABLE IF NOT EXISTS cleaned_texts (
                source_trace_id TEXT PRIMARY KEY,
                clean_id TEXT,
                dedup_group_id TEXT,
                risk_level TEXT,
                quality_score REAL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_cleaned_texts_risk_level ON cleaned_texts(risk_level)",
            "CREATE INDEX IF NOT EXISTS idx_cleaned_texts_quality_score ON cleaned_texts(quality_score)",
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor TEXT,
                target_id TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type)",
            "CREATE INDEX IF NOT EXISTS idx_audit_events_target_id ON audit_events(target_id)",
            """
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                source_trace_id TEXT,
                entity_type TEXT,
                entity_value TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_entities_source_trace_id ON entities(source_trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type)",
            """
            CREATE TABLE IF NOT EXISTS candidate_clues (
                clue_id TEXT PRIMARY KEY,
                clue_type TEXT,
                risk_category TEXT,
                quality_score REAL,
                confidence REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_candidate_clues_risk_category ON candidate_clues(risk_category)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_clues_quality_score ON candidate_clues(quality_score)",
            """
            CREATE TABLE IF NOT EXISTS task_runs (
                task_id TEXT PRIMARY KEY,
                task_type TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status)",
            "CREATE INDEX IF NOT EXISTS idx_task_runs_type ON task_runs(task_type)",
        ]
        for statement in statements:
            self._execute(statement)
        self._commit()

    def save_raw(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        """Upsert one raw intelligence record."""

        data = _normalize_payload(record)
        hash_id = _require_text(data, "hash_id")
        trace_id = str(data.get("trace_id") or data.get("source_trace_id") or hash_id)
        created_at = str(data.get("crawl_time") or data.get("created_at") or _now_iso())
        data["trace_id"] = trace_id

        self._upsert(
            "raw_records",
            key_column="hash_id",
            columns={
                "hash_id": hash_id,
                "trace_id": trace_id,
                "source_type": data.get("source_type"),
                "source_name": data.get("source_name"),
                "legal_basis": data.get("legal_basis"),
                "content_text": data.get("content_text"),
                "created_at": created_at,
                "payload": _to_json(data),
            },
        )
        return dict(data)

    def save_cleaned(self, record: Mapping[str, Any] | Any, *, commit: bool = True) -> dict[str, Any]:
        """Upsert one cleaned-text payload keyed by source trace id."""

        data = _normalize_payload(record)
        source_trace_id = _require_text(data, "source_trace_id")
        clean_id = str(data.get("clean_id") or uuid4())
        created_at = str(data.get("created_at") or _now_iso())

        data["source_trace_id"] = source_trace_id
        data["clean_id"] = clean_id
        data["created_at"] = created_at

        columns = {
            "source_trace_id": source_trace_id,
            "clean_id": clean_id,
            "dedup_group_id": data.get("dedup_group_id"),
            "risk_level": data.get("risk_level"),
            "quality_score": data.get("quality_score"),
            "created_at": created_at,
            "payload": _to_json(data),
        }
        if commit:
            self._upsert(
                "cleaned_texts",
                key_column="source_trace_id",
                columns=columns,
            )
        else:
            names = list(columns)
            assignments = ", ".join(
                f"{name} = excluded.{name}" for name in names if name != "source_trace_id"
            )
            sql = (
                f"INSERT INTO cleaned_texts ({', '.join(names)}) "
                f"VALUES ({self._placeholders(len(names))}) "
                f"ON CONFLICT(source_trace_id) DO UPDATE SET {assignments}"
            )
            self._execute(sql, [columns[name] for name in names])
        return dict(data)

    def list_raw(self, limit: int | None = None) -> list[dict[str, Any]]:
        """List raw intelligence payloads in insertion-time order."""

        return self._list_payloads(
            "SELECT payload FROM raw_records ORDER BY created_at, hash_id",
            limit=limit,
        )

    def list_cleaned(self, *, risk_level: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """List cleaned-text payloads, optionally filtered by risk level."""

        if risk_level is None:
            sql = "SELECT payload FROM cleaned_texts ORDER BY created_at, source_trace_id"
            return self._list_payloads(sql, limit=limit)
        sql = (
            "SELECT payload FROM cleaned_texts "
            f"WHERE risk_level = {self._placeholder} "
            "ORDER BY created_at, source_trace_id"
        )
        return self._list_payloads(sql, [risk_level], limit=limit)

    def clear_cleaned(self) -> None:
        """Delete all cleaned-text rows before replacing a derived corpus snapshot."""

        self._execute("DELETE FROM cleaned_texts")
        self._commit()

    def save_review(
        self,
        review: Mapping[str, Any] | Any,
        *,
        state: Mapping[str, Any] | Any | None = None,
    ) -> dict[str, Any]:
        """Upsert one human-review task or sandbox hypothesis."""

        data = _normalize_payload(review)
        state_data = _normalize_payload(state) if state is not None else dict(data.get("review_state") or {})

        hypothesis_id = str(
            data.get("hypothesis_id")
            or state_data.get("hypothesis_id")
            or data.get("task_id")
            or uuid4()
        )
        source_trace_id = str(data.get("source_trace_id") or "")
        status = str(data.get("status") or state_data.get("status") or "PENDING")
        created_at = str(data.get("created_at") or _now_iso())
        updated_at = str(data.get("updated_at") or state_data.get("updated_at") or _now_iso())

        data["hypothesis_id"] = hypothesis_id
        review_state = dict(data.get("review_state") or {})
        review_state.update(state_data)
        review_state.setdefault("hypothesis_id", hypothesis_id)
        review_state["status"] = status
        review_state.setdefault("updated_at", updated_at)
        data["review_state"] = review_state

        self._upsert(
            "review_tasks",
            key_column="hypothesis_id",
            columns={
                "hypothesis_id": hypothesis_id,
                "source_trace_id": source_trace_id,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
                "payload": _to_json(data),
            },
        )
        return dict(data)

    def list_review(self, status: str | None = None) -> list[dict[str, Any]]:
        """List review tasks, optionally filtered by status."""

        if status is None or str(status).lower() == "all":
            return self._list_payloads(
                "SELECT payload FROM review_tasks ORDER BY created_at, hypothesis_id"
            )
        sql = (
            "SELECT payload FROM review_tasks "
            f"WHERE status = {self._placeholder} "
            "ORDER BY created_at, hypothesis_id"
        )
        return self._list_payloads(sql, [status])

    def append_audit(self, event: Mapping[str, Any] | Any) -> dict[str, Any]:
        """Append one audit event."""

        data = _normalize_payload(event)
        event_id = str(data.get("event_id") or uuid4())
        event_type = _require_text(data, "event_type")
        created_at = str(data.get("created_at") or _now_iso())
        data["event_id"] = event_id
        data["created_at"] = created_at

        sql = (
            "INSERT INTO audit_events "
            "(event_id, event_type, actor, target_id, created_at, payload) "
            f"VALUES ({self._placeholders(6)})"
        )
        self._execute(
            sql,
            [
                event_id,
                event_type,
                data.get("actor"),
                data.get("target_id"),
                created_at,
                _to_json(data),
            ],
        )
        self._commit()
        return dict(data)

    def list_audit(self, event_type: str | None = None) -> list[dict[str, Any]]:
        """List audit events, optionally filtered by event type."""

        if event_type is None:
            return self._list_payloads("SELECT payload FROM audit_events ORDER BY created_at, event_id")
        sql = (
            "SELECT payload FROM audit_events "
            f"WHERE event_type = {self._placeholder} "
            "ORDER BY created_at, event_id"
        )
        return self._list_payloads(sql, [event_type])

    def save_entity(self, entity: Mapping[str, Any] | Any) -> dict[str, Any]:
        """Upsert one extracted entity."""

        data = _normalize_payload(entity)
        entity_id = str(data.get("entity_id") or uuid4())
        created_at = str(data.get("created_at") or _now_iso())
        data["entity_id"] = entity_id
        data["created_at"] = created_at

        self._upsert(
            "entities",
            key_column="entity_id",
            columns={
                "entity_id": entity_id,
                "source_trace_id": data.get("source_trace_id"),
                "entity_type": data.get("entity_type"),
                "entity_value": data.get("entity_value"),
                "created_at": created_at,
                "payload": _to_json(data),
            },
        )
        return dict(data)

    def save_clue(self, clue: Mapping[str, Any] | Any) -> dict[str, Any]:
        """Upsert one candidate clue."""

        data = _normalize_payload(clue)
        clue_id = _require_text(data, "clue_id")
        created_at = str(data.get("created_at") or _now_iso())
        updated_at = str(data.get("updated_at") or created_at)
        data["clue_id"] = clue_id
        data["created_at"] = created_at
        data["updated_at"] = updated_at

        self._upsert(
            "candidate_clues",
            key_column="clue_id",
            columns={
                "clue_id": clue_id,
                "clue_type": data.get("clue_type"),
                "risk_category": data.get("risk_category"),
                "quality_score": data.get("quality_score"),
                "confidence": data.get("confidence"),
                "created_at": created_at,
                "updated_at": updated_at,
                "payload": _to_json(data),
            },
        )
        return dict(data)

    def list_clues(self, *, risk_category: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """List candidate clues, optionally filtered by risk category."""

        if risk_category is None:
            return self._list_payloads(
                "SELECT payload FROM candidate_clues ORDER BY quality_score DESC, confidence DESC, updated_at DESC",
                limit=limit,
            )
        sql = (
            "SELECT payload FROM candidate_clues "
            f"WHERE risk_category = {self._placeholder} "
            "ORDER BY quality_score DESC, confidence DESC, updated_at DESC"
        )
        return self._list_payloads(sql, [risk_category], limit=limit)

    def list_entities(self, source_trace_id: str | None = None) -> list[dict[str, Any]]:
        """List entities, optionally filtered by source trace id."""

        if source_trace_id is None:
            return self._list_payloads("SELECT payload FROM entities ORDER BY created_at, entity_id")
        sql = (
            "SELECT payload FROM entities "
            f"WHERE source_trace_id = {self._placeholder} "
            "ORDER BY created_at, entity_id"
        )
        return self._list_payloads(sql, [source_trace_id])

    def save_task(self, task: Mapping[str, Any] | str, **fields: Any) -> dict[str, Any]:
        """Upsert one task-run status record."""

        if isinstance(task, str):
            data: dict[str, Any] = {"task_id": task}
        else:
            data = _normalize_payload(task)
        data.update(fields)

        task_id = str(data.get("task_id") or data.get("run_id") or uuid4())
        existing = self.get_task(task_id)
        created_at = str(data.get("created_at") or (existing or {}).get("created_at") or _now_iso())
        updated_at = str(data.get("updated_at") or _now_iso())
        status = str(data.get("status") or (existing or {}).get("status") or "PENDING")

        data["task_id"] = task_id
        data["created_at"] = created_at
        data["updated_at"] = updated_at
        data["status"] = status

        self._upsert(
            "task_runs",
            key_column="task_id",
            columns={
                "task_id": task_id,
                "task_type": data.get("task_type"),
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
                "payload": _to_json(data),
            },
        )
        return dict(data)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Return one task-run payload by id."""

        sql = f"SELECT payload FROM task_runs WHERE task_id = {self._placeholder}"
        cursor = self._execute(sql, [task_id])
        row = cursor.fetchone()
        if row is None:
            return None
        return _from_json(_row_value(row, "payload"))

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        """List task-run payloads, optionally filtered by status."""

        if status is None or str(status).lower() == "all":
            return self._list_payloads("SELECT payload FROM task_runs ORDER BY created_at, task_id")
        sql = (
            "SELECT payload FROM task_runs "
            f"WHERE status = {self._placeholder} "
            "ORDER BY created_at, task_id"
        )
        return self._list_payloads(sql, [status])

    def close(self) -> None:
        """Close the underlying SQL connection."""

        self.connection.close()

    def _upsert(self, table: str, *, key_column: str, columns: Mapping[str, Any]) -> None:
        names = list(columns)
        assignments = ", ".join(
            f"{name} = excluded.{name}" for name in names if name != key_column
        )
        sql = (
            f"INSERT INTO {table} ({', '.join(names)}) "
            f"VALUES ({self._placeholders(len(names))}) "
            f"ON CONFLICT({key_column}) DO UPDATE SET {assignments}"
        )
        self._execute(sql, [columns[name] for name in names])
        self._commit()

    def _list_payloads(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params = list(params or [])
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            sql = f"{sql} LIMIT {self._placeholder}"
            params.append(limit)
        cursor = self._execute(sql, params)
        return [_from_json(_row_value(row, "payload")) for row in cursor.fetchall()]

    def _placeholders(self, count: int) -> str:
        return ", ".join(self._placeholder for _ in range(count))

    def _execute(self, sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> Any:
        with self._lock:
            return self.connection.execute(sql, tuple(params or ()))

    def _commit(self) -> None:
        with self._lock:
            self.connection.commit()


def connect(dsn: str) -> SQLBackend:
    """Open a SQLBackend for ``sqlite:///`` or optional ``postgresql://`` DSNs."""

    if dsn.startswith("sqlite:///"):
        path = _sqlite_path_from_dsn(dsn)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return SQLBackend(dsn, connection, dialect="sqlite")

    if dsn.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on optional local dependency
            raise RuntimeError(
                "PostgreSQL DSN requires optional dependency 'psycopg'. "
                "Install psycopg or use sqlite:///... for local tests."
            ) from exc
        connection = psycopg.connect(dsn, row_factory=dict_row)
        return SQLBackend(dsn, connection, dialect="postgresql")

    schemes = ", ".join(SUPPORTED_SCHEMES)
    raise ValueError(f"unsupported SQL backend DSN {dsn!r}; expected one of: {schemes}")


def _sqlite_path_from_dsn(dsn: str) -> str:
    path = dsn[len("sqlite:///") :]
    if path == ":memory:":
        return path
    # Windows absolute paths commonly arrive as sqlite:///D:/path/to/file.db.
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if not path:
        raise ValueError("sqlite DSN must include a database path or :memory:")
    return path


def _normalize_payload(record: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if record is None:
        return {}
    if hasattr(record, "model_dump"):
        try:
            payload = record.model_dump(mode="json")
        except TypeError:
            payload = record.model_dump()
    elif is_dataclass(record):
        payload = asdict(record)
    elif isinstance(record, Mapping):
        payload = dict(record)
    else:
        raise TypeError(f"unsupported payload type: {type(record).__name__}")
    return _from_json(_to_json(payload))


def _require_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or str(value) == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _to_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=_json_default)


def _from_json(value: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return json.loads(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    try:
        return row[key]
    except (TypeError, IndexError):
        # psycopg rows are dicts when opened through connect(); this fallback keeps
        # direct unit tests with tuple-like cursors readable.
        return row[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["SQLBackend", "connect"]
