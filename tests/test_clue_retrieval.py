from src.retrieval import ClueRetriever
from storage import InMemoryClueRepo


def test_clue_retriever_ranks_matching_risk_clues():
    repo = InMemoryClueRepo()
    repo.save(
        {
            "clue_id": "clue-1",
            "clue_type": "shared_contact_48h",
            "key": "tg:core01",
            "risk_category": "诈骗引流",
            "source_names": ["telegram_feed", "forum_feed"],
            "entity_values": ["tg:core01", "risk.example"],
            "confidence": 0.88,
            "quality_score": 0.83,
        }
    )
    repo.save(
        {
            "clue_id": "clue-2",
            "clue_type": "shared_domain_multi_source",
            "key": "benign.example",
            "risk_category": "普通噪声",
            "source_names": ["news_feed"],
            "entity_values": ["benign.example"],
            "confidence": 0.2,
            "quality_score": 0.1,
        }
    )

    results = ClueRetriever().retrieve(
        repo.list(),
        query="找诈骗引流 telegram 高质量线索",
        intent={
            "risk_types": ["诈骗引流"],
            "source_preferences": ["telegram"],
            "require_cross_source": True,
        },
        limit=5,
    )

    assert results[0]["clue_id"] == "clue-1"
    assert results[0]["retrieval_score"] > 0


def test_clue_retriever_applies_time_source_and_quality_filters():
    repo = InMemoryClueRepo()
    repo.save(
        {
            "clue_id": "recent-good",
            "clue_type": "shared_contact_48h",
            "key": "tg:core01",
            "risk_category": "诈骗引流",
            "source_names": ["telegram_feed"],
            "source_types": ["IM"],
            "entity_values": ["tg:core01"],
            "confidence": 0.88,
            "quality_score": 0.85,
            "last_seen": "2026-05-27T00:00:00+00:00",
        }
    )
    repo.save(
        {
            "clue_id": "old-or-low",
            "clue_type": "shared_contact_48h",
            "key": "tg:old01",
            "risk_category": "诈骗引流",
            "source_names": ["forum_feed"],
            "source_types": ["Forum"],
            "entity_values": ["tg:old01"],
            "confidence": 0.5,
            "quality_score": 0.2,
            "last_seen": "2020-05-27T00:00:00+00:00",
        }
    )

    results = ClueRetriever().retrieve(
        repo.list(),
        query="找诈骗引流 telegram 高质量线索",
        intent={"risk_types": ["诈骗引流"], "source_preferences": ["telegram"]},
        limit=5,
        time_range_hours=24 * 365 * 2,
        allowed_source_types=["im"],
        min_quality_score=0.8,
    )
    assert [item["clue_id"] for item in results] == ["recent-good"]


def test_clue_retriever_reads_cross_run_entity_graph_clues(tmp_path):
    from storage.entity_graph import EntityGraphStore

    db_path = tmp_path / "entity_graph.db"
    first_run = EntityGraphStore(db_path=db_path)
    first_run.add_observation(
        {"entity_type": "contact", "entity_value": "TG:graph01", "confidence": 0.91},
        {"trace_id": "run-a", "source_name": "telegram_feed", "source_type": "IM"},
    )

    second_run = EntityGraphStore(db_path=db_path)
    second_run.add_observation(
        {"entity_type": "contact", "entity_value": "TG:graph01", "confidence": 0.93},
        {"trace_id": "run-b", "source_name": "forum_feed", "source_type": "Forum"},
    )

    results = ClueRetriever().retrieve(
        [],
        query="找 TG graph01 跨源实体图谱线索",
        intent={"require_cross_source": True},
        entity_graph=second_run,
        limit=5,
    )

    assert results
    assert results[0]["retrieval_source"] == "entity_graph"
    assert results[0]["entity_graph_backend"] == "entity_graph_store"
    assert len(results[0]["entity_observation_refs"]) == 2
    assert set(results[0]["evidence_trace_ids"]) == {"run-a", "run-b"}
