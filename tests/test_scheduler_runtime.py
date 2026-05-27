from datetime import datetime, timezone

from src.scheduling import ACTIVE_CLUE_BUILD_DEDUPE_KEY, CollectionQueueScheduler, CronExpression, ScheduleDefinition
from src.scheduling.layered_collection import LAYER_CLUE_BUILD, LAYER_FAST
from storage.sql_backend import connect


def sqlite_dsn(db_path):
    return f"sqlite:///{db_path.as_posix()}"


def test_cron_expression_resolves_next_matching_time():
    expr = CronExpression("*/15 9,10 * * 1-5")
    start = datetime(2026, 5, 27, 9, 7, tzinfo=timezone.utc)  # Wednesday

    next_run = expr.next_after(start)

    assert next_run == datetime(2026, 5, 27, 9, 15, tzinfo=timezone.utc)
    assert expr.matches(datetime(2026, 5, 27, 10, 45, tzinfo=timezone.utc)) is True
    assert expr.matches(datetime(2026, 5, 30, 10, 45, tzinfo=timezone.utc)) is False


def test_collection_queue_scheduler_enqueues_followup_clue_build_and_processes_batch(tmp_path):
    db_path = tmp_path / "scheduler.db"
    backend = connect(sqlite_dsn(db_path))
    backend.create_schema()

    def fake_runner(command):
        if "x_recent_search_collector.py" in " ".join(command):
            rows = [
                {
                    "hash_id": "raw-hash-1",
                    "trace_id": "trace-1",
                    "source_type": "IM",
                    "source_name": "tg-authorized-a",
                    "source_url": "https://t.me/core01",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "publish_time": "2026-05-23T01:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
                },
                {
                    "hash_id": "raw-hash-2",
                    "trace_id": "trace-2",
                    "source_type": "Forum",
                    "source_name": "forum-authorized-b",
                    "source_url": "https://forum.example/post/1",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "publish_time": "2026-05-23T02:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第二条",
                },
                {
                    "hash_id": "raw-hash-3",
                    "trace_id": "trace-3",
                    "source_type": "THREAT_INTEL",
                    "source_name": "feed-authorized-c",
                    "source_url": "https://intel.example/feed/1",
                    "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
                    "publish_time": "2026-05-23T03:00:00+00:00",
                    "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
                },
            ]
            for row in rows:
                backend.save_raw(row)
            return {"status": "completed", "persisted_count": len(rows), "command": command}
        return {"status": "completed", "command": command}

    scheduler = CollectionQueueScheduler(
        backend,
        runner=fake_runner,
        start_immediately=True,
        default_worker_count=2,
        claim_limit_per_worker=1,
        max_claim_rounds=4,
        clue_batch_limit=50,
    )
    scheduler.sync_schedules(
        [
            ScheduleDefinition(
                schedule_name="fast_x_collect",
                task_type="collect_x_recent",
                layer=LAYER_FAST,
                task_payload={"config": "config/x_watch.example.yaml"},
                interval_seconds=60,
                priority=100,
                dedupe_key="schedule:fast_x_collect",
            ),
            ScheduleDefinition(
                schedule_name="scheduled_clue_build",
                task_type="build_candidate_clues",
                layer=LAYER_CLUE_BUILD,
                task_payload={
                    "quality_profile": "high_precision",
                    "require_cross_source": True,
                    "require_evidence_chain": True,
                    "batch_limit": 50,
                },
                interval_seconds=180,
                priority=80,
                dedupe_key=ACTIVE_CLUE_BUILD_DEDUPE_KEY,
            ),
        ],
        now=datetime(2026, 5, 27, 0, 0, tzinfo=timezone.utc),
    )

    tick_result = scheduler.tick(now=datetime(2026, 5, 27, 0, 0, tzinfo=timezone.utc))
    worker_result = scheduler.run_workers(worker_count=2, claim_limit=1, max_rounds=4)

    assert tick_result["due_count"] == 2
    assert worker_result["claimed_count"] >= 2
    assert backend.count_clue_batch_items(status="PENDING") == 0
    assert len(backend.list_clues()) >= 1
    active_jobs = backend.list_queue_jobs(dedupe_key=ACTIVE_CLUE_BUILD_DEDUPE_KEY)
    assert any(job["status"] == "SUCCEEDED" for job in active_jobs)
    assert any(item["status"] == "SUCCEEDED" for item in worker_result["executed"])

    backend.close()
