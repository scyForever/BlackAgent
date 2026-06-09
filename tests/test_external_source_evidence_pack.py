from __future__ import annotations

import json

from scripts.build_external_source_evidence_pack import build_pack, source_evidence_group


def test_external_source_evidence_pack_balances_four_required_groups_and_preserves_provenance():
    rows = (
        [_row("im", idx, source_name="telegram_public", source_type="IM") for idx in range(3)]
        + [
            _row(
                "article",
                idx,
                source_name="direct_public_article",
                source_type="Article",
                platform="wechat_public",
            )
            for idx in range(3)
        ]
        + [_row("forum", idx, source_name="tieba_public", source_type="Forum") for idx in range(3)]
        + [_row("vertical", idx, source_name="direct_technical_forum", source_type="Vertical") for idx in range(3)]
    )

    pack = build_pack(rows, per_group=3)

    assert pack["report"]["status"] == "completed"
    assert pack["report"]["selected_group_counts"] == {
        "im_or_group": 3,
        "public_account_or_article": 3,
        "social_or_forum": 3,
        "vertical_or_technical": 3,
    }
    assert pack["report"]["missing_required_fields"] == 0
    assert len(pack["rows"]) == 12
    for row in pack["rows"]:
        assert row["source_url"].startswith("https://")
        assert row["crawl_time"]
        assert row["raw_payload_uri"]
        assert row["capture_snapshot_uri"].startswith("local_snapshot://")
        assert row["cleaning_reason"]
        assert row["entity_source_snippets"]


def test_external_source_evidence_pack_reports_insufficient_external_groups():
    pack = build_pack([_row("im", 0, source_name="telegram_public", source_type="IM")], per_group=2)

    assert pack["report"]["status"] == "insufficient_records"
    assert pack["report"]["selected_group_counts"]["im_or_group"] == 1
    assert pack["report"]["selected_group_counts"]["public_account_or_article"] == 0
    assert "public_account_or_article_insufficient:available=0;required=2" in pack["report"]["warnings"]


def test_external_source_evidence_pack_round_robins_sources_within_group():
    rows = (
        [_row("im-a", idx, source_name="telegram_a", source_type="IM") for idx in range(4)]
        + [_row("im-b", idx, source_name="telegram_b", source_type="IM") for idx in range(4)]
        + [
            _row(
                "article",
                idx,
                source_name="direct_public_article",
                source_type="Article",
                platform="wechat_public",
            )
            for idx in range(4)
        ]
        + [_row("forum", idx, source_name="tieba_public", source_type="Forum") for idx in range(4)]
        + [_row("vertical", idx, source_name="direct_technical_forum", source_type="Vertical") for idx in range(4)]
    )

    pack = build_pack(rows, per_group=4)
    im_sources = [
        row["source_name"]
        for row in pack["rows"]
        if row["source_evidence_group"] == "im_or_group"
    ]

    assert im_sources == ["telegram_a", "telegram_b", "telegram_a", "telegram_b"]


def test_external_source_evidence_group_does_not_treat_generic_article_urls_as_public_account():
    row = _row(
        "tech",
        1,
        source_name="direct_technical_forum_search",
        source_type="Forum",
        platform="technical_community",
    )
    row["source_url"] = "https://blog.csdn.net/developer/article/details/1"

    assert source_evidence_group(row) == "vertical_or_technical"


def _row(prefix: str, idx: int, *, source_name: str, source_type: str, platform: str = "") -> dict[str, str]:
    source_url = f"https://evidence.example/{prefix}/{idx}"
    return {
        "trace_id": f"{prefix}-{idx}",
        "source_name": source_name,
        "source_type": source_type,
        "platform": platform,
        "source_url": source_url,
        "crawl_time": "2026-06-08T08:00:00Z",
        "raw_payload_uri": f"https://payload.example/{prefix}/{idx}.json",
        "content_text": f"{prefix} 公开证据 接码 群控 TG:{prefix}{idx:03d} {source_url}",
        "legal_basis": "PUBLIC_COMPLIANT_DATA",
    }
