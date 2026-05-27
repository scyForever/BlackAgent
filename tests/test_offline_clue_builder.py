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
