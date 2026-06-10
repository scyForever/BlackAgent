from scripts.build_heldout_eval import build_holdout_coverage_report, build_report


def _coverage_records():
    return [
        {
            "trace_id": "telegram-recent",
            "platform": "telegram",
            "source_name": "telegram_public_delivery:sample",
            "source_url": "https://t.me/sample/10",
            "publish_time": "2026-06-10T12:00:00+00:00",
            "content_text": "纸飞机 TG @sample_bot 协议号 session 数据",
            "matched_keywords": ["纸飞机", "协议号"],
            "matched_themes": ["工具交易"],
            "expected_entities": [{"entity_type": "contact", "normalized_value": "TG:sample_bot"}],
            "expected_risk_categories": ["工具交易"],
            "expected_secondary_labels": ["群控脚本"],
        },
        {
            "trace_id": "secondhand-mid",
            "source_type": "secondhand",
            "source_name": "闲鱼二手账号交易市场",
            "publish_time": "2026-05-25T12:00:00+00:00",
            "content_text": "实名号 账号资料 可加v 走平台",
            "matched_keywords": ["加v", "实名号"],
            "matched_themes": ["账号交易"],
            "expected_entities": [{"entity_type": "slang_term", "normalized_value": "微信"}],
            "expected_risk_categories": ["账号交易"],
            "expected_secondary_labels": ["实名账号买卖"],
        },
        {
            "trace_id": "crowd-older",
            "source_type": "task_platform",
            "source_name": "众包接单平台",
            "crawl_time": "2026-04-01T12:00:00+00:00",
            "content_text": "拉群 接单 群发任务",
            "matched_keywords": ["拉群", "群发"],
            "matched_themes": ["众包任务"],
            "expected_risk_categories": ["众包服务"],
            "expected_secondary_labels": ["拉群获客"],
        },
        {
            "trace_id": "article-recent",
            "source_type": "public_account",
            "source_name": "微信公众号文章",
            "collection_time": "2026-06-03T12:00:00+00:00",
            "content_text": "短信验证码 接码平台 教程",
            "matched_keywords": ["短信验证码", "接码"],
            "matched_themes": ["接码"],
            "expected_risk_categories": ["账号交易"],
            "expected_secondary_labels": ["接码注册"],
        },
        {
            "trace_id": "forum-missing",
            "source_type": "forum",
            "source_name": "forum_sample",
            "content_text": "观察到新暗语 蓝标货 暂无归类",
            "matched_keywords": ["新暗语"],
            "expected_entities": [{"entity_type": "slang_term", "normalized_value": "蓝标货"}],
            "expected_risk_categories": ["unknown"],
            "expected_secondary_labels": ["待研判"],
            "evidence": [{"snippet": "蓝标货 是未知新黑话"}],
        },
    ]


def test_holdout_coverage_report_counts_source_time_and_slang_dimensions(tmp_path):
    report = build_holdout_coverage_report(_coverage_records(), tmp_path / "heldout.jsonl")

    source = report["source_holdout"]
    assert set(source["covered_required_groups"]) == {
        "real_telegram",
        "secondhand_market",
        "crowdsourcing_platform",
        "public_account_or_article",
    }
    assert source["missing_required_groups"] == []
    assert source["counts"]["real_telegram"] == 1
    assert source["counts"]["secondhand_market"] == 1
    assert source["counts"]["crowdsourcing_platform"] == 1
    assert source["counts"]["public_account_or_article"] == 1

    assert report["time_holdout"]["reference_time"] == "2026-06-10T12:00:00+00:00"
    assert report["time_holdout"]["bucket_counts"] == {
        "recent_0_7d": 2,
        "mid_8_30d": 1,
        "older_31d_plus": 1,
        "missing_time": 1,
    }

    assert report["slang_family_holdout"]["family_counts"] == {
        "telegram_alias": 1,
        "wechat_alias": 1,
        "sms_code_alias": 1,
        "group_alias": 1,
        "account_material_alias": 2,
        "unknown_new_slang": 1,
    }
    assert report["claim_boundary"].startswith("Holdout coverage")


def test_build_report_includes_holdout_dimensions_without_dropping_existing_fields(tmp_path):
    records = _coverage_records()

    report = build_report(records, output_path=tmp_path / "heldout.jsonl")

    assert report["status"] == "completed"
    assert report["record_count"] == len(records)
    assert "category_counts" in report
    assert "human_review" in report
    assert report["holdout_dimensions"]["source_holdout"]["missing_required_groups"] == []
    assert report["holdout_dimensions"]["claim_boundary"].startswith("Holdout coverage")


def test_real_telegram_detection_handles_domain_only_url_and_cjk_adjacent_tg(tmp_path):
    records = [
        {
            "trace_id": "telegram-domain-only",
            "source_url": "https://t.me",
            "content_text": "domain only source",
        },
        {
            "trace_id": "telegram-source-name",
            "source_name": "TG频道",
            "content_text": "source-name metadata",
        },
    ]

    report = build_holdout_coverage_report(records, tmp_path / "heldout.jsonl")

    assert report["source_holdout"]["counts"]["real_telegram"] == 2


def test_real_telegram_source_holdout_ignores_body_text_mentions(tmp_path):
    records = [
        {
            "trace_id": "article-telegram-discussion",
            "source_type": "Article",
            "source_name": "public_article",
            "content_text": "安全研究文章讨论 Telegram 和 加TG群 发布 的诈骗话术",
        },
        {
            "trace_id": "metadata-telegram",
            "source_type": "IM",
            "source_url": "https://t.me/channel/1",
            "content_text": "body without source signal",
        },
    ]

    report = build_holdout_coverage_report(records, tmp_path / "heldout.jsonl")

    assert report["source_holdout"]["counts"]["real_telegram"] == 1


def test_real_telegram_source_holdout_ignores_explicit_quota_metadata(tmp_path):
    records = [
        {
            "trace_id": "article-explicit-telegram-quota",
            "source_type": "Article",
            "source_name": "public_article",
            "source_quota_groups": ["real_telegram", "REAL_TELEGRAM", "Real_Telegram"],
            "content_text": "article row with misleading explicit quota group",
        }
    ]

    report = build_holdout_coverage_report(records, tmp_path / "heldout.jsonl")
    counts = report["source_holdout"]["counts"]

    assert counts.get("real_telegram", 0) == 0
    assert "REAL_TELEGRAM" not in counts
    assert "Real_Telegram" not in counts


def test_real_telegram_source_holdout_ignores_loose_metadata_substrings(tmp_path):
    records = [
        {
            "trace_id": "article-about-telegram",
            "source_type": "not_telegram",
            "source_name": "public_article_about_telegram",
            "source_url": "https://example.com/post?topic=telegram",
            "content_text": "metadata mentions telegram but source is not Telegram",
        }
    ]

    report = build_holdout_coverage_report(records, tmp_path / "heldout.jsonl")

    assert report["source_holdout"]["counts"].get("real_telegram", 0) == 0


def test_slang_family_detection_counts_cjk_adjacent_tg_wx_and_vx_aliases(tmp_path):
    records = [
        {
            "trace_id": "slang-cjk-aliases",
            "source_type": "Article",
            "content_text": "加TG群 TG频道 wx号",
            "evidence": [{"snippet": "vx号 也会用于导流"}],
        }
    ]

    report = build_holdout_coverage_report(records, tmp_path / "heldout.jsonl")
    families = report["slang_family_holdout"]["family_counts"]

    assert families["telegram_alias"] == 1
    assert families["wechat_alias"] == 1


def test_holdout_coverage_empty_input_reports_zero_counts_and_missing_required_groups(tmp_path):
    report = build_holdout_coverage_report([], tmp_path / "heldout.jsonl")

    assert report["source_holdout"]["counts"] == {}
    assert report["source_holdout"]["covered_required_groups"] == []
    assert set(report["source_holdout"]["missing_required_groups"]) == {
        "real_telegram",
        "secondhand_market",
        "crowdsourcing_platform",
        "public_account_or_article",
    }
    assert report["time_holdout"]["bucket_counts"] == {
        "recent_0_7d": 0,
        "mid_8_30d": 0,
        "older_31d_plus": 0,
        "missing_time": 0,
    }
    assert all(count == 0 for count in report["slang_family_holdout"]["family_counts"].values())


def test_time_holdout_uses_created_at_and_timestamp_for_reference_and_buckets(tmp_path):
    records = [
        {
            "trace_id": "created-at-reference",
            "created_at": "2026-06-10T12:00:00+00:00",
            "content_text": "latest timestamp only in created_at",
        },
        {
            "trace_id": "timestamp-mid",
            "timestamp": "2026-05-25T12:00:00+00:00",
            "content_text": "older timestamp only in timestamp",
        },
    ]

    report = build_holdout_coverage_report(records, tmp_path / "heldout.jsonl")

    assert report["time_holdout"]["reference_time"] == "2026-06-10T12:00:00+00:00"
    assert report["time_holdout"]["bucket_counts"] == {
        "recent_0_7d": 1,
        "mid_8_30d": 1,
        "older_31d_plus": 0,
        "missing_time": 0,
    }
