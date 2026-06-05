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
    first_run.close()

    second_run = EntityGraphStore(db_path=db_path)
    try:
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
    finally:
        second_run.close()


def test_entity_graph_persists_assets_observations_and_query_apis(tmp_path):
    from storage.entity_graph import EntityGraphStore

    db_path = tmp_path / "persistent-graph.db"
    first = EntityGraphStore(db_path=db_path)
    obs1 = first.add_observation(
        {"entity_type": "contact", "entity_value": "TG:core01", "normalized_value": "tg:core01"},
        {"trace_id": "run-1", "source_name": "tg-a", "source_type": "IM", "publish_time": "2026-05-30T00:00:00+00:00"},
    )
    first.close()
    second = EntityGraphStore(db_path=db_path)
    try:
        obs2 = second.add_observation(
            {"entity_type": "contact", "entity_value": "TG:core01", "normalized_value": "tg:core01"},
            {"trace_id": "run-2", "source_name": "forum-b", "source_type": "Forum", "publish_time": "2026-05-31T00:00:00+00:00"},
        )

        assert obs1.entity_id == obs2.entity_id
        assert len(second.observations_for_entity(obs1.entity_id)) == 2
        assert second.cross_source_entities(min_sources=2)[0].entity_id == obs1.entity_id
        assert second.entities_seen_since(days=7)
        assert second.related_clues(obs1.entity_id)
        neighborhood = second.neighborhood(obs1.entity_id, depth=1)
        assert neighborhood["entities"][0]["entity_id"] == obs1.entity_id
        assert {item["trace_id"] for item in neighborhood["observations"]} == {"run-1", "run-2"}
    finally:
        second.close()


def test_entity_graph_close_releases_sqlite_file_for_windows_cleanup(tmp_path):
    from storage.entity_graph import EntityGraphStore

    db_path = tmp_path / "close-check.db"
    graph = EntityGraphStore(db_path=db_path)
    graph.add_observation(
        {"entity_type": "contact", "entity_value": "TG:close01", "normalized_value": "tg:close01"},
        {"trace_id": "close-run", "source_name": "tg-a", "source_type": "IM"},
    )

    graph.close()
    db_path.unlink()
    assert not db_path.exists()


def test_entity_graph_generates_risk_profiled_tool_trade_cluster(tmp_path):
    from storage.entity_graph import EntityGraphStore

    graph = EntityGraphStore(db_path=tmp_path / "risk-profile.db")
    try:
        records = [
            {
                "trace_id": "risk-a",
                "source_name": "tg-risk",
                "source_type": "IM",
                "risk_category": "工具交易",
                "publish_time": "2026-05-30T00:00:00+00:00",
            },
            {
                "trace_id": "risk-b",
                "source_name": "forum-risk",
                "source_type": "Forum",
                "classification": {"final": {"risk_category": "工具交易"}},
                "publish_time": "2026-05-31T00:00:00+00:00",
            },
        ]
        contact_ids = []
        for record in records:
            contact_ids.append(
                graph.add_observation(
                    {"entity_type": "contact", "entity_value": "TG:risk01", "normalized_value": "tg:risk01"},
                    record,
                ).entity_id
            )
            tool = graph.add_observation(
                {"entity_type": "tool_name", "entity_value": "群控脚本", "normalized_value": "群控脚本"},
                record,
            )
            graph.add_relation(contact_ids[-1], tool.entity_id, "CO_OCCURS_IN_RECORD", evidence_trace_ids=[record["trace_id"]])

        clues = graph.generate_clues()
        tool_cluster = next(item for item in clues if item["clue_type"] == "entity_graph_tool_trade_cluster")

        assert tool_cluster["risk_category"] == "工具交易"
        assert tool_cluster["risk_score"] >= 60
        assert tool_cluster["key_entity_id"] == contact_ids[0]
        assert tool_cluster["related_entity_ids"]
        assert set(tool_cluster["evidence_trace_ids"]) == {"risk-a", "risk-b"}
        assert set(tool_cluster["evidence_observation_ids"]) == set(tool_cluster["entity_observation_refs"])
        assert tool_cluster["risk_profile"]["risk_categories"]["工具交易"] == 2
    finally:
        graph.close()


def test_entity_graph_persists_observation_risk_categories_for_clues(tmp_path):
    from storage.entity_graph import EntityGraphStore

    db_path = tmp_path / "risk-profile-persisted.db"
    first = EntityGraphStore(db_path=db_path)
    try:
        for trace_id, source_name in (("risk-persist-a", "tg-risk"), ("risk-persist-b", "forum-risk")):
            contact = first.add_observation(
                {"entity_type": "contact", "entity_value": "TG:riskpersist", "normalized_value": "tg:riskpersist"},
                {
                    "trace_id": trace_id,
                    "source_name": source_name,
                    "source_type": "IM" if source_name.startswith("tg") else "Forum",
                    "risk_category": "工具交易",
                },
            )
            tool = first.add_observation(
                {"entity_type": "tool_name", "entity_value": "群控脚本", "normalized_value": "群控脚本"},
                {"trace_id": trace_id, "source_name": source_name, "risk_category": "工具交易"},
            )
            first.add_relation(contact.entity_id, tool.entity_id, "CO_OCCURS_IN_RECORD", evidence_trace_ids=[trace_id])
    finally:
        first.close()

    second = EntityGraphStore(db_path=db_path)
    try:
        contact_entity = next(item for item in second.cross_source_entities() if item.entity_type == "contact")
        profile = second.risk_profile(contact_entity.entity_id)
        clues = second.generate_clues()
        tool_cluster = next(item for item in clues if item["clue_type"] == "entity_graph_tool_trade_cluster")

        assert profile.risk_categories["工具交易"] == 2
        assert tool_cluster["risk_category"] == "工具交易"
        assert tool_cluster["risk_profile"]["risk_categories"]["工具交易"] == 2
    finally:
        second.close()
