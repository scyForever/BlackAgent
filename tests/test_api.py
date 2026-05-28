from fastapi.testclient import TestClient

from main import create_app
from src.collector import load_source_catalog
from src.config_loader import Settings


def test_health_endpoint_returns_prd_mode():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "mode": "llm_driven_investigation",
        "year": 2026,
    }


def test_legacy_agent_orchestrator_routes_are_not_exposed():
    client = TestClient(create_app())

    assert client.post("/api/v1/pipeline/run", json={"content_text": "legacy"}).status_code == 404
    assert client.get("/api/v1/review/tasks").status_code == 404
    assert client.get("/api/v1/review/audit").status_code == 404


def test_investigation_endpoint_uses_llm_driven_plan_and_returns_high_quality_clues():
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/investigations/run",
        json={
            "query": "帮我找近24小时诈骗引流相关的高质量线索，优先 Telegram 和论坛，输出可复核的线索卡片",
            "fixture_items": [
                {
                    "trace_id": "inv-1",
                    "source_name": "tg-authorized-a",
                    "source_type": "IM",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "publish_time": "2026-05-23T01:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
                },
                {
                    "trace_id": "inv-2",
                    "source_name": "forum-authorized-b",
                    "source_type": "Forum",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-23T02:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
                },
                {
                    "trace_id": "inv-3",
                    "source_name": "feed-authorized-c",
                    "source_type": "THREAT_INTEL",
                    "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                    "publish_time": "2026-05-23T03:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
                },
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "llm_driven_investigation"
    assert payload["intent"]["goal"] == "collect_high_quality_risk_clues"
    assert payload["investigation_plan"]["agent_steps"][0]["agent"] == "intent_planner"
    assert len(payload["llm_traces"]) >= 2
    assert payload["input_count"] == 3
    assert payload["high_quality_count"] >= 1
    assert payload["execution_summary"]["mode"] == "investigation_processing"
    assert payload["execution_summary"]["risk_clue_count"] >= 2
    assert payload["execution_summary"]["refined_clue_count"] >= 1
    assert payload["execution_summary"]["budget"]["max_llm_refine_clues"] >= 1
    assert payload["high_quality_clues"][0]["quality"]["pass_threshold"] is True
    assert "refinement" in payload["high_quality_clues"][0]


def test_investigation_endpoint_prefers_existing_clue_pool_before_reprocessing():
    client = TestClient(create_app())

    seed = client.post(
        "/api/v1/pipeline/advanced/run",
        json={
            "fixture_items": [
                {
                    "trace_id": "pool-1",
                    "source_name": "tg-authorized-a",
                    "source_type": "IM",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "publish_time": "2026-05-23T01:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
                },
                {
                    "trace_id": "pool-2",
                    "source_name": "forum-authorized-b",
                    "source_type": "Forum",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-23T02:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
                },
                {
                    "trace_id": "pool-3",
                    "source_name": "feed-authorized-c",
                    "source_type": "THREAT_INTEL",
                    "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                    "publish_time": "2026-05-23T03:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
                },
            ]
        },
    )
    assert seed.status_code == 200

    response = client.post(
        "/api/v1/investigations/run",
        json={"query": "帮我找诈骗引流高质量线索，优先 telegram"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["execution_summary"]["mode"] == "candidate_clue_retrieval"
    assert payload["execution_summary"]["status"] == "retrieved_from_clue_pool"
    assert payload["execution_summary"]["refined_clue_count"] >= 1


def test_clue_build_endpoint_and_task_seed_candidate_pool():
    client = TestClient(create_app())

    build_response = client.post(
        "/api/v1/clues/build",
        json={
            "fixture_items": [
                {
                    "trace_id": "build-api-1",
                    "source_name": "tg-authorized-a",
                    "source_type": "IM",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "publish_time": "2026-05-23T01:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
                },
                {
                    "trace_id": "build-api-2",
                    "source_name": "forum-authorized-b",
                    "source_type": "Forum",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-23T02:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
                },
                {
                    "trace_id": "build-api-3",
                    "source_name": "feed-authorized-c",
                    "source_type": "THREAT_INTEL",
                    "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                    "publish_time": "2026-05-23T03:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
                },
            ],
            "quality_profile": "high_precision",
            "require_cross_source": True,
        },
    )
    assert build_response.status_code == 200
    assert build_response.json()["saved_clue_count"] >= 2

    investigation_response = client.post(
        "/api/v1/investigations/run",
        json={
            "query": "找近24小时诈骗引流高质量线索",
            "time_range_hours": 24 * 365 * 3,
            "source_types": ["im"],
            "min_quality_score": 0.7,
        },
    )
    assert investigation_response.status_code == 200
    assert investigation_response.json()["execution_summary"]["mode"] == "candidate_clue_retrieval"

    task_submit = client.post(
        "/api/v1/tasks/clues/build",
        json={
            "fixture_items": [
                {
                    "trace_id": "build-task-1",
                    "source_name": "tg-authorized-a",
                    "source_type": "IM",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "publish_time": "2026-05-23T01:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core02，落地 https://risk2.example/path，音符暗号 第一条",
                },
                {
                    "trace_id": "build-task-2",
                    "source_name": "forum-authorized-b",
                    "source_type": "Forum",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-23T02:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core02，落地 https://risk2.example/path，音符暗号 第二条",
                },
                {
                    "trace_id": "build-task-3",
                    "source_name": "feed-authorized-c",
                    "source_type": "THREAT_INTEL",
                    "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                    "publish_time": "2026-05-23T03:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core02，落地 https://risk2.example/path，音符暗号 第三条",
                },
            ]
        },
    )
    assert task_submit.status_code == 200
    run_pending = client.post("/api/v1/tasks/run-pending")
    assert run_pending.status_code == 200


def test_scheduler_bootstrap_tick_and_status_endpoints(tmp_path):
    scheduler_db = tmp_path / "scheduler.db"
    settings = Settings.model_validate(
        {
            "storage": {"backend": "memory"},
            "scheduler": {
                "enabled": True,
                "dsn": f"sqlite:///{scheduler_db.as_posix()}",
                "start_immediately": True,
                "worker_count": 2,
                "claim_limit_per_worker": 1,
                "max_claim_rounds": 2,
            },
        }
    )
    client = TestClient(create_app(settings))

    bootstrap = client.post("/api/v1/scheduler/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["schedule_count"] >= 5

    tick = client.post("/api/v1/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["due_count"] >= 5
    assert tick.json()["enqueued_count"] >= 5

    status_response = client.get("/api/v1/scheduler/status")
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["schedule_count"] >= 5
    assert payload["pending_jobs"] >= 5


def test_investigation_endpoint_defaults_to_all_authorized_sources(monkeypatch, tmp_path):
    catalog_path = tmp_path / "intel_sources.test.yaml"
    catalog_path.write_text(
        """
sources:
  - source_name: fraud-feed
    query_url_template: https://feed.example/search?q={query}
    query_seed_terms: [site:test]
    query_themes: [诈骗引流]
    query_term_limit: 6
    legal_basis: PUBLIC_COMPLIANT_DATA
    allowed_domain: feed.example
    include_themes: [诈骗引流]
        """.strip(),
        encoding="utf-8",
    )

    bodies: dict[str, tuple[str, str]] = {
        "https://feed.example/search?q=site%3Atest%20%E7%A7%81%E5%9F%9F%E5%AF%BC%E6%B5%81": (
            '<html><body><p>私域导流 TG:traffic01 https://risk.example/1</p></body></html>',
            "text/html; charset=utf-8",
        ),
        "https://feed.example/search?q=site%3Atest%20%E5%8A%A0v": (
            '<html><body><p>加v 拉群 TG:traffic02 https://risk.example/2</p></body></html>',
            "text/html; charset=utf-8",
        ),
        "https://feed.example/search?q=site%3Atest%20%E6%8B%89%E7%BE%A4": (
            '<html><body><p>拉群 高佣 TG:traffic03 https://risk.example/3</p></body></html>',
            "text/html; charset=utf-8",
        ),
        "https://feed.example/search?q=site%3Atest%20%E9%AB%98%E4%BD%A3": (
            '<html><body><p>高佣 引流 TG:traffic04 https://risk.example/4</p></body></html>',
            "text/html; charset=utf-8",
        ),
        "https://feed.example/search?q=site%3Atest%20%E5%8A%A0%E8%96%87": (
            '<html><body><p>加薇 进裙 TG:traffic05 https://risk.example/5</p></body></html>',
            "text/html; charset=utf-8",
        ),
        "https://feed.example/search?q=site%3Atest%20%E2%9E%95v": (
            '<html><body><p>➕V 小飞机 TG:traffic06 https://risk.example/6</p></body></html>',
            "text/html; charset=utf-8",
        ),
    }

    class _FakeHTTPResponse:
        def __init__(self, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            self._body = body.encode("utf-8")
            self.headers = {"Content-Type": content_type}
            self.status = 200

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _open(request, timeout):
        body, content_type = bodies[request.full_url]
        return _FakeHTTPResponse(body, content_type)

    monkeypatch.setattr("src.collector.http_feed_collector.urllib_request.urlopen", _open)
    settings = Settings.model_validate({"network": {"enabled": True, "max_records_per_fetch": 1}})
    client = TestClient(create_app(settings))
    sources = load_source_catalog(catalog_path)

    response = client.post(
        "/api/v1/investigations/run",
        json={
            "query": "取一下当天诈骗引流相关的线索信息",
            "sources": sources,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_source_count"] == 6
    assert payload["input_count"] == 6
    assert payload["execution_summary"]["budget"]["max_sources"] == 6


def test_investigation_endpoint_continues_when_one_source_fetch_fails(monkeypatch):
    settings = Settings.model_validate({"network": {"enabled": True, "max_records_per_fetch": 1}})
    client = TestClient(create_app(settings))

    sources = [
        {
            "source_name": "good-feed",
            "source_type": "IM",
            "source_url": "https://feed.example/good",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "allowed_domains": ["feed.example"],
            "include_themes": ["诈骗引流"],
            "query_theme": "诈骗引流",
            "search_query": "site:test 私域导流",
        },
        {
            "source_name": "bad-feed",
            "source_type": "IM",
            "source_url": "https://feed.example/bad",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "allowed_domains": ["feed.example"],
            "include_themes": ["诈骗引流"],
            "query_theme": "诈骗引流",
            "search_query": "site:test 加v",
        },
    ]

    class _FakeHTTPResponse:
        def __init__(self, body: str) -> None:
            self._body = body.encode("utf-8")
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.status = 200

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _open(request, timeout):
        if request.full_url.endswith("/bad"):
            raise RuntimeError("http_error:429")
        return _FakeHTTPResponse('<html><body><p>私域导流 TG:traffic01 https://risk.example/1</p></body></html>')

    monkeypatch.setattr("src.collector.http_feed_collector.urllib_request.urlopen", _open)

    response = client.post(
        "/api/v1/investigations/run",
        json={
            "query": "取一下当天诈骗引流相关的线索信息",
            "sources": sources,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_source_count"] == 2
    assert payload["input_count"] == 1
    assert any((item.get("error") or "").startswith("http_error:429") for item in payload["collection_runs"])
