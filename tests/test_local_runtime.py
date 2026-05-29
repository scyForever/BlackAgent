from typing import Any

from src.collector import load_source_catalog
from src.config_loader import PROJECT_ROOT, Settings
from src.local_runtime import LocalAgentRuntime


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _test_settings(overrides: dict[str, Any] | None = None) -> Settings:
    payload: dict[str, Any] = {
        "llm": {
            "provider": "mock",
            "enabled": False,
            "model": "mock-test",
            "dry_run": True,
        }
    }
    if overrides:
        payload = _deep_merge(payload, overrides)
    return Settings.model_validate(payload)


def _runtime(overrides: dict[str, Any] | None = None) -> LocalAgentRuntime:
    return LocalAgentRuntime(_test_settings(overrides))


def _risk_records(prefix: str = "rt") -> list[dict[str, Any]]:
    return [
        {
            "trace_id": f"{prefix}-1",
            "source_name": "tg-authorized-a",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": "2026-05-23T01:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
        },
        {
            "trace_id": f"{prefix}-2",
            "source_name": "forum-authorized-b",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": "2026-05-23T02:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
        },
        {
            "trace_id": f"{prefix}-3",
            "source_name": "feed-authorized-c",
            "source_type": "THREAT_INTEL",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": "2026-05-23T03:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
        },
    ]


def test_local_runtime_has_no_public_api_surface():
    runtime = _runtime()
    try:
        assert runtime.backend_status()["status"] == "ok"
        assert not hasattr(runtime, "create_app")
    finally:
        runtime.close()


def test_local_runtime_runs_llm_driven_investigation():
    runtime = _runtime()
    try:
        payload = runtime.run_investigation(
            "帮我找近24小时诈骗引流相关的高质量线索，优先 Telegram 和论坛，输出可复核的线索卡片",
            fixture_items=_risk_records("inv"),
        )
    finally:
        runtime.close()

    assert payload["mode"] == "llm_driven_investigation"
    assert payload["intent"]["goal"] == "collect_high_quality_risk_clues"
    assert payload["investigation_plan"]["agent_steps"][0]["agent"] == "intent_planner"
    assert payload["input_count"] == 3
    assert payload["high_quality_count"] >= 1
    assert payload["execution_summary"]["mode"] == "investigation_processing"
    assert payload["execution_summary"]["risk_clue_count"] >= 2
    assert payload["execution_summary"]["refined_clue_count"] >= 1
    assert payload["high_quality_clues"][0]["quality"]["pass_threshold"] is True


def test_local_runtime_prefers_existing_clue_pool_before_reprocessing():
    runtime = _runtime()
    try:
        seed = runtime.run_advanced_pipeline(_risk_records("pool"))
        assert seed["risk_clue_count"] >= 2

        payload = runtime.run_investigation("帮我找诈骗引流高质量线索，优先 telegram")
    finally:
        runtime.close()

    assert payload["execution_summary"]["mode"] == "candidate_clue_retrieval"
    assert payload["execution_summary"]["status"] == "retrieved_from_clue_pool"
    assert payload["execution_summary"]["refined_clue_count"] >= 1


def test_local_runtime_supports_budget_policy_override():
    runtime = _runtime()
    try:
        payload = runtime.run_investigation(
            "帮我找近24小时诈骗引流相关的高质量线索",
            fixture_items=_risk_records("budget"),
            sources=[
                {
                    "source_name": "budget-source-a",
                    "source_type": "IM",
                    "source_url": "https://feed.example/a",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "query_theme": "诈骗引流",
                    "search_query": "site:t.me/s 私域导流",
                },
                {
                    "source_name": "budget-source-b",
                    "source_type": "Forum",
                    "source_url": "https://feed.example/b",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "query_theme": "诈骗引流",
                    "search_query": "site:t.me/s 加v",
                },
            ],
            policy_override={
                "max_sources": 1,
                "max_raw_records": 2,
                "max_candidate_clues": 1,
                "max_llm_refine_clues": 1,
            },
        )
    finally:
        runtime.close()

    assert payload["selected_source_count"] == 1
    assert payload["input_count"] == 2
    assert payload["execution_summary"]["budget"]["max_sources"] == 1
    assert payload["execution_summary"]["budget"]["max_raw_records"] == 2
    assert payload["execution_summary"]["budget"]["max_candidate_clues"] == 1
    assert payload["execution_summary"]["budget"]["max_llm_refine_clues"] == 1


def test_local_runtime_builds_clues_and_runs_local_tasks():
    runtime = _runtime()
    try:
        build = runtime.build_clues(
            _risk_records("build"),
            quality_profile="high_precision",
            require_cross_source=True,
        )
        assert build["saved_clue_count"] >= 2

        investigation = runtime.run_investigation(
            "找近24小时诈骗引流高质量线索",
            time_range_hours=24 * 365 * 3,
            source_types=["im"],
            min_quality_score=0.7,
        )
        assert investigation["execution_summary"]["mode"] == "candidate_clue_retrieval"

        task_submit = runtime.submit_build_clues_task(_risk_records("build-task"))
        assert task_submit["task_status"] == "PENDING"
        run_pending = runtime.run_pending_tasks()
    finally:
        runtime.close()

    assert run_pending["status"] == "ok"
    assert run_pending["tasks"][0]["status"] == "SUCCEEDED"


def test_local_scheduler_bootstrap_tick_and_status(tmp_path):
    scheduler_db = tmp_path / "scheduler.db"
    runtime = _runtime(
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
    try:
        bootstrap = runtime.scheduler_bootstrap()
        tick = runtime.scheduler_tick()
        status = runtime.scheduler_status()
    finally:
        runtime.close()

    assert bootstrap["schedule_count"] >= 5
    assert tick["due_count"] >= 5
    assert tick["enqueued_count"] >= 5
    assert status["schedule_count"] >= 5
    assert status["pending_jobs"] >= 5


def test_local_runtime_defaults_to_all_authorized_sources(monkeypatch, tmp_path):
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
    runtime = _runtime({"network": {"enabled": True, "max_records_per_fetch": 1}})
    try:
        payload = runtime.run_investigation(
            "取一下当天诈骗引流相关的线索信息",
            sources=load_source_catalog(catalog_path),
        )
    finally:
        runtime.close()

    assert payload["selected_source_count"] == 6
    assert payload["input_count"] == 6
    assert payload["execution_summary"]["budget"]["max_sources"] == 6


def test_importing_main_does_not_create_web_app():
    import main

    assert not hasattr(main, "app")
    assert not hasattr(main, "create_app")
    assert callable(main.main)


def test_public_api_dependencies_are_not_imported():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert "fastapi" not in pyproject
    assert "uvicorn" not in pyproject
