from __future__ import annotations

import json

from scripts import run_acceptance_gate
from scripts.build_authorized_source_rerun_pack import build_pack, main


def test_authorized_source_rerun_pack_reports_completed_real_external_coverage():
    raw_rows = [
        _row(
            "tg-real-1",
            source_name="authorized_telegram_partner",
            source_type="telegram_channel",
            platform="telegram",
            source_url="https://t.me/authorized_partner_channel/101",
            source_access_type="authorized_partner",
            legal_basis="AUTHORIZED_PARTNER",
            capture_snapshot_uri="s3://snapshots/tg-real-1.html",
            raw_payload_uri="s3://payloads/tg-real-1.json",
            raw_snippet="TG 真实授权采集样例 联系 TG:risk01",
        ),
        _row(
            "article-real-1",
            source_name="wechat_public_account_article",
            source_type="Article",
            platform="wechat_public",
            source_url="https://mp.weixin.qq.com/s/authorized-article-1",
            source_access_type="public_compliant",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            capture_snapshot_uri="s3://snapshots/article-real-1.html",
            raw_payload_uri="s3://payloads/article-real-1.json",
            raw_snippet="公众号文章授权样例",
        ),
        _row(
            "market-failed-1",
            source_name="xianyu_authorized_market",
            source_type="secondhand_market",
            platform="xianyu",
            source_url="https://2.taobao.example/item/authorized-1",
            source_access_type="authorized_partner",
            legal_basis="AUTHORIZED_PARTNER",
            capture_snapshot_uri="s3://snapshots/market-failed-1.html",
            raw_payload_uri="s3://payloads/market-failed-1.json",
            failure_reason="login_required",
            raw_snippet="二手市场登录后可见授权失败样例",
        ),
    ]
    source_reports = [
        {
            "source_name": "authorized_telegram_partner",
            "credential_fields_present": ["api_id", "api_hash"],
            "credentialed": True,
            "status": "completed",
        },
        {
            "source_name": "xianyu_authorized_market",
            "credential_fields_present": ["session_cookie"],
            "failure_reason": "login_required",
            "status": "failed",
        },
    ]

    report_payload = build_pack(
        raw_rows,
        source_reports=source_reports,
        collection_started_at="2026-06-10T00:00:00Z",
        collection_finished_at="2026-06-10T00:03:00Z",
    )
    report = report_payload["report"]
    rows = report_payload["rows"]

    assert report["status"] == "completed"
    assert report["credential_boundary"]["has_real_external_source"] is True
    assert report["credential_boundary"]["loopback_only"] is False
    assert report["source_coverage"]["covered_groups"]["real_telegram"] == 1
    assert report["source_coverage"]["covered_groups"]["public_account_or_article"] == 1
    assert report["snapshot_coverage"]["raw_snapshot_uri_count"] == 3
    assert report["failure_summary"]["by_reason"]["login_required"] == 1
    assert rows[0]["capture_snapshot_uri"].startswith("s3://snapshots/")

    assert rows[0]["collection_started_at"] == "2026-06-10T00:00:00Z"
    assert rows[0]["collection_finished_at"] == "2026-06-10T00:03:00Z"
    assert rows[0]["source_groups"] == ["im_or_group", "real_telegram"]
    assert "api_id" in report["credential_boundary"]["credential_fields_present"]
    assert "session_cookie" in report["credential_boundary"]["redacted_credential_fields"]
    assert "does not claim loopback-only demos" in report["claim_boundary"]


def test_authorized_source_rerun_pack_marks_loopback_only_as_insufficient():
    report_payload = build_pack(
        [
            _row(
                "loopback-tg-1",
                source_name="loopback-authorized-im-feed",
                source_type="telegram",
                platform="telegram",
                source_url="http://127.0.0.1:8080/authorized-im-feed.json",
                source_access_type="loopback_demo",
                legal_basis="INTERNAL_AUTHORIZED_SOURCE",
                capture_snapshot_uri="loopback://snapshots/loopback-tg-1.html",
                raw_payload_uri="loopback://payloads/loopback-tg-1.json",
            )
        ],
        source_reports=[
            {
                "source_name": "loopback-authorized-im-feed",
                "credential_fields_present": ["Authorization"],
                "loopback_only": True,
                "status": "completed",
            }
        ],
    )

    report = report_payload["report"]

    assert report["status"] == "insufficient_real_authorized_sources"
    assert report["credential_boundary"]["has_real_external_source"] is False
    assert report["credential_boundary"]["loopback_only"] is True
    assert report["source_coverage"]["covered_groups"]["real_telegram"] == 0
    assert report["source_coverage"]["loopback_group_counts"]["im_or_group"] == 1
    assert "loopback" in report["credential_boundary"]["claim_boundary"]


def test_authorized_source_rerun_pack_ignores_non_scalar_source_report_names():
    report_payload = build_pack(
        [],
        source_reports=[
            {
                "source": {
                    "source_name": "loopback-authorized-feed",
                    "source_url": "http://127.0.0.1:50400",
                },
                "credential_fields_present": ["Authorization"],
                "credentialed": True,
            }
        ],
    )

    assert report_payload["report"]["credential_boundary"]["credentialed_source_names"] == [
        "loopback-authorized-feed"
    ]


def test_authorized_source_rerun_pack_requires_authorization_metadata_for_real_external_completion():
    report_payload = build_pack(
        [
            _row(
                "manual-no-auth-1",
                source_name="manual_upload_public_url",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/manual_uploaded_channel/1",
                source_access_type="manual_upload",
                legal_basis="PUBLIC_COMPLIANT_DATA",
            ),
            _row(
                "no-metadata-1",
                source_name="unknown_http_source",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/unknown_channel/2",
                source_access_type="",
                legal_basis="",
            ),
        ],
    )

    report = report_payload["report"]

    assert report["status"] == "insufficient_real_authorized_sources"
    assert report["real_external_row_count"] == 0
    assert report["credential_boundary"]["has_real_external_source"] is False
    assert report["source_coverage"]["covered_groups"]["real_telegram"] == 0
    assert report["source_coverage"]["all_group_counts"]["real_telegram"] == 0
    assert report_payload["rows"][0]["is_real_external_source"] is False


def test_authorized_source_rerun_pack_rejects_explicit_disallowed_raw_access_types():
    report_payload = build_pack(
        [
            _row(
                "raw-access-unauthorized-1",
                source_name="raw_access_unauthorized",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/raw_access_unauthorized/1",
                source_access_type="unauthorized",
                legal_basis="PUBLIC_COMPLIANT_DATA",
            ),
            _row(
                "raw-access-manual-1",
                source_name="raw_access_manual",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/raw_access_manual/1",
                source_access_type="manual",
                legal_basis="AUTHORIZED_PARTNER",
            ),
        ],
    )

    report = report_payload["report"]

    assert report["status"] == "insufficient_real_authorized_sources"
    assert report["real_external_row_count"] == 0
    assert report["credential_boundary"]["has_real_external_source"] is False
    assert report_payload["rows"][0]["raw_source_access_type"] == "unauthorized"
    assert report_payload["rows"][1]["raw_source_access_type"] == "manual"
    assert report_payload["rows"][0]["source_access_type"] == "public_compliant"
    assert report_payload["rows"][1]["source_access_type"] == "manual_upload"
    assert report_payload["rows"][0]["is_authorized_source"] is False
    assert report_payload["rows"][1]["is_authorized_source"] is False


def test_authorized_source_rerun_pack_excludes_failed_and_evidence_empty_rows_from_coverage():
    report_payload = build_pack(
        [
            _row(
                "tg-failed-1",
                source_name="authorized_telegram_partner",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/authorized_partner_channel/301",
                source_access_type="authorized_partner",
                legal_basis="AUTHORIZED_PARTNER",
                failure_reason="login_required",
                raw_snippet="failed row still retained for failure summary",
            ),
            _row(
                "article-empty-1",
                source_name="wechat_public_account_article",
                source_type="Article",
                platform="wechat_public",
                source_url="https://mp.weixin.qq.com/s/authorized-empty",
                source_access_type="public_compliant",
                legal_basis="PUBLIC_COMPLIANT_DATA",
                raw_snippet="",
                content_hash="",
            ),
        ],
    )

    report = report_payload["report"]

    assert report["status"] == "insufficient_real_authorized_sources"
    assert report["real_external_row_count"] == 0
    assert report["source_coverage"]["all_group_counts"]["real_telegram"] == 0
    assert report["source_coverage"]["all_group_counts"]["im_or_group"] == 1
    assert report["source_coverage"]["all_group_counts"]["public_account_or_article"] == 1
    assert report["source_coverage"]["covered_groups"]["real_telegram"] == 0
    assert report["source_coverage"]["covered_groups"]["public_account_or_article"] == 0
    assert report["failure_summary"]["by_reason"]["login_required"] == 1
    assert report_payload["rows"][0]["is_real_external_source"] is False
    assert report_payload["rows"][0]["is_real_external_candidate"] is True


def test_authorized_source_rerun_pack_treats_failed_status_fields_as_non_evidence():
    report_payload = build_pack(
        [
            _row(
                "tg-status-failed-1",
                source_name="authorized_telegram_partner",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/authorized_partner_channel/401",
                source_access_type="authorized_partner",
                legal_basis="AUTHORIZED_PARTNER",
                status="failed",
                failure_reason="",
                raw_snippet="otherwise complete failed status row",
            )
        ],
    )

    report = report_payload["report"]

    assert report["status"] == "insufficient_real_authorized_sources"
    assert report["real_external_row_count"] == 0
    assert report["source_coverage"]["all_group_counts"]["real_telegram"] == 0
    assert report["source_coverage"]["all_group_counts"]["im_or_group"] == 1
    assert report["source_coverage"]["covered_groups"]["real_telegram"] == 0
    assert report["failure_summary"]["by_reason"]["failed"] == 1
    assert report_payload["rows"][0]["failure_reason"] == "failed"
    assert report_payload["rows"][0]["is_real_external_source"] is False


def test_authorized_source_rerun_pack_treats_partial_failure_status_as_non_evidence():
    report_payload = build_pack(
        [
            _row(
                "tg-status-partial-failure-1",
                source_name="authorized_telegram_partner",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/authorized_partner_channel/402",
                source_access_type="authorized_partner",
                legal_basis="AUTHORIZED_PARTNER",
                status="partial_failure",
                failure_reason="",
                raw_snippet="otherwise complete partial failure status row",
            )
        ],
    )

    report = report_payload["report"]

    assert report["status"] == "insufficient_real_authorized_sources"
    assert report["real_external_row_count"] == 0
    assert report["source_coverage"]["covered_groups"]["real_telegram"] == 0
    assert report["failure_summary"]["by_reason"]["partial_failure"] == 1
    assert report_payload["rows"][0]["failure_reason"] == "partial_failure"
    assert report_payload["rows"][0]["is_real_external_source"] is False


def test_authorized_source_rerun_pack_does_not_trust_explicit_real_telegram_quota_group():
    report_payload = build_pack(
        [
            _row(
                "forum-explicit-telegram-1",
                source_name="authorized_public_forum",
                source_type="Forum",
                platform="forum",
                source_url="https://forum.example.com/thread/explicit-quota",
                source_access_type="public_compliant",
                legal_basis="PUBLIC_COMPLIANT_DATA",
                source_quota_groups=["real_telegram", "social_or_forum"],
            )
        ],
    )

    report = report_payload["report"]

    assert report["status"] == "completed"
    assert report["real_external_row_count"] == 1
    assert report["source_coverage"]["covered_groups"]["real_telegram"] == 0
    assert report["source_coverage"]["all_group_counts"]["real_telegram"] == 0
    assert "real_telegram" not in report_payload["rows"][0]["source_groups"]


def test_authorized_source_rerun_pack_excludes_private_and_local_hosts_from_real_external_coverage():
    for source_url in (
        "https://192.168.1.25/authorized/row",
        "https://10.10.0.5/authorized/row",
        "https://172.16.0.5/authorized/row",
        "https://collector.local/authorized/row",
        "https://internalhost/authorized/row",
    ):
        report_payload = build_pack(
            [
                _row(
                    f"private-{source_url.split('//', 1)[1].split('/', 1)[0]}",
                    source_name="internal_authorized_lab_source",
                    source_type="telegram_channel",
                    platform="telegram",
                    source_url=source_url,
                    source_access_type="internal_authorized",
                    legal_basis="INTERNAL_AUTHORIZED_SOURCE",
                )
            ],
        )
        report = report_payload["report"]

        assert report["status"] == "insufficient_real_authorized_sources"
        assert report["real_external_row_count"] == 0
        assert report["source_coverage"]["covered_groups"]["real_telegram"] == 0
        assert report_payload["rows"][0]["is_authorized_source"] is True
        assert report_payload["rows"][0]["is_real_external_source"] is False


def test_authorized_source_rerun_pack_cli_writes_jsonl_and_report(tmp_path):
    input_path = tmp_path / "raw.jsonl"
    source_report_path = tmp_path / "source_report.json"
    output_path = tmp_path / "authorized_source_rerun_pack.jsonl"
    report_path = tmp_path / "authorized_source_rerun_pack_report.json"
    input_path.write_text(
        json.dumps(
            _row(
                "tg-real-cli",
                source_name="authorized_telegram_partner",
                source_type="telegram_channel",
                platform="telegram",
                source_url="https://t.me/authorized_partner_channel/202",
                source_access_type="authorized_partner",
                legal_basis="AUTHORIZED_PARTNER",
                capture_snapshot_uri="s3://snapshots/tg-real-cli.html",
                raw_payload_uri="s3://payloads/tg-real-cli.json",
            ),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    source_report_path.write_text(
        json.dumps(
            {
                "source_name": "authorized_telegram_partner",
                "credential_fields_present": ["api_id"],
                "status": "completed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--input-jsonl",
            str(input_path),
            "--source-report",
            str(source_report_path),
            "--output",
            str(output_path),
            "--report",
            str(report_path),
        ]
    )

    saved_rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert saved_rows[0]["trace_id"] == "tg-real-cli"
    assert saved_report["status"] == "completed"
    assert saved_report["source_coverage"]["covered_groups"]["real_telegram"] == 1


def test_acceptance_gate_optionally_includes_authorized_rerun_pack(tmp_path):
    _write_required_acceptance_artifacts(tmp_path)
    data = tmp_path / "data"
    report_path = data / "authorized_source_rerun_pack_report.json"
    jsonl_path = data / "authorized_source_rerun_pack.jsonl"
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "row_count": 1,
                "real_external_row_count": 1,
                "source_coverage": {"covered_groups": {"real_telegram": 1}},
                "credential_boundary": {
                    "has_real_external_source": True,
                    "loopback_only": False,
                    "claim_boundary": "real external row present",
                },
                "snapshot_coverage": {"raw_snapshot_uri_count": 1},
                "failure_summary": {"by_reason": {}},
                "claim_boundary": "Authorized rerun pack only.",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    jsonl_path.write_text(
        json.dumps(
            {
                "trace_id": "tg-real-1",
                "is_real_external_source": True,
                "capture_snapshot_uri": "s3://snapshots/tg-real-1.html",
                "raw_payload_uri": "s3://payloads/tg-real-1.json",
                "source_groups": ["im_or_group", "real_telegram"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    source_paths = {source["path"] for source in summary["artifact_sources"].values()}
    assert "data/authorized_source_rerun_pack_report.json" in source_paths
    assert "data/authorized_source_rerun_pack.jsonl" in source_paths
    assert summary["authorized_source_rerun"]["status"] == "completed"
    assert summary["authorized_source_rerun"]["source_coverage"]["covered_groups"]["real_telegram"] == 1

    report_path.unlink()
    jsonl_path.unlink()
    summary_without_optional = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    source_paths = {source["path"] for source in summary_without_optional["artifact_sources"].values()}
    assert "data/authorized_source_rerun_pack_report.json" not in source_paths
    assert "authorized_source_rerun" not in summary_without_optional


def test_acceptance_gate_fails_incomplete_authorized_rerun_artifact_pair(tmp_path):
    _write_required_acceptance_artifacts(tmp_path)
    data = tmp_path / "data"
    report_path = data / "authorized_source_rerun_pack_report.json"
    jsonl_path = data / "authorized_source_rerun_pack.jsonl"
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "row_count": 1,
                "real_external_row_count": 1,
                "source_coverage": {"covered_groups": {"real_telegram": 1}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert summary["authorized_source_rerun"]["artifact_pair_complete"] is False
    assert {
        "type": "authorized_source_rerun_artifact_pair_incomplete",
        "name": "authorized_source_rerun_pack",
        "report_path": "data/authorized_source_rerun_pack_report.json",
        "jsonl_path": "data/authorized_source_rerun_pack.jsonl",
        "report_exists": True,
        "jsonl_exists": False,
    } in summary["gate_failures"]

    report_path.unlink()
    jsonl_path.write_text('{"trace_id":"orphan-row"}\n', encoding="utf-8")
    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert "authorized_source_rerun" not in summary
    assert {
        "type": "authorized_source_rerun_artifact_pair_incomplete",
        "name": "authorized_source_rerun_pack",
        "report_path": "data/authorized_source_rerun_pack_report.json",
        "jsonl_path": "data/authorized_source_rerun_pack.jsonl",
        "report_exists": False,
        "jsonl_exists": True,
    } in summary["gate_failures"]


def test_acceptance_gate_fails_completed_authorized_rerun_report_with_invalid_jsonl(tmp_path):
    for jsonl_text, reason in (
        ("", "empty_jsonl"),
        ("not-json\n", "malformed_jsonl"),
        ("[]\n{}\n", "non_object_jsonl_row"),
    ):
        _write_required_acceptance_artifacts(tmp_path)
        data = tmp_path / "data"
        report_path = data / "authorized_source_rerun_pack_report.json"
        jsonl_path = data / "authorized_source_rerun_pack.jsonl"
        report_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "row_count": 1,
                    "real_external_row_count": 1,
                    "source_coverage": {"covered_groups": {"real_telegram": 1}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        jsonl_path.write_text(jsonl_text, encoding="utf-8")

        summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

        assert summary["status"] == "failed"
        assert {
            "type": "authorized_source_rerun_jsonl_invalid",
            "name": "authorized_source_rerun_pack_jsonl",
            "path": "data/authorized_source_rerun_pack.jsonl",
            "reason": reason,
        } in summary["gate_failures"]

        report_path.unlink()
        jsonl_path.unlink()


def test_acceptance_gate_fails_completed_authorized_rerun_report_with_mixed_non_object_jsonl(tmp_path):
    _write_required_acceptance_artifacts(tmp_path)
    data = tmp_path / "data"
    report_path = data / "authorized_source_rerun_pack_report.json"
    jsonl_path = data / "authorized_source_rerun_pack.jsonl"
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "row_count": 1,
                "real_external_row_count": 1,
                "source_coverage": {"covered_groups": {"real_telegram": 1}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    jsonl_path.write_text('[]\n{"trace_id":"ok"}\n', encoding="utf-8")

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert {
        "type": "authorized_source_rerun_jsonl_invalid",
        "name": "authorized_source_rerun_pack_jsonl",
        "path": "data/authorized_source_rerun_pack.jsonl",
        "reason": "non_object_jsonl_row",
    } in summary["gate_failures"]


def test_acceptance_gate_fails_completed_authorized_rerun_report_with_arbitrary_jsonl_row(tmp_path):
    _write_required_acceptance_artifacts(tmp_path)
    data = tmp_path / "data"
    report_path = data / "authorized_source_rerun_pack_report.json"
    jsonl_path = data / "authorized_source_rerun_pack.jsonl"
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "row_count": 1,
                "real_external_row_count": 1,
                "source_coverage": {"covered_groups": {"real_telegram": 1}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    jsonl_path.write_text('{"trace_id":"tg-real-1"}\n', encoding="utf-8")

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert {
        "type": "authorized_source_rerun_jsonl_invalid",
        "name": "authorized_source_rerun_pack_jsonl",
        "path": "data/authorized_source_rerun_pack.jsonl",
        "reason": "real_external_row_count_mismatch",
    } in summary["gate_failures"]


def test_acceptance_gate_fails_completed_authorized_rerun_report_missing_required_report_fields(tmp_path):
    cases = [
        (
            {
                "status": "completed",
                "real_external_row_count": 1,
                "source_coverage": {"covered_groups": {"real_telegram": 1}},
            },
            "missing_report_row_count",
        ),
        (
            {
                "status": "completed",
                "row_count": 1,
                "source_coverage": {"covered_groups": {"real_telegram": 1}},
            },
            "missing_report_real_external_row_count",
        ),
        (
            {
                "status": "completed",
                "row_count": 1,
                "real_external_row_count": 1,
                "source_coverage": {"covered_groups": ["real_telegram"]},
            },
            "missing_report_covered_groups",
        ),
    ]
    for report, reason in cases:
        _write_required_acceptance_artifacts(tmp_path)
        data = tmp_path / "data"
        report_path = data / "authorized_source_rerun_pack_report.json"
        jsonl_path = data / "authorized_source_rerun_pack.jsonl"
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        jsonl_path.write_text(
            json.dumps(
                {
                    "trace_id": "tg-real-1",
                    "is_real_external_source": True,
                    "capture_snapshot_uri": "s3://snapshots/tg-real-1.html",
                    "raw_payload_uri": "s3://payloads/tg-real-1.json",
                    "source_groups": ["im_or_group", "real_telegram"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

        assert summary["status"] == "failed"
        assert {
            "type": "authorized_source_rerun_jsonl_invalid",
            "name": "authorized_source_rerun_pack_jsonl",
            "path": "data/authorized_source_rerun_pack.jsonl",
            "reason": reason,
        } in summary["gate_failures"]

        report_path.unlink()
        jsonl_path.unlink()


def test_acceptance_gate_fails_completed_authorized_rerun_report_with_unclaimable_real_external_row(tmp_path):
    _write_required_acceptance_artifacts(tmp_path)
    data = tmp_path / "data"
    report_path = data / "authorized_source_rerun_pack_report.json"
    jsonl_path = data / "authorized_source_rerun_pack.jsonl"
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "row_count": 2,
                "real_external_row_count": 2,
                "source_coverage": {"covered_groups": {"real_telegram": 1, "public_account_or_article": 1}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "trace_id": "tg-real-1",
                        "is_real_external_source": True,
                        "capture_snapshot_uri": "s3://snapshots/tg-real-1.html",
                        "raw_payload_uri": "s3://payloads/tg-real-1.json",
                        "source_groups": ["im_or_group", "real_telegram"],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "trace_id": "article-real-1",
                        "is_real_external_source": True,
                        "capture_snapshot_uri": "",
                        "raw_payload_uri": "s3://payloads/article-real-1.json",
                        "source_groups": ["public_account_or_article"],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

    assert summary["status"] == "failed"
    assert {
        "type": "authorized_source_rerun_jsonl_invalid",
        "name": "authorized_source_rerun_pack_jsonl",
        "path": "data/authorized_source_rerun_pack.jsonl",
        "reason": "real_external_row_without_claimable_evidence",
    } in summary["gate_failures"]


def test_acceptance_gate_fails_completed_authorized_rerun_report_with_count_or_group_mismatch(tmp_path):
    for report_overrides, reason in (
        ({"real_external_row_count": 2}, "real_external_row_count_mismatch"),
        (
            {
                "real_external_row_count": 1,
                "source_coverage": {"covered_groups": {"real_telegram": 2}},
            },
            "covered_group_count_mismatch",
        ),
    ):
        _write_required_acceptance_artifacts(tmp_path)
        data = tmp_path / "data"
        report_path = data / "authorized_source_rerun_pack_report.json"
        jsonl_path = data / "authorized_source_rerun_pack.jsonl"
        report = {
            "status": "completed",
            "row_count": 1,
            "real_external_row_count": 1,
            "source_coverage": {"covered_groups": {"real_telegram": 1}},
        }
        report.update(report_overrides)
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        jsonl_path.write_text(
            json.dumps(
                {
                    "trace_id": "tg-real-1",
                    "is_real_external_source": True,
                    "capture_snapshot_uri": "s3://snapshots/tg-real-1.html",
                    "raw_payload_uri": "s3://payloads/tg-real-1.json",
                    "source_groups": ["im_or_group", "real_telegram"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        summary = run_acceptance_gate.build_summary(root=tmp_path, command_results=[])

        assert summary["status"] == "failed"
        assert {
            "type": "authorized_source_rerun_jsonl_invalid",
            "name": "authorized_source_rerun_pack_jsonl",
            "path": "data/authorized_source_rerun_pack.jsonl",
            "reason": reason,
        } in summary["gate_failures"]

        report_path.unlink()
        jsonl_path.unlink()


def _row(trace_id: str, **overrides) -> dict[str, str]:
    row = {
        "trace_id": trace_id,
        "source_name": "authorized_source",
        "source_type": "Forum",
        "platform": "forum",
        "source_url": f"https://evidence.example/{trace_id}",
        "source_access_type": "public_compliant",
        "legal_basis": "PUBLIC_COMPLIANT_DATA",
        "crawl_time": "2026-06-10T00:01:00Z",
        "capture_snapshot_uri": f"s3://snapshots/{trace_id}.html",
        "raw_payload_uri": f"s3://payloads/{trace_id}.json",
        "failure_reason": "",
        "content_hash": f"hash-{trace_id}",
        "raw_snippet": f"raw snippet {trace_id}",
    }
    row.update(overrides)
    return row


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_text(path, text="{}\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_required_acceptance_artifacts(root):
    data = root / "data"
    _write_json(
        data / "manual_heldout_eval_current.json",
        {"status": "completed", "dataset": {"kind": "manual"}, "profile": "fast"},
    )
    _write_json(
        data / "eval_manual_heldout_clue_recall_report.json",
        {"status": "completed", "dataset": {"kind": "clue"}, "profile": "high_recall"},
    )
    _write_json(data / "external_balanced_source_evidence_pack_report.json", {"status": "completed"})
    _write_text(data / "external_balanced_source_evidence_pack.jsonl", '{"id":"external"}\n')
    _write_json(data / "collection_phase_multi_source_evidence_pack_report.json", {"status": "completed"})
    _write_text(data / "collection_phase_multi_source_evidence_pack.jsonl", '{"id":"joined"}\n')
