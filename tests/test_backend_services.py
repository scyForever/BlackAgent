import json
import urllib.error

from src.agent import BudgetController, RuntimeBudget
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


def test_llm_gateway_failure_after_reserve_updates_failed_budget_ledger(monkeypatch):
    def raise_http_error(*_args, **_kwargs):
        body = type("Body", (), {"read": lambda self: b'{"error":"rate"}', "close": lambda self: None})()
        raise urllib.error.HTTPError(
            url="https://llm.example/v1/chat/completions",
            code=429,
            msg="too many requests",
            hdrs=None,
            fp=body,
        )

    monkeypatch.setattr(llm_gateway.urllib_request, "urlopen", raise_http_error)
    gateway = LLMGateway(base_url="https://llm.example/v1", api_key="sk-test", model="real-model", dry_run=False)
    budget = BudgetController(RuntimeBudget(max_llm_calls=2, max_llm_tokens=2000))

    response = gateway.chat(
        [{"role": "user", "content": "hello"}],
        stage="llm_classify",
        max_tokens=16,
        budget=budget,
    )
    ledger = budget.snapshot()["llm_budget"]

    assert response.ok is False
    assert response.error == "http_error:429"
    assert response.network_attempted is True
    assert ledger["attempted_calls"] == 1
    assert ledger["allowed_calls"] == 1
    assert ledger["failed_calls"] == 1
    assert ledger["network_calls"] == 1


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


def test_llm_gateway_supports_provider_specific_headers_and_payload(monkeypatch):
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
                    "id": "chatcmpl-provider-test",
                    "model": "mimo-v2.5-pro",
                    "choices": [{"message": {"role": "assistant", "content": json.dumps({"pong": True})}}],
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
        base_url="https://api.xiaomimimo.com/v1",
        api_key="mimo-test",
        model="mimo-v2.5-pro",
        dry_run=False,
        auth_header="api-key",
        max_tokens_param="max_completion_tokens",
        extra_body={"thinking": {"type": "disabled"}},
    )

    response = gateway.chat([{"role": "user", "content": "ping"}], max_tokens=32)

    assert captured["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert captured["headers"]["Api-key"] == "mimo-test"
    assert "Authorization" not in captured["headers"]
    assert captured["body"]["max_completion_tokens"] == 32
    assert "max_tokens" not in captured["body"]
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert response.ok is True
    assert response.parsed_json == {"pong": True}


def test_llm_gateway_can_omit_response_format_for_unsupported_models(monkeypatch):
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
                    "id": "chatcmpl-no-response-format",
                    "model": "provider-model",
                    "choices": [{"message": {"role": "assistant", "content": json.dumps({"ok": True})}}],
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse()

    monkeypatch.setattr(llm_gateway.urllib_request, "urlopen", fake_urlopen)

    gateway = LLMGateway(
        base_url="https://llm.example/v1",
        api_key="sk-test",
        model="provider-model",
        dry_run=False,
        response_format_supported=False,
    )

    response = gateway.chat([{"role": "user", "content": "return json"}], response_format={"type": "json_object"})

    assert "response_format" not in captured["body"]
    assert response.ok is True
    assert response.parsed_json == {"ok": True}


def test_llm_gateway_extracts_json_from_fenced_response(monkeypatch):
    class FakeHTTPResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "chatcmpl-fenced-json",
                    "model": "provider-model",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "```json\n{\"search_query\":\"site:t.me/s 接码 群控\"}\n```",
                            }
                        }
                    ],
                }
            ).encode("utf-8")

    monkeypatch.setattr(llm_gateway.urllib_request, "urlopen", lambda request, timeout: FakeHTTPResponse())

    gateway = LLMGateway(
        base_url="https://llm.example/v1",
        api_key="sk-test",
        model="provider-model",
        dry_run=False,
        response_format_supported=False,
    )

    response = gateway.chat([{"role": "user", "content": "rewrite query"}], response_format={"type": "json_object"})

    assert response.ok is True
    assert response.parsed_json == {"search_query": "site:t.me/s 接码 群控"}


def test_llm_gateway_timeout_returns_normalized_error(monkeypatch):
    def fake_urlopen(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm_gateway.urllib_request, "urlopen", fake_urlopen)

    gateway = LLMGateway(base_url="https://llm.example/v1", api_key="sk-test", model="adapter-model", dry_run=False)
    response = gateway.chat([{"role": "user", "content": "hello"}])

    assert response.ok is False
    assert response.network_attempted is True
    assert response.error.startswith("timeout:")


def test_llm_gateway_records_stage_stats_uses_cache_and_respects_budget():
    gateway = LLMGateway(
        base_url="https://llm.invalid/v1",
        api_key=None,
        model="mock-risk-model",
        mock=True,
    )
    budget = BudgetController(RuntimeBudget(max_llm_calls=1, max_llm_tokens=200, max_llm_refine_clues=1))
    messages = [{"role": "user", "content": "提取风险实体并返回 JSON"}]

    first = gateway.chat(
        messages,
        stage="clue_refine",
        max_tokens=16,
        budget=budget,
        cache_policy="read_write",
        deadline_ms=1000,
    )
    second = gateway.chat(
        messages,
        stage="clue_refine",
        max_tokens=16,
        budget=budget,
        cache_policy="read_write",
        deadline_ms=1000,
    )
    denied = gateway.chat(
        [{"role": "user", "content": "another uncached call"}],
        stage="clue_refine",
        max_tokens=16,
        budget=budget,
        cache_policy="none",
    )

    stats = gateway.stats()
    assert first.ok is True
    assert second.ok is True
    assert second.raw["cache_hit"] is True
    assert denied.ok is False
    assert denied.error == "budget_exhausted"
    assert stats[0]["stage"] == "clue_refine"
    assert stats[1]["cache_hit"] is True
    assert stats[2]["error"] == "budget_exhausted"
