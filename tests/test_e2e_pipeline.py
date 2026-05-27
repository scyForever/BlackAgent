from src.agent.agent_orchestrator import AgentOrchestrator


def test_e2e_high_confidence_standard_path_and_unknown_sandbox_path():
    entity_repo = []
    review_repo = []
    orchestrator = AgentOrchestrator(
        entity_repo=entity_repo,
        review_repo=review_repo,
        history=[{"trace_id": "hist-1", "text": "历史样本提到音符暗号和拉新"}],
    )

    result = orchestrator.run_pipeline(
        [
            {
                "trace_id": "high-1",
                "content_text": "出售接码平台 https://risk.example/path 联系 tg_alpha001",
            },
            {
                "trace_id": "low-1",
                "content_text": "音符暗号新变体，今晚上车，具体含义待研判",
            },
        ]
    )

    assert result.standard_count == 1
    assert result.sandbox_count == 1
    assert result.review_count == 1
    assert result.entity_count >= 2
    assert len(entity_repo) == result.entity_count
    assert len(review_repo) == 1

    high_item = next(item for item in result.items if item.source_trace_id == "high-1")
    low_item = next(item for item in result.items if item.source_trace_id == "low-1")

    assert high_item.route == "standard"
    assert low_item.route == "sandbox"
    assert low_item.reason in {"low_confidence", "unknown_label", "anomalous_slang", "classification_review_required"}
    assert low_item.hypothesis is not None
    assert low_item.hypothesis.requires_human_review is True
    assert low_item.hypothesis.source_trace_id == "low-1"
    assert low_item.hypothesis.budget_consumed.rounds <= 3
    assert all(item.get("source_trace_id") != "low-1" for item in entity_repo)


def test_e2e_unknown_classifier_result_never_writes_entity_repo():
    entity_repo = []
    review_repo = []

    class UnknownClassifier:
        def classify(self, cleaned):
            return {"risk_category": "unknown", "confidence": 0.9, "review_required": True}

    orchestrator = AgentOrchestrator(
        classifier=UnknownClassifier(),
        entity_repo=entity_repo,
        review_repo=review_repo,
    )

    result = orchestrator.run_pipeline({"trace_id": "unknown-1", "content_text": "未知黑话但包含 https://unknown.example"})

    assert result.standard_count == 0
    assert result.sandbox_count == 1
    assert entity_repo == []
    assert len(review_repo) == 1
    assert review_repo[0].requires_human_review is True
