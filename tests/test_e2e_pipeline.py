from src.agent import InvestigationOrchestrator
from src.backend import LLMGateway


def _records():
    return [
        {
            "trace_id": "e2e-1",
            "source_name": "tg-authorized-a",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": "2026-05-23T01:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
        },
        {
            "trace_id": "e2e-2",
            "source_name": "forum-authorized-b",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": "2026-05-23T02:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
        },
        {
            "trace_id": "e2e-3",
            "source_name": "feed-authorized-c",
            "source_type": "THREAT_INTEL",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": "2026-05-23T03:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
        },
    ]


def _orchestrator() -> InvestigationOrchestrator:
    return InvestigationOrchestrator(llm_gateway=LLMGateway(dry_run=True, mock=True))


def test_e2e_current_investigation_flow_builds_reviewable_clues():
    result = _orchestrator().run("找近24小时接码群控高质量线索", records=_records())

    assert result.status == "completed"
    assert result.mode == "llm_driven_investigation"
    assert result.input_count == 3
    assert result.execution_summary["mode"] == "investigation_processing"
    assert result.execution_summary["risk_clue_count"] >= 2
    assert result.execution_summary["strategy_count"] >= 1
    assert result.high_quality_count >= 1
    assert len(result.high_quality_clues) + len(result.candidate_clues) >= 1
    assert {trace["stage"] for trace in result.llm_traces} >= {"intent_parse", "investigation_plan", "clue_refine"}


def test_e2e_current_investigation_flow_reuses_clue_pool_before_reprocessing():
    orchestrator = _orchestrator()
    seed = orchestrator.run("找接码群控高质量线索", records=_records())
    assert seed.high_quality_count >= 1

    result = orchestrator.run("复核接码群控线索")

    assert result.status == "completed"
    assert result.input_count == 0
    assert result.execution_summary["mode"] == "candidate_clue_retrieval"
    assert result.execution_summary["status"] == "retrieved_from_clue_pool"
    assert result.high_quality_count + result.candidate_count >= 1
