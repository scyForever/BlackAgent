import builtins
import sqlite3

import pytest

from storage.schemas import (
    AuditEvent,
    BudgetConsumed,
    CleanedText,
    EntityExtractionResult,
    ExplorationHypothesis,
    HypothesisType,
    LegalBasis,
    RawIntelligence,
)
from storage.sql_backend import connect


def sqlite_dsn(db_path):
    return f"sqlite:///{db_path.as_posix()}"


def test_sql_backend_sqlite_creates_tables_and_roundtrips_contracts(tmp_path):
    db_path = tmp_path / "blackagent.db"
    backend = connect(sqlite_dsn(db_path))
    backend.create_schema()

    raw = RawIntelligence(
        hash_id="raw-hash-1",
        trace_id="00000000-0000-0000-0000-000000000001",
        source_type="IM",
        source_name="authorized-fixture",
        legal_basis=LegalBasis.AUTHORIZED_PARTNER,
        content_text="群控脚本接码测试样本",
    )
    saved_raw = backend.save_raw(raw)
    assert saved_raw["hash_id"] == "raw-hash-1"
    assert backend.list_raw()[0]["content_text"] == "群控脚本接码测试样本"

    cleaned = CleanedText(
        source_trace_id=str(raw.trace_id),
        clean_text="群控脚本接码测试样本",
        noise_score=0.02,
        dedup_group_id="dedup-clean-1",
        quality_score=0.86,
        risk_score=0.78,
        risk_level="HIGH",
        risk_categories=["工具交易", "接码注册"],
        risk_markers=["群控", "接码"],
        text_entropy=2.8641,
    )
    saved_cleaned = backend.save_cleaned(cleaned)
    assert saved_cleaned["risk_level"] == "HIGH"
    assert backend.list_cleaned(risk_level="HIGH")[0]["source_trace_id"] == str(raw.trace_id)

    hypothesis = ExplorationHypothesis(
        hypothesis_id="00000000-0000-0000-0000-000000000101",
        source_trace_id=str(raw.trace_id),
        hypothesis_type=HypothesisType.NEW_SLANG_VARIANT,
        hypothesis_summary="音符暗号疑似新变体",
        supporting_evidence_ids=["e1", "e2"],
        suggested_label="unknown_slang",
        confidence=0.41,
        budget_consumed=BudgetConsumed(rounds=1, tokens=128, elapsed_ms=7),
    )
    backend.save_review(
        hypothesis,
        state={
            "hypothesis_id": str(hypothesis.hypothesis_id),
            "status": "PENDING",
            "reviewer": "analyst_a",
        },
    )
    review_rows = backend.list_review(status="PENDING")
    assert len(review_rows) == 1
    assert review_rows[0]["review_state"]["status"] == "PENDING"
    assert backend.list_review(status="REVIEWED") == []

    audit = AuditEvent(
        event_type="review_decision_recorded",
        actor="analyst_a",
        target_id=str(hypothesis.hypothesis_id),
        payload={"decision": "APPROVED", "sandbox_hypothesis_kept_review_only": True},
    )
    backend.append_audit(audit)
    audit_rows = backend.list_audit(event_type="review_decision_recorded")
    assert audit_rows[0]["payload"]["sandbox_hypothesis_kept_review_only"] is True

    entity = EntityExtractionResult(
        entity_id="00000000-0000-0000-0000-000000000201",
        source_trace_id=str(raw.trace_id),
        entity_type="tool_or_keyword",
        entity_value="接码",
        start_offset=4,
        end_offset=6,
        confidence=0.91,
        masking_status="MASKED",
    )
    backend.save_entity(entity)
    entity_rows = backend.list_entities(source_trace_id=str(raw.trace_id))
    assert len(entity_rows) == 1
    assert entity_rows[0]["entity_value"] == "接码"

    clue = {
        "clue_id": "clue-1",
        "clue_type": "shared_contact_48h",
        "risk_category": "诈骗引流",
        "quality_score": 0.82,
        "confidence": 0.91,
        "source_names": ["tg-a", "forum-b"],
    }
    backend.save_clue(clue)
    clue_rows = backend.list_clues(risk_category="诈骗引流")
    assert clue_rows[0]["clue_id"] == "clue-1"

    backend.close()

    with sqlite3.connect(db_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "raw_records",
        "cleaned_texts",
        "review_tasks",
        "audit_events",
        "candidate_clues",
        "entities",
        "task_runs",
        "scheduled_jobs",
        "queue_jobs",
        "clue_batch_items",
    }.issubset(table_names)


def test_sql_backend_persists_task_status_across_connections(tmp_path):
    db_path = tmp_path / "task-runs.db"
    dsn = sqlite_dsn(db_path)

    first = connect(dsn)
    first.create_schema()
    first.save_task(
        {
            "task_id": "pipeline-run-1",
            "task_type": "pipeline",
            "status": "RUNNING",
            "metrics": {"raw_count": 2},
        }
    )
    assert first.get_task("pipeline-run-1")["status"] == "RUNNING"
    first.close()

    second = connect(dsn)
    second.create_schema()
    assert second.get_task("pipeline-run-1")["metrics"]["raw_count"] == 2
    second.save_task("pipeline-run-1", status="COMPLETED", metrics={"raw_count": 2, "entity_count": 3})

    task = second.get_task("pipeline-run-1")
    assert task["status"] == "COMPLETED"
    assert task["created_at"] <= task["updated_at"]
    assert second.list_tasks(status="COMPLETED")[0]["task_id"] == "pipeline-run-1"
    second.close()


def test_postgresql_dsn_without_psycopg_has_clear_runtime_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError("psycopg intentionally hidden for test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="PostgreSQL DSN requires optional dependency 'psycopg'"):
        connect("postgresql://user:pass@localhost:5432/blackagent")
