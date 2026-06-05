from __future__ import annotations

from scripts.run_cleaning_phase import run_cleaning_phase
from storage.schemas import LegalBasis, RawIntelligence
from storage.sql_backend import connect


def sqlite_dsn(db_path):
    return f"sqlite:///{db_path.as_posix()}"


def test_run_cleaning_phase_outputs_high_risk_corpus_and_persists_cleaned_rows(tmp_path):
    db_path = tmp_path / "cleaning-phase.db"
    backend = connect(sqlite_dsn(db_path))
    backend.create_schema()

    backend.save_raw(
        RawIntelligence(
            hash_id="clean-raw-1",
            trace_id="00000000-0000-0000-0000-000000000301",
            source_type="IM",
            source_name="authorized-a",
            legal_basis=LegalBasis.AUTHORIZED_PARTNER,
            content_text="接码平台继续放单，支持批量注册，联系 TG:captcha01，落地 https://risk.example/captcha",
        )
    )
    backend.save_raw(
        RawIntelligence(
            hash_id="clean-raw-2",
            trace_id="00000000-0000-0000-0000-000000000302",
            source_type="IM",
            source_name="authorized-b",
            legal_basis=LegalBasis.AUTHORIZED_PARTNER,
            content_text="接码平台继续放单 支持批量注册 联系 TG:captcha01 落地 https://risk.example/captcha !!!",
        )
    )
    backend.save_raw(
        RawIntelligence(
            hash_id="clean-raw-3",
            trace_id="00000000-0000-0000-0000-000000000303",
            source_type="Forum",
            source_name="public-guide",
            legal_basis=LegalBasis.PUBLIC_COMPLIANT_DATA,
            content_text="分享2025年8个实用的接码平台使用指南，推荐收藏，帮助你快速注册和选择建议。",
        )
    )

    cleaned_rows, high_risk_rows, summary = run_cleaning_phase(backend.list_raw(), persist_backend=backend)
    persisted_rows = backend.list_cleaned()
    backend.close()

    assert summary["input_count"] == 3
    assert summary["cleaned_count"] == 1
    assert summary["high_risk_count"] == 1
    assert summary["duplicate_drop_count"] == 1
    assert any(item["reason"] == "generic_guide_noise" and item["count"] == 1 for item in summary["drop_reason_counts"])
    assert len(high_risk_rows) == 1
    assert high_risk_rows[0]["risk_level"] in {"HIGH", "CRITICAL"}
    assert high_risk_rows[0]["quality_score"] > 0.7
    assert len(persisted_rows) == 1
    assert persisted_rows[0]["source_trace_id"] == cleaned_rows[0]["source_trace_id"]


def test_run_cleaning_phase_materializes_multimodal_text_before_cleaning() -> None:
    rows = [
        {
            "trace_id": "mm-clean-1",
            "source_name": "tg-mm",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "content_text": "主贴：普通招募文案",
            "attachments": [{"ocr_text": "海报OCR：接码平台继续放单，联系 TG:ocr9001"}],
            "screenshots": [{"screenshot_ref": "poster-001.png", "alt_text": "截图：拉裙上车，➕V demo001"}],
        }
    ]

    cleaned_rows, high_risk_rows, summary = run_cleaning_phase(rows)

    assert summary["input_count"] == 1
    assert summary["multimodal_materialized_count"] == 1
    assert any(item["source"] == "attachments.ocr_text" for item in summary["multimodal_source_counts"])
    assert len(cleaned_rows) == 1
    assert cleaned_rows[0]["multimodal_text_extracted"] is True
    assert cleaned_rows[0]["multimodal_signal_count"] >= 2
    assert "attachments.ocr_text" in cleaned_rows[0]["multimodal_text_sources"]
    assert "screenshots.alt_text" in cleaned_rows[0]["multimodal_text_sources"]
    assert "screenshots.screenshot_ref" in cleaned_rows[0]["multimodal_reference_fields"]
    assert cleaned_rows[0]["multimodal_reference_count"] >= 1
    assert len(high_risk_rows) == 1


def test_run_cleaning_phase_replaces_persisted_cleaned_snapshot_instead_of_leaving_stale_rows(tmp_path) -> None:
    db_path = tmp_path / "cleaning-phase-replace.db"
    backend = connect(sqlite_dsn(db_path))
    backend.create_schema()

    first_rows = [
        {
            "trace_id": "replace-1",
            "source_name": "first",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "content_text": "接码平台继续放单，联系 TG:first001，落地 https://risk.example/1",
        },
        {
            "trace_id": "replace-2",
            "source_name": "second",
            "source_type": "IM",
            "legal_basis": "AUTHORIZED_PARTNER",
            "content_text": "刷单返佣日结，联系 TG:second001，落地 https://risk.example/2",
        },
    ]
    run_cleaning_phase(first_rows, persist_backend=backend)
    assert len(backend.list_cleaned()) == 2

    second_rows = [
        {
            "trace_id": "replace-3",
            "source_name": "third",
            "source_type": "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": "普通指南推荐收藏，不应进入清洗高质量语料。",
        }
    ]
    run_cleaning_phase(second_rows, persist_backend=backend)
    assert backend.list_cleaned() == []
    backend.close()
