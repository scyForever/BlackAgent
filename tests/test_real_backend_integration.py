from fastapi.testclient import TestClient

from main import create_app
from src.config_loader import Settings
from storage.sql_backend import connect


def backend_records():
    return [
        {
            "trace_id": "svc-r1",
            "source_name": "tg-svc-a",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": "2026-05-23T01:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
        },
        {
            "trace_id": "svc-r2",
            "source_name": "forum-svc-b",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": "2026-05-23T02:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
        },
        {
            "trace_id": "svc-r3",
            "source_name": "feed-svc-c",
            "source_type": "THREAT_INTEL",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": "2026-05-23T03:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
        },
    ]


def make_settings(tmp_path):
    return Settings(
        storage={"backend": "sql", "dsn": f"sqlite:///{(tmp_path / 'blackagent_service.db').as_posix()}", "auto_create_schema": True},
        tasks={"backend": "local", "persist": True},
        llm={"provider": "mock", "enabled": False, "model": "mock-backend", "dry_run": True},
    )


def test_real_backend_task_sql_and_llm_integration(tmp_path):
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings))

    status = client.get("/api/v1/system/backend")
    assert status.status_code == 200
    assert status.json()["storage_connected"] is True
    assert status.json()["storage_backend"] == "sql"

    submit = client.post(
        "/api/v1/tasks/pipeline/advanced",
        json={
            "fixture_items": backend_records(),
            "prompt_text": "Return JSON with confidence evidence requires_human_review",
        },
    )
    assert submit.status_code == 200
    task_id = submit.json()["task_id"]
    assert submit.json()["task_status"] == "PENDING"

    before = client.get(f"/api/v1/tasks/{task_id}").json()
    assert before["task"]["status"] == "PENDING"

    run = client.post("/api/v1/tasks/run-pending")
    assert run.status_code == 200
    assert run.json()["tasks"][0]["status"] == "SUCCEEDED"
    assert run.json()["tasks"][0]["result"]["risk_clue_count"] >= 2
    assert run.json()["tasks"][0]["result"]["playbook_count"] == 1

    llm = client.post("/api/v1/llm/chat", json={"messages": [{"role": "user", "content": "返回 JSON"}]})
    assert llm.status_code == 200
    assert llm.json()["ok"] is True
    assert llm.json()["network_attempted"] is False
    assert llm.json()["parsed_json"]["model"] == "mock-backend"

    backend = connect(settings.storage.dsn)
    backend.create_schema()
    assert len(backend.list_raw()) == 3
    assert len(backend.list_entities()) > 0
    assert backend.list_tasks(status="SUCCEEDED")[0]["task_id"] == task_id
    assert backend.list_audit(event_type="candidate_strategy_generated")
    backend.close()

    # New app instance can still read task state from SQL when local memory is empty.
    fresh_client = TestClient(create_app(settings))
    persisted = fresh_client.get(f"/api/v1/tasks/{task_id}")
    assert persisted.status_code == 200
    assert persisted.json()["source"] == "sql"
    assert persisted.json()["task"]["status"] == "SUCCEEDED"
