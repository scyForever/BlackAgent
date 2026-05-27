from datetime import datetime, timedelta, timezone

from src.scheduling.layered_collection import (
    LAYER_CLUE_BUILD,
    LAYER_FAST,
    LAYER_SLOW,
    LayeredIntervalConfig,
    LayeredRunPlanner,
    PendingClueBatch,
    build_candidate_clues_from_raw_rows,
    should_run_clue_build,
    source_candidates_from_rows,
)


def test_layered_run_planner_splits_fast_slow_and_clue_intervals():
    planner = LayeredRunPlanner(
        LayeredIntervalConfig(
            fast_interval_seconds=30,
            slow_interval_seconds=300,
            clue_build_interval_seconds=120,
        )
    )
    start = datetime(2026, 5, 27, 0, 0, tzinfo=timezone.utc)

    assert planner.due_layers(now=start) == [LAYER_FAST, LAYER_SLOW, LAYER_CLUE_BUILD]

    for layer in (LAYER_FAST, LAYER_SLOW, LAYER_CLUE_BUILD):
        planner.mark_ran(layer, when=start)

    assert planner.due_layers(now=start + timedelta(seconds=29)) == []
    assert planner.due_layers(now=start + timedelta(seconds=31)) == [LAYER_FAST]
    assert planner.is_due(LAYER_CLUE_BUILD, now=start + timedelta(seconds=121)) is True
    assert planner.is_due(LAYER_SLOW, now=start + timedelta(seconds=299)) is False
    assert planner.is_due(LAYER_SLOW, now=start + timedelta(seconds=301)) is True


def test_pending_clue_batch_dedupes_by_trace_id_and_drains():
    batch = PendingClueBatch()

    added = batch.add_rows(
        [
            {"trace_id": "r1", "content_text": "first"},
            {"trace_id": "r2", "content_text": "second"},
            {"trace_id": "r1", "content_text": "first-updated"},
        ]
    )

    assert added == 2
    assert batch.count() == 2
    drained = batch.drain(limit=1)
    assert len(drained) == 1
    assert batch.count() == 1
    remaining = batch.drain()
    assert len(remaining) == 1
    assert batch.count() == 0


def test_batch_clue_build_reuses_raw_rows_as_source_candidates():
    rows = [
        {
            "trace_id": "builder-1",
            "source_name": "tg-authorized-a",
            "source_type": "IM",
            "source_url": "https://t.me/core01",
            "legal_basis": "AUTHORIZED_PARTNER",
            "publish_time": "2026-05-23T01:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
        },
        {
            "trace_id": "builder-2",
            "source_name": "forum-authorized-b",
            "source_type": "Forum",
            "source_url": "https://forum.example/post/1",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "publish_time": "2026-05-23T02:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
        },
        {
            "trace_id": "builder-3",
            "source_name": "feed-authorized-c",
            "source_type": "THREAT_INTEL",
            "source_url": "https://intel.example/feed/1",
            "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
            "publish_time": "2026-05-23T03:00:00+00:00",
            "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
        },
    ]

    candidates = source_candidates_from_rows(rows)
    result = build_candidate_clues_from_raw_rows(
        rows,
        quality_profile="high_precision",
        require_cross_source=True,
    )

    assert len(candidates) == 3
    assert result.saved_clue_count >= 2
    assert any(clue.get("first_seen") for clue in result.clues)
    assert any(clue.get("last_seen") for clue in result.clues)


def test_should_run_clue_build_only_when_due_or_collection_ran_with_pending_rows():
    assert should_run_clue_build(pending_count=0, collection_layer_ran=True, clue_layer_due=True) is False
    assert should_run_clue_build(pending_count=2, collection_layer_ran=True, clue_layer_due=False) is True
    assert should_run_clue_build(pending_count=2, collection_layer_ran=False, clue_layer_due=True) is True
    assert should_run_clue_build(pending_count=2, collection_layer_ran=False, clue_layer_due=False) is False
