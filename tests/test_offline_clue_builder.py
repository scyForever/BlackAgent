from types import SimpleNamespace

from src.pipeline import OfflineClueBuilder
from storage import InMemoryClueRepo


def test_offline_clue_builder_persists_candidate_clues_to_repo():
    repo = InMemoryClueRepo()
    builder = OfflineClueBuilder(clue_repo=repo)
    result = builder.build(
        [
            {
                "trace_id": "builder-1",
                "source_name": "tg-authorized-a",
                "source_type": "IM",
                "legal_basis": "AUTHORIZED_PARTNER",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
            },
            {
                "trace_id": "builder-2",
                "source_name": "forum-authorized-b",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
            },
            {
                "trace_id": "builder-3",
                "source_name": "feed-authorized-c",
                "source_type": "THREAT_INTEL",
                "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
            },
        ],
        quality_profile="high_precision",
        require_cross_source=True,
    )

    assert result.saved_clue_count >= 2
    assert len(repo.list()) >= 2
    assert repo.list()[0]["clue_id"].startswith("clue_")
    assert result.execution_summary["pipeline_backend"] == "intelligence_pipeline"
    assert result.execution_summary["fallback_backend"] is None
    reviewability = repo.list()[0]["evidence_reviewability"]
    assert reviewability["source_count"] >= 2
    assert reviewability["entity_support_count"] >= 1
    assert reviewability["original_snippets"]
    assert repo.list()[0]["evidence_cards"] == reviewability["evidence_cards"]
    assert reviewability["evidence_cards"]
    assert {
        "trace_id",
        "source_name",
        "source_type",
        "publish_time",
        "raw_snippet",
        "clean_text",
        "classification",
        "entities",
    } <= set(reviewability["evidence_cards"][0])
    assert reviewability["evidence_cards"][0]["classification"]["risk_category"]
    assert reviewability["evidence_cards"][0]["entities"]
    assert reviewability["time_range"]["start"] == "2026-05-23T01:00:00+00:00"
    assert reviewability["time_range"]["end"] == "2026-05-23T03:00:00+00:00"


def test_offline_clue_builder_does_not_fallback_to_phase_engine_when_pipeline_has_no_clues():
    class _FailIfRun:
        def __init__(self) -> None:
            from src.enhancement.engine import PhaseTwoThreeEngine

            engine = PhaseTwoThreeEngine()
            self.playbook_builder = engine.playbook_builder
            self.strategy_planner = engine.strategy_planner
            self._last_run_payload = None

        def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("PhaseTwoThreeEngine.run must not be used as fallback")

    builder = OfflineClueBuilder(phase_engine=_FailIfRun())
    result = builder.build(
        [
            {
                "trace_id": "no-clue-1",
                "source_name": "public",
                "source_type": "Forum",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "content_text": "普通安全研究复盘，不提供任何联系方式。",
            }
        ],
        require_evidence_chain=False,
    )

    assert result.status == "completed"
    assert result.saved_clue_count == 0
    assert result.execution_summary["pipeline_backend"] == "intelligence_pipeline"
    assert result.execution_summary["fallback_backend"] is None
    assert result.execution_summary["no_clue_reason"] == "aggregation_threshold_not_met"


def test_offline_clue_builder_persists_archived_weak_clues_to_review_pool():
    class _Summary(dict):
        def model_dump(self):
            return dict(self)

    class _Pipeline:
        def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return SimpleNamespace(
                execution_summary=_Summary(
                    cleaned_count=2,
                    classified_count=2,
                    entity_count=2,
                    clue_count=1,
                    candidate_clue_count=2,
                    actionable_clue_count=1,
                    archived_weak_clue_count=1,
                ),
                classified=[
                    {"source_trace_id": "strong-a", "risk_category": "工具交易", "confidence": 0.9},
                    {"source_trace_id": "weak-a", "risk_category": "工具交易", "confidence": 0.5},
                ],
                entities=[
                    {"source_trace_id": "strong-a", "entity_type": "contact", "normalized_value": "Telegram:core01"},
                    {"source_trace_id": "strong-b", "entity_type": "contact", "normalized_value": "Telegram:core01"},
                ],
                clues=[
                    {
                        "clue_id": "actionable-contact",
                        "clue_stage": "actionable",
                        "clue_type": "shared_contact_48h",
                        "key": "Telegram:core01",
                        "risk_category": "工具交易",
                        "evidence_trace_ids": ["strong-a", "strong-b"],
                        "source_names": ["tg-a", "forum-b"],
                        "entity_values": ["Telegram:core01"],
                        "confidence": 0.9,
                    }
                ],
                archived_weak_clues=[
                    {
                        "clue_id": "archived-template-contact",
                        "clue_stage": "archived_weak",
                        "archive_reason": "bulk_template_shared_contact_48h_represented_by_top_evidence",
                        "clue_type": "shared_contact_48h",
                        "key": "Telegram:weak01",
                        "risk_category": "工具交易",
                        "evidence_trace_ids": ["weak-a"],
                        "source_names": ["tg-weak"],
                        "entity_values": ["Telegram:weak01"],
                        "confidence": 0.3,
                    }
                ],
            )

    repo = InMemoryClueRepo()
    builder = OfflineClueBuilder(clue_repo=repo)
    builder.intelligence_pipeline = _Pipeline()

    result = builder.build(
        [
            {
                "trace_id": "strong-a",
                "source_name": "tg-a",
                "source_type": "IM",
                "publish_time": "2026-05-23T01:00:00+00:00",
                "content_text": "群控脚本联系 Telegram:core01",
            },
            {
                "trace_id": "strong-b",
                "source_name": "forum-b",
                "source_type": "Forum",
                "publish_time": "2026-05-23T02:00:00+00:00",
                "content_text": "论坛复核 Telegram:core01",
            },
            {
                "trace_id": "weak-a",
                "source_name": "tg-weak",
                "source_type": "IM",
                "publish_time": "2026-05-23T03:00:00+00:00",
                "content_text": "模板化弱线索 Telegram:weak01",
            },
        ],
        require_cross_source=True,
    )

    rows = {item["clue_id"]: item for item in repo.list()}

    assert result.saved_clue_count == 2
    assert result.high_quality_count == 1
    assert result.candidate_count == 1
    assert rows["actionable-contact"]["clue_stage"] == "actionable"
    assert rows["archived-template-contact"]["clue_stage"] == "archived_weak"
    assert rows["archived-template-contact"]["archive_reason"] == "bulk_template_shared_contact_48h_represented_by_top_evidence"
