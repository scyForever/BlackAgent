from __future__ import annotations

import json
import sys

from scripts import export_delivery_corpora
from scripts.export_delivery_corpora import (
    _annotate_row,
    _query_stage_lookup,
    build_acceptance_pack_sample,
    build_source_evidence_pack_sample,
    build_quota_sample,
)
from src.enhancement.source_intake import MultimodalTextExtractor
from storage.sql_backend import connect


def sqlite_dsn(db_path):
    return f"sqlite:///{db_path.as_posix()}"


def raw_row(prefix: str, idx: int, *, source_name: str, source_type: str) -> dict[str, str]:
    return {
        "hash_id": f"{prefix}-{idx}",
        "trace_id": f"{prefix}-{idx}",
        "source_name": source_name,
        "source_type": source_type,
        "legal_basis": "AUTHORIZED_PARTNER",
        "content_text": "接码平台继续放单，联系 TG:captcha01",
    }


def acceptance_row(prefix: str, idx: int, *, source_name: str, source_type: str, source_class: str | None = None) -> dict[str, str]:
    row = raw_row(prefix, idx, source_name=source_name, source_type=source_type)
    if source_class:
        row["source_class"] = source_class
    return row


def test_export_delivery_corpora_can_infer_variant_stage_and_special_signals():
    annotated = _annotate_row(
        {
            "trace_id": "export-1",
            "source_name": "variant-source",
            "query_theme": "诈骗引流",
            "query_term": "加薇",
            "content_text": "截图文案：➕V 后拉裙，继续引流。",
            "images": [{"image_url": "https://img.example/poster.png", "ocr_text": "海报写着 联系 TG:plane007"}],
            "screenshot_ref": "screenshots/poster-1.png",
        },
        stage_lookup=_query_stage_lookup(),
        multimodal_extractor=MultimodalTextExtractor(),
    )

    assert annotated["query_term_stage"] == "variant"
    assert annotated["query_term_stage_inferred"] is True
    assert "variant_or_homophone_normalized" in annotated["special_signal_types"]
    assert "emoji_marker" in annotated["special_signal_types"]
    assert "multimodal_text" in annotated["special_signal_types"]
    assert annotated["multimodal_signal_count"] >= 1
    assert "images.ocr_text" in annotated["multimodal_text_sources"]
    assert "images.image_url" in annotated["multimodal_reference_fields"]
    assert "screenshot_ref" in annotated["multimodal_reference_fields"]
    assert annotated["content_hash"]
    assert annotated["source_snapshot_id"].startswith("variant-source:")
    assert annotated["source_access_type"] == "manual_upload"
    assert annotated["collection_quality"]["quality_version"] == "collection_quality_v1"


def test_acceptance_pack_selects_balanced_300_records_across_required_source_categories():
    rows = (
        [
            acceptance_row("article", idx, source_name="wechat-public-account", source_type="public_account")
            for idx in range(80)
        ]
        + [
            acceptance_row("secondhand", idx, source_name="xianyu-secondhand-market", source_type="marketplace")
            for idx in range(80)
        ]
        + [
            acceptance_row("crowd", idx, source_name="crowdsourcing-task-platform", source_type="task_platform")
            for idx in range(80)
        ]
        + [
            acceptance_row("technical", idx, source_name="technical-forum", source_type="technical")
            for idx in range(80)
        ]
    )

    sample = build_acceptance_pack_sample(rows, include_trace_ids=True)
    selected_category_counts = {item["category"]: item["count"] for item in sample["selected_category_counts"]}
    available_category_counts = {item["category"]: item["count"] for item in sample["available_category_counts"]}

    assert sample["status"] == "completed"
    assert sample["selected_count"] == 300
    assert sample["target_record_range"] == {"min": 300, "max": 500}
    assert selected_category_counts == {
        "public_account_or_article": 75,
        "secondhand_market": 75,
        "crowdsourcing_platform": 75,
        "technical_or_forum": 75,
    }
    assert available_category_counts == {
        "public_account_or_article": 80,
        "secondhand_market": 80,
        "crowdsourcing_platform": 80,
        "technical_or_forum": 80,
    }
    assert len(sample["selected_trace_ids"]) == 300
    assert sample["warnings"] == []


def test_acceptance_pack_can_require_evidence_ready_trace_ids():
    rows = (
        [
            acceptance_row("article", idx, source_name="wechat-public-account", source_type="public_account")
            for idx in range(80)
        ]
        + [
            acceptance_row("secondhand", idx, source_name="xianyu-secondhand-market", source_type="marketplace")
            for idx in range(80)
        ]
        + [
            acceptance_row("crowd", idx, source_name="crowdsourcing-task-platform", source_type="task_platform")
            for idx in range(80)
        ]
        + [
            acceptance_row("technical", idx, source_name="technical-forum", source_type="technical")
            for idx in range(80)
        ]
    )
    evidence_ready = {
        row["trace_id"]
        for row in rows
        if not (row["trace_id"].startswith("secondhand-") and int(row["trace_id"].rsplit("-", 1)[1]) < 5)
    }

    sample = build_acceptance_pack_sample(rows, include_trace_ids=True, required_trace_ids=evidence_ready)
    selected_category_counts = {item["category"]: item["count"] for item in sample["selected_category_counts"]}
    available_category_counts = {item["category"]: item["count"] for item in sample["available_category_counts"]}

    assert sample["status"] == "completed"
    assert sample["selected_count"] == 300
    assert sample["evidence_ready_trace_filter"] == {
        "enabled": True,
        "required_trace_count": 315,
        "excluded_without_required_trace": 5,
    }
    assert selected_category_counts == {
        "public_account_or_article": 75,
        "secondhand_market": 75,
        "crowdsourcing_platform": 75,
        "technical_or_forum": 75,
    }
    assert available_category_counts["secondhand_market"] == 75
    assert all(trace_id in evidence_ready for trace_id in sample["selected_trace_ids"])


def test_acceptance_pack_reports_insufficiency_without_claiming_300_records():
    rows = [
        acceptance_row("article", idx, source_name="wechat-public-account", source_type="public_account")
        for idx in range(40)
    ] + [
        acceptance_row("technical", idx, source_name="technical-forum", source_type="technical")
        for idx in range(90)
    ]

    sample = build_acceptance_pack_sample(rows)
    selected_category_counts = {item["category"]: item["count"] for item in sample["selected_category_counts"]}
    available_category_counts = {item["category"]: item["count"] for item in sample["available_category_counts"]}

    assert sample["status"] == "insufficient_records"
    assert sample["selected_count"] == 130
    assert sample["target_record_range"] == {"min": 300, "max": 500}
    assert selected_category_counts == {
        "public_account_or_article": 40,
        "secondhand_market": 0,
        "crowdsourcing_platform": 0,
        "technical_or_forum": 90,
    }
    assert available_category_counts["public_account_or_article"] == 40
    assert available_category_counts["technical_or_forum"] == 90
    assert sample["claim_boundary"] == "insufficient_records_exported_for_audit_not_300_record_acceptance"
    assert any("acceptance_pack_total_below_minimum" in warning for warning in sample["warnings"])
    assert any("secondhand_market_insufficient" in warning for warning in sample["warnings"])
    assert any("crowdsourcing_platform_insufficient" in warning for warning in sample["warnings"])


def test_source_evidence_pack_balances_im_article_social_and_vertical_categories():
    rows = (
        [
            acceptance_row("im", idx, source_name="telegram-public-group", source_type="IM")
            for idx in range(80)
        ]
        + [
            {
                **acceptance_row("article", idx, source_name="wechat-public-account", source_type="Article"),
                "platform": "wechat_public",
            }
            for idx in range(80)
        ]
        + [
            acceptance_row("forum", idx, source_name="tieba-public-forum", source_type="Forum")
            for idx in range(80)
        ]
        + [
            acceptance_row("vertical", idx, source_name="vertical-security-market", source_type="Vertical")
            for idx in range(80)
        ]
    )

    sample = build_source_evidence_pack_sample(rows, include_trace_ids=True)
    selected_category_counts = {item["category"]: item["count"] for item in sample["selected_category_counts"]}

    assert sample["status"] == "completed"
    assert sample["pack_version"] == "source_evidence_pack_v2"
    assert sample["selected_count"] == 300
    assert selected_category_counts == {
        "im_or_group": 75,
        "public_account_or_article": 75,
        "social_or_forum": 75,
        "vertical_or_technical": 75,
    }
    assert len(sample["selected_trace_ids"]) == 300
    assert sample["warnings"] == []


def test_source_evidence_pack_insufficient_selection_keeps_available_non_im_categories():
    rows = (
        [
            acceptance_row("im", idx, source_name="telegram-public-group", source_type="IM")
            for idx in range(600)
        ]
        + [
            acceptance_row("forum", idx, source_name="tieba-public-forum", source_type="Forum")
            for idx in range(80)
        ]
        + [
            acceptance_row("vertical", idx, source_name="vertical-security-market", source_type="Vertical")
            for idx in range(20)
        ]
    )

    sample = build_source_evidence_pack_sample(rows, include_trace_ids=True)
    selected_category_counts = {item["category"]: item["count"] for item in sample["selected_category_counts"]}

    assert sample["status"] == "insufficient_records"
    assert sample["selected_count"] == 500
    assert selected_category_counts["social_or_forum"] > 0
    assert selected_category_counts["vertical_or_technical"] > 0
    assert selected_category_counts["im_or_group"] < 500
    assert any("public_account_or_article_insufficient" in warning for warning in sample["warnings"])


def test_quota_sample_caps_single_source_and_reports_underfilled_classes():
    rows = [
        {"trace_id": f"im-{idx}", "source_name": "telegram_public_delivery:big", "source_type": "IM", "content_text": "接码"}
        for idx in range(10)
    ] + [
        {"trace_id": "forum-1", "source_name": "tieba", "source_type": "Forum", "content_text": "接码"},
        {"trace_id": "vertical-1", "source_name": "market", "source_type": "Vertical", "content_text": "接码"},
    ]

    sample = build_quota_sample(rows)

    assert sample["selected_count"] == 5
    assert any(item["source_name"] == "telegram_public_delivery:big" and item["count"] == 3 for item in sample["source_counts"])
    assert any("social_or_forum_quota_underfilled" in warning for warning in sample["warnings"])
    assert any("vertical_or_technical_quota_underfilled" in warning for warning in sample["warnings"])


def test_strict_quota_sample_caps_dominant_im_class_and_retains_forum_and_vertical():
    rows = [
        raw_row("im", idx, source_name="telegram_public_delivery:big", source_type="IM")
        for idx in range(100)
    ] + [
        raw_row("forum", idx, source_name="public-forum", source_type="Forum")
        for idx in range(30)
    ] + [
        raw_row("vertical", idx, source_name="vertical-market", source_type="Vertical")
        for idx in range(20)
    ]

    sample = build_quota_sample(
        rows,
        strict_balance=True,
        min_class_count=20,
        max_class_share=0.45,
        include_trace_ids=True,
    )

    class_counts = {item["source_class"]: item["count"] for item in sample["class_counts"]}
    source_counts = {item["source_name"]: item["count"] for item in sample["source_counts"]}

    assert sample["strict_balance"] is True
    assert sample["selected_count"] == 71
    assert class_counts["im_or_group"] == 21
    assert class_counts["social_or_forum"] == 30
    assert class_counts["vertical_or_technical"] == 20
    assert source_counts["telegram_public_delivery:big"] == 21
    assert source_counts["telegram_public_delivery:big"] <= int(sample["selected_count"] * 0.30)
    assert any("public-forum_source_share_cap_infeasible" in warning for warning in sample["warnings"])
    assert max(class_counts.values()) / sample["selected_count"] <= 0.45
    assert len(sample["selected_trace_ids"]) == 71


def test_source_class_for_record_preserves_existing_structured_class():
    row = {
        "source_class": "vertical_or_technical",
        "source_name": "telegram_automation_market",
        "source_type": "IM",
        "platform": "telegram",
    }

    assert export_delivery_corpora.source_class_for_record(row) == "vertical_or_technical"


def test_quota_sample_source_cap_is_relative_to_selected_sample():
    rows = [
        raw_row("im", idx, source_name="telegram_public_delivery:big", source_type="IM")
        for idx in range(300)
    ] + [
        raw_row("forum", idx, source_name="public-forum", source_type="Forum")
        for idx in range(30)
    ] + [
        raw_row("vertical", idx, source_name="vertical-market", source_type="Vertical")
        for idx in range(30)
    ]

    sample = build_quota_sample(
        rows,
        strict_balance=True,
        min_class_count=20,
        max_class_share=0.45,
        max_source_share=0.30,
    )
    source_counts = {item["source_name"]: item["count"] for item in sample["source_counts"]}

    assert source_counts["telegram_public_delivery:big"] <= int(sample["selected_count"] * 0.30)
    assert source_counts["telegram_public_delivery:big"] == 25
    assert source_counts["public-forum"] == 30
    assert source_counts["vertical-market"] == 30
    assert any("source_share_cap_infeasible" in warning for warning in sample["warnings"])


def test_strict_quota_trims_dominant_class_after_other_classes_underfill_source_caps():
    rows = [
        raw_row(f"im-{source_idx}", idx, source_name=f"telegram_public_delivery:{source_idx}", source_type="IM")
        for source_idx in range(4)
        for idx in range(25)
    ] + [
        raw_row("forum", idx, source_name="public-forum", source_type="Forum")
        for idx in range(30)
    ] + [
        raw_row("vertical", idx, source_name="vertical-market", source_type="Vertical")
        for idx in range(20)
    ]

    sample = build_quota_sample(
        rows,
        strict_balance=True,
        min_class_count=20,
        max_class_share=0.45,
        max_source_share=0.10,
    )

    class_counts = {item["source_class"]: item["count"] for item in sample["class_counts"]}

    assert sample["selected_count"] == 54
    assert class_counts["im_or_group"] == 24
    assert class_counts["social_or_forum"] == 15
    assert class_counts["vertical_or_technical"] == 15
    assert max(class_counts.values()) / sample["selected_count"] <= 0.45


def test_main_manifest_exposes_existing_and_strict_defense_quota_samples(tmp_path, monkeypatch):
    db_path = tmp_path / "delivery.db"
    raw_out = tmp_path / "raw.jsonl"
    quota_out = tmp_path / "quota.jsonl"
    defense_quota_out = tmp_path / "defense-quota.jsonl"
    acceptance_pack_out = tmp_path / "acceptance-pack.jsonl"
    source_evidence_pack_out = tmp_path / "source-evidence-pack.jsonl"
    manifest_out = tmp_path / "manifest.json"

    backend = connect(sqlite_dsn(db_path))
    backend.create_schema()
    rows = [
        raw_row("im", idx, source_name="telegram_public_delivery:big", source_type="IM")
        for idx in range(100)
    ] + [
        raw_row("forum", idx, source_name="public-forum", source_type="Forum")
        for idx in range(30)
    ] + [
        raw_row("vertical", idx, source_name="vertical-market", source_type="Vertical")
        for idx in range(20)
    ]
    for row in rows:
        backend.save_raw(row)
    backend.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_delivery_corpora.py",
            "--db",
            str(db_path),
            "--raw-jsonl-out",
            str(raw_out),
            "--quota-jsonl-out",
            str(quota_out),
            "--defense-quota-jsonl-out",
            str(defense_quota_out),
            "--acceptance-pack-jsonl-out",
            str(acceptance_pack_out),
            "--source-evidence-pack-jsonl-out",
            str(source_evidence_pack_out),
            "--manifest-out",
            str(manifest_out),
        ],
    )

    assert export_delivery_corpora.main() == 0

    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    defense_rows = [
        json.loads(line)
        for line in defense_quota_out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert manifest["quota_balanced_jsonl"] == str(quota_out.resolve())
    assert manifest["quota_balanced_sample"]["strict_balance"] is False
    assert manifest["defense_quota_balanced_jsonl"] == str(defense_quota_out.resolve())
    assert manifest["defense_quota_balanced_sample"]["strict_balance"] is True
    assert manifest["defense_quota_balanced_sample"]["selected_count"] == 71
    assert len(defense_rows) == 71
    assert manifest["acceptance_pack_jsonl"] == str(acceptance_pack_out.resolve())
    assert manifest["multi_source_acceptance_pack"]["status"] == "insufficient_records"
    assert manifest["multi_source_acceptance_pack"]["target_record_range"] == {"min": 300, "max": 500}
    assert manifest["multi_source_acceptance_pack"]["claim_boundary"] == "insufficient_records_exported_for_audit_not_300_record_acceptance"
    assert len(acceptance_pack_out.read_text(encoding="utf-8").splitlines()) == 50
    assert manifest["source_evidence_pack_jsonl"] == str(source_evidence_pack_out.resolve())
    assert manifest["multi_source_evidence_pack"]["pack_version"] == "source_evidence_pack_v2"
    assert manifest["multi_source_evidence_pack"]["target_categories"] == [
        "im_or_group",
        "public_account_or_article",
        "social_or_forum",
        "vertical_or_technical",
    ]
    assert len(source_evidence_pack_out.read_text(encoding="utf-8").splitlines()) == manifest["multi_source_evidence_pack"]["selected_count"]
