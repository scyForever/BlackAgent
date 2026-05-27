import json

from src.backend import LLMGateway, TaskBackend, TaskStatus
from src.backend import llm_gateway


def test_task_backend_runs_pending_task_and_records_success():
    backend = TaskBackend()

    submitted = backend.submit(
        "double",
        {"value": 21},
        handler=lambda payload: {"value": payload["value"] * 2},
    )

    assert submitted.status == TaskStatus.PENDING
    assert backend.get(submitted.task_id).status == TaskStatus.PENDING

    completed = backend.run_pending()

    assert len(completed) == 1
    assert completed[0].status == TaskStatus.SUCCEEDED
    assert completed[0].result == {"value": 42}
    assert completed[0].attempts == 1
    assert backend.get(submitted.task_id).status == TaskStatus.SUCCEEDED
    assert backend.list(status="SUCCEEDED")[0].task_id == submitted.task_id
    assert backend.get(submitted.task_id).model_dump()["status"] == "SUCCEEDED"


def test_task_backend_records_failure_without_crashing_queue():
    backend = TaskBackend()

    def fail(_payload):
        raise RuntimeError("boom from worker")

    failed = backend.submit("fail", {"trace_id": "t1"}, handler=fail)
    ok = backend.submit("ok", {"trace_id": "t2"}, handler=lambda payload: payload)

    results = backend.run_pending()

    failed_record = backend.get(failed.task_id)
    ok_record = backend.get(ok.task_id)
    assert [item.status for item in results] == [TaskStatus.FAILED, TaskStatus.SUCCEEDED]
    assert failed_record.status == TaskStatus.FAILED
    assert failed_record.error.error_type == "RuntimeError"
    assert "boom from worker" in failed_record.error.message
    assert failed_record.result is None
    assert ok_record.status == TaskStatus.SUCCEEDED


def test_llm_gateway_mock_mode_returns_deterministic_json_without_network():
    gateway = LLMGateway(
        base_url="https://llm.invalid/v1",
        api_key=None,
        model="mock-risk-model",
        service_tier="flex",
        mock=True,
    )
    messages = [{"role": "user", "content": "提取风险实体并返回 JSON"}]

    first = gateway.chat(messages)
    second = gateway.chat(messages)

    assert first.ok is True
    assert first.network_attempted is False
    assert first.content == second.content
    assert first.parsed_json["mock"] is True
    assert first.parsed_json["model"] == "mock-risk-model"
    assert first.parsed_json["service_tier"] == "flex"
    assert first.parsed_json["requires_human_review"] is True


def test_llm_gateway_missing_api_key_does_not_attempt_network(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("urlopen must not be called without an API key")

    monkeypatch.setattr(llm_gateway.urllib_request, "urlopen", fail_if_called)

    gateway = LLMGateway(
        base_url="http://127.0.0.1:9/v1",
        api_key="",
        model="real-model",
        dry_run=False,
        mock=False,
    )

    response = gateway.chat([{"role": "user", "content": "hello"}])

    assert response.ok is False
    assert response.error == "missing_api_key"
    assert response.network_attempted is False
    assert response.parsed_json["error"] == "missing_api_key"


def test_llm_gateway_builds_openai_compatible_urllib_request(monkeypatch):
    captured = {}

    class FakeHTTPResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "chatcmpl-test",
                    "model": "adapter-model",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"answer": "ok"}),
                            }
                        }
                    ],
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse()

    monkeypatch.setattr(llm_gateway.urllib_request, "urlopen", fake_urlopen)

    gateway = LLMGateway(
        base_url="https://llm.example/v1",
        api_key="sk-test",
        model="adapter-model",
        service_tier="default",
        dry_run=False,
    )

    response = gateway.chat(
        [{"role": "system", "content": "Return JSON"}, {"role": "user", "content": "ping"}],
        response_format={"type": "json_object"},
        max_tokens=64,
    )

    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["timeout"] == 30.0
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"]["model"] == "adapter-model"
    assert captured["body"]["service_tier"] == "default"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["max_tokens"] == 64
    assert response.ok is True
    assert response.network_attempted is True
    assert response.parsed_json == {"answer": "ok"}
