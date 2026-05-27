from fastapi.testclient import TestClient

from main import create_app


def test_health_endpoint_returns_prd_mode():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "mode": "controlled_exploration",
        "year": 2026,
    }


def test_pipeline_endpoint_uses_real_orchestrator_and_persists_review_queue():
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/pipeline/run",
        json={
            "fixture_items": [
                {
                    "trace_id": "api-high-1",
                    "content_text": "出售接码平台 https://risk.example 联系 tg_api001",
                },
                {
                    "trace_id": "api-low-1",
                    "content_text": "音符暗号新变体，具体含义待研判",
                },
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["orchestrator_available"] is True
    assert payload["input_count"] == 2
    assert payload["standard_count"] == 1
    assert payload["review_count"] == 1

    review_response = client.get("/api/v1/review/tasks")

    assert review_response.status_code == 200
    review_payload = review_response.json()
    assert review_payload["count"] == 1
    assert review_payload["tasks"][0]["source_trace_id"] == "api-low-1"
    assert review_payload["tasks"][0]["requires_human_review"] is True
    assert review_payload["tasks"][0]["review_state"]["status"] == "PENDING"
    assert "priority_score" in review_payload["tasks"][0]


def test_review_workbench_ranks_decides_and_audits_without_entity_promotion():
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/pipeline/run",
        json={
            "fixture_items": [
                {
                    "trace_id": "review-medium-1",
                    "content_text": "音符暗号新变体，具体含义待研判",
                },
                {
                    "trace_id": "review-high-1",
                    "content_text": "群控脚本里出现音符暗号，需要人工研判",
                },
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["review_count"] == 2
    assert response.json()["standard_count"] == 0

    queue_response = client.get("/api/v1/review/tasks")
    assert queue_response.status_code == 200
    queue_payload = queue_response.json()
    assert queue_payload["count"] == 2
    assert queue_payload["tasks"][0]["source_trace_id"] == "review-high-1"
    assert queue_payload["tasks"][0]["priority_features"]["risk_level"] == "HIGH"

    hypothesis_id = queue_payload["tasks"][0]["hypothesis_id"]
    decision_response = client.post(
        f"/api/v1/review/tasks/{hypothesis_id}/decision",
        json={
            "decision": "approved",
            "reviewer": "analyst_a",
            "notes": "确认新黑话候选，先进入候选池，不直写正式实体库。",
            "edited_risk_type": "tool_trade",
            "secondary_label": "群控脚本",
            "corrected_entities": [{"entity_type": "tool_or_keyword", "entity_value": "群控脚本"}],
            "add_to_wordlist": True,
        },
    )

    assert decision_response.status_code == 200
    decision_payload = decision_response.json()
    assert decision_payload["decision"] == "APPROVED"
    assert decision_payload["review_state"]["status"] == "REVIEWED"
    assert decision_payload["review_state"]["edited_risk_type"] == "tool_trade"
    assert decision_payload["audit_event"]["event_type"] == "review_decision_recorded"
    assert decision_payload["audit_event"]["payload"]["sandbox_hypothesis_kept_review_only"] is True
    assert "dynamic_wordlist_candidate_pool" in decision_payload["audit_event"]["payload"]["feedback_targets"]

    pending_after_decision = client.get("/api/v1/review/tasks")
    assert pending_after_decision.status_code == 200
    assert pending_after_decision.json()["count"] == 1
    assert pending_after_decision.json()["tasks"][0]["source_trace_id"] == "review-medium-1"

    all_tasks = client.get("/api/v1/review/tasks", params={"status": "all"}).json()["tasks"]
    reviewed_task = next(task for task in all_tasks if task["hypothesis_id"] == hypothesis_id)
    assert reviewed_task["requires_human_review"] is True
    assert reviewed_task["review_state"]["decision"] == "APPROVED"

    audit_response = client.get("/api/v1/review/audit", params={"event_type": "review_decision_recorded"})
    assert audit_response.status_code == 200
    assert audit_response.json()["count"] == 1


def test_review_decision_rejects_unknown_decision():
    client = TestClient(create_app())
    client.post(
        "/api/v1/pipeline/run",
        json={"content_text": "音符暗号新变体，具体含义待研判", "source_name": "api-test"},
    )
    hypothesis_id = client.get("/api/v1/review/tasks").json()["tasks"][0]["hypothesis_id"]

    response = client.post(
        f"/api/v1/review/tasks/{hypothesis_id}/decision",
        json={"decision": "auto_ban", "reviewer": "analyst_a"},
    )

    assert response.status_code == 400


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
