from __future__ import annotations

from scripts.run_classification_extraction_phase import _load_phase_rows
from storage.schemas import CleanedText, LegalBasis, RawIntelligence
from storage.sql_backend import connect


def sqlite_dsn(db_path):
    return f"sqlite:///{db_path.as_posix()}"


def test_classification_phase_can_consume_cleaned_texts_and_high_risk_subset(tmp_path):
    db_path = tmp_path / "classification-phase.db"
    backend = connect(sqlite_dsn(db_path))
    backend.create_schema()

    backend.save_raw(
        RawIntelligence(
            hash_id="raw-phase-1",
            trace_id="00000000-0000-0000-0000-000000000401",
            source_type="IM",
            source_name="authorized-hi",
            legal_basis=LegalBasis.AUTHORIZED_PARTNER,
            content_text="接码平台继续放单，支持批量注册，联系 TG:captcha01，落地 https://risk.example/captcha",
        )
    )
    backend.save_raw(
        RawIntelligence(
            hash_id="raw-phase-2",
            trace_id="00000000-0000-0000-0000-000000000402",
            source_type="Forum",
            source_name="authorized-low",
            legal_basis=LegalBasis.PUBLIC_COMPLIANT_DATA,
            content_text="普通教程集合，欢迎收藏。",
        )
    )

    backend.save_cleaned(
        CleanedText(
            source_trace_id="00000000-0000-0000-0000-000000000401",
            clean_text="接码平台继续放单，支持批量注册，联系 TG:captcha01，落地 https://risk.example/captcha",
            noise_score=0.02,
            quality_score=0.91,
            risk_score=0.88,
            risk_level="CRITICAL",
            risk_categories=["接码注册"],
            risk_markers=["接码", "contact_handle", "destination_url"],
        )
    )
    backend.save_cleaned(
        CleanedText(
            source_trace_id="00000000-0000-0000-0000-000000000402",
            clean_text="普通教程集合，欢迎收藏。",
            noise_score=0.05,
            quality_score=0.42,
            risk_score=0.12,
            risk_level="LOW",
            risk_categories=[],
            risk_markers=[],
        )
    )

    rows, resolved_source, source_total_count = _load_phase_rows(
        backend,
        source="cleaned",
        high_risk_only=True,
        min_quality_score=0.8,
    )
    backend.close()

    assert resolved_source == "cleaned"
    assert source_total_count == 2
    assert len(rows) == 1
    assert rows[0]["source_name"] == "authorized-hi"
    assert rows[0]["legal_basis"] == "AUTHORIZED_PARTNER"
    assert rows[0]["risk_level"] == "CRITICAL"
    assert rows[0]["clean_text"].startswith("接码平台继续放单")
