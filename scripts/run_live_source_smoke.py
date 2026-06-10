"""Run an authorized live-collection smoke against a local loopback feed.

This proves the runtime can perform a real HTTP fetch with an authorization
header and compliance metadata, without touching external sites or relying on
private credentials during tests/demo.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cleaner.text_filter import normalize_text
from src.collector import HTTPFeedCollector, HTTPFeedConfig
from src.collector.base_collector import model_dump
from src.collector.source_metadata import source_quota_groups_for_record
from src.enhancement.text_intelligence import AdvancedEntityExtractor
from src.pipeline import IntelligencePipeline


DEFAULT_TOKEN = "BLACKAGENT_AUTHORIZED_LOOPBACK_SMOKE"
SOURCE_CLASS_SCENARIOS = (
    {
        "source_class": "im_or_group",
        "smoke_group": "im_or_group",
        "source_name": "loopback-authorized-im-feed",
        "source_type": "IM",
        "path": "/authorized-im-feed.json",
        "items": [
            {
                "source_url": "https://t.me/authorized_live_smoke/1",
                "full_text": "群控脚本接码上车，联系 TG:live001，落地 https://live-smoke.example/a",
                "capture_snapshot_uri": "loopback://snapshots/im-feed/message-1.json",
                "raw_payload_uri": "loopback://payloads/im-feed/message-1.json",
            },
            {
                "source_url": "https://t.me/authorized_live_smoke/2",
                "full_text": "群控脚本接码上车，联系 TG:live001，落地 https://live-smoke.example/a",
                "capture_snapshot_uri": "loopback://snapshots/im-feed/message-2.json",
                "raw_payload_uri": "loopback://payloads/im-feed/message-2.json",
            },
            {
                "source_url": "https://t.me/authorized_live_smoke/3",
                "full_text": "群控安全研究讨论，参考 https://safety.example.test/im-guide，不含交易招募",
                "capture_snapshot_uri": "loopback://snapshots/im-feed/message-3.json",
                "raw_payload_uri": "loopback://payloads/im-feed/message-3.json",
            },
        ],
        "include_keywords": ("群控", "接码", "私域"),
    },
    {
        "source_class": "social_or_forum",
        "smoke_group": "social_or_forum",
        "source_name": "loopback-authorized-forum-feed",
        "source_type": "Forum",
        "path": "/authorized-forum-feed.json",
        "items": [
            {
                "source_url": "https://forum.example.test/thread/authorized-live-smoke-1",
                "full_text": "论坛帖子：私域导流返利拉新，开户链接 https://lead-smoke.example/b，联系 TG:live002",
                "capture_snapshot_uri": "loopback://snapshots/forum-feed/thread-1.html",
                "raw_payload_uri": "loopback://payloads/forum-feed/thread-1.json",
            },
            {
                "source_url": "https://forum.example.test/thread/authorized-live-smoke-2",
                "full_text": "论坛帖子：群发广告投放业务，客户包量，联系 @forumops",
                "capture_snapshot_uri": "loopback://snapshots/forum-feed/thread-2.html",
                "raw_payload_uri": "loopback://payloads/forum-feed/thread-2.json",
            },
            {
                "source_url": "https://forum.example.test/thread/authorized-live-smoke-3",
                "full_text": "平台公告：私域风险反诈安全通告，参考 https://safety.example.test/forum-guide",
                "capture_snapshot_uri": "loopback://snapshots/forum-feed/thread-3.html",
                "raw_payload_uri": "loopback://payloads/forum-feed/thread-3.json",
            },
        ],
        "include_keywords": ("私域", "返利", "群发", "投放"),
    },
    {
        "source_class": "vertical_or_technical",
        "smoke_group": "vertical_or_technical",
        "source_name": "loopback-authorized-vertical-feed",
        "source_type": "Vertical",
        "path": "/authorized-vertical-feed.json",
        "items": [
            {
                "source_url": "https://market.example.test/item/authorized-live-smoke-1",
                "full_text": "垂直站点供给：账号批量出号，支持接码注册，价格 2U，客服 @vertical01",
                "capture_snapshot_uri": "loopback://snapshots/vertical-feed/item-1.html",
                "raw_payload_uri": "loopback://payloads/vertical-feed/item-1.json",
            },
            {
                "source_url": "https://market.example.test/item/authorized-live-smoke-2",
                "full_text": "垂直站点供给：账号批量出号，支持接码注册，价格 2U，客服 @vertical01",
                "capture_snapshot_uri": "loopback://snapshots/vertical-feed/item-2.html",
                "raw_payload_uri": "loopback://payloads/vertical-feed/item-2.json",
            },
            {
                "source_url": "https://market.example.test/item/authorized-live-smoke-3",
                "full_text": "站点帮助文档：合规账号安全设置，参考 https://safety.example.test/account-guide",
                "capture_snapshot_uri": "loopback://snapshots/vertical-feed/item-3.html",
                "raw_payload_uri": "loopback://payloads/vertical-feed/item-3.json",
            },
        ],
        "include_keywords": ("账号", "出号", "接码", "价格"),
    },
    {
        "source_class": "social_or_forum",
        "smoke_group": "public_account_or_article",
        "source_name": "loopback-authorized-public-account-article-feed",
        "source_type": "Article",
        "platform": "wechat_public",
        "path": "/authorized-public-account-article-feed.json",
        "items": [
            {
                "source_url": "https://mp.weixin.qq.com/s/authorized-live-smoke-risk-article",
                "full_article_body": (
                    "公众号长文：接码平台和群控脚本被用于批量注册与私域导流，"
                    "文章保留完整正文、发布时间、来源链接和本地授权快照，联系线索 TG:article001。"
                ),
                "capture_snapshot_uri": "loopback://snapshots/public-account-article-feed/article-1.html",
                "raw_payload_uri": "loopback://payloads/public-account-article-feed/article-1.json",
            },
            {
                "source_url": "https://mp.weixin.qq.com/s/authorized-live-smoke-risk-article-followup",
                "full_article_body": (
                    "公众号跟进文章：账号买卖、短信接码和群发工具形成链条，"
                    "原文正文用于 smoke 证据而不是搜索摘要，客服 @articleops。"
                ),
                "capture_snapshot_uri": "loopback://snapshots/public-account-article-feed/article-2.html",
                "raw_payload_uri": "loopback://payloads/public-account-article-feed/article-2.json",
            },
            {
                "source_url": "https://mp.weixin.qq.com/s/authorized-live-smoke-benign-article",
                "full_article_body": "公众号文章：接码风险反诈宣传提醒，参考 https://safety.example.test/article-guide，不提供交易方式。",
                "capture_snapshot_uri": "loopback://snapshots/public-account-article-feed/article-3.html",
                "raw_payload_uri": "loopback://payloads/public-account-article-feed/article-3.json",
            },
        ],
        "include_keywords": ("接码", "群控", "账号", "导流"),
    },
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local authorized live source collection smoke.")
    parser.add_argument("--output", default="data/source_live_smoke_report.json", help="Where to write the report JSON.")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token expected by the loopback feed.")
    return parser.parse_args(argv)


def run_smoke(*, token: str = DEFAULT_TOKEN) -> dict[str, Any]:
    server = _LiveSmokeServer(token=token)
    server.start()
    try:
        unauthorized_statuses = {
            scenario["smoke_group"]: _probe_without_authorization(server.url_for(str(scenario["path"])))
            for scenario in SOURCE_CLASS_SCENARIOS
        }
        started = time.perf_counter()
        source_reports: list[dict[str, Any]] = []
        all_records: list[dict[str, Any]] = []
        for scenario in SOURCE_CLASS_SCENARIOS:
            collector = HTTPFeedCollector(
                HTTPFeedConfig(
                    source_url=server.url_for(str(scenario["path"])),
                    source_name=str(scenario["source_name"]),
                    source_type=str(scenario["source_type"]),
                    platform=str(scenario.get("platform") or ""),
                    legal_basis="INTERNAL_AUTHORIZED_SOURCE",
                    feed_format="json",
                    max_records=10,
                    timeout_seconds=3.0,
                    allowed_domains=("127.0.0.1", "localhost"),
                    headers={"Authorization": f"Bearer {token}"},
                    include_keywords=tuple(scenario["include_keywords"]),
                    text_fields=("content_text", "full_text", "full_article_body", "text", "raw_text"),
                    network_enabled=True,
                )
            )
            records = [model_dump(item) for item in collector.collect()]
            classifications = IntelligencePipeline(load_runtime_llm_value=False).run(
                records,
                context={"quality_profile": "fast", "require_evidence_chain": False},
            ).classified
            source_reports.append(
                _source_report(
                    scenario=scenario,
                    records=records,
                    classifications=classifications,
                    unauthorized_status=unauthorized_statuses.get(str(scenario["smoke_group"])),
                )
            )
            all_records.extend(records)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        pipeline_result = IntelligencePipeline(load_runtime_llm_value=False).run(
            all_records,
            context={"quality_profile": "fast", "require_evidence_chain": False},
        )
        classifications = pipeline_result.classified
        source_evidence_by_group = _source_evidence_by_group(source_reports)
        return {
            "status": "completed",
            "run_type": "live_authorized_loopback_collection_smoke",
            "smoke_scope": "four_required_source_evidence_groups",
            "network_attempted": True,
            "loopback_only": True,
            "authorization_enforced": all(status == 401 for status in unauthorized_statuses.values()),
            "unauthorized_probe_statuses": unauthorized_statuses,
            "authorized_request_headers": ["Authorization"],
            "required_source_classes": sorted({str(item["source_class"]) for item in SOURCE_CLASS_SCENARIOS}),
            "covered_source_classes": sorted({item["source_class"] for item in source_reports if item["collected_count"] > 0}),
            "required_smoke_groups": sorted({str(item["smoke_group"]) for item in SOURCE_CLASS_SCENARIOS}),
            "covered_smoke_groups": sorted({item["smoke_group"] for item in source_reports if item["collected_count"] > 0}),
            "source_evidence_by_group": source_evidence_by_group,
            "per_smoke_group_evidence": _per_smoke_group_evidence(source_reports),
            "sources": source_reports,
            "source": {
                "source_name": "loopback-authorized-feed",
                "source_url": server.base_url,
                "allowed_domains": ["127.0.0.1", "localhost"],
                "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
            },
            "fetched_count": len(all_records),
            "high_risk_candidate_count": sum(
                1
                for item in classifications
                if str(item.get("risk_category") or "").strip() not in {"", "unknown", "正常业务白噪声"}
            ),
            "classification_count": len(classifications),
            "elapsed_ms": elapsed_ms,
            "raw_records": all_records,
            "pipeline_summary": pipeline_result.execution_summary.model_dump(),
            "claim_boundary": (
                "This is a real authorized HTTP collection smoke against local loopback IM/forum/vertical/public-account article feeds. "
                "It demonstrates live fetch, auth enforcement, parsing, filtering, de-dup metrics, and pipeline handoff; "
                "it does not claim external platform access."
            ),
        }
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_smoke(token=args.token)
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


class _LiveSmokeServer:
    def __init__(self, *, token: str) -> None:
        self.token = token
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def url_for(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, name="blackagent-live-smoke", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3.0)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        expected_token = self.token

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
                scenario = next((item for item in SOURCE_CLASS_SCENARIOS if self.path == item["path"]), None)
                if scenario is None:
                    self.send_error(404)
                    return
                if self.headers.get("Authorization") != f"Bearer {expected_token}":
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))
                    return
                body = {"items": scenario["items"]}
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature.
                return

        return Handler


def _probe_without_authorization(url: str) -> int:
    try:
        urllib_request.urlopen(url, timeout=3.0).read()  # noqa: S310 - local loopback smoke only.
    except urllib_error.HTTPError as exc:
        return int(exc.code)
    return 200


def _source_report(
    *,
    scenario: dict[str, Any],
    records: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    unauthorized_status: int | None,
) -> dict[str, Any]:
    normalized_texts = [normalize_text(str(record.get("content_text") or record.get("full_text") or "")) for record in records]
    duplicate_rate = 0.0
    if normalized_texts:
        duplicate_rate = round(1.0 - (len(set(normalized_texts)) / len(normalized_texts)), 4)
    high_risk = sum(
        1
        for item in classifications
        if str(item.get("risk_category") or "").strip() not in {"", "unknown", "正常业务白噪声"}
    )
    configured_count = len(scenario.get("items") or [])
    return {
        "source_class": str(scenario["source_class"]),
        "smoke_group": str(scenario.get("smoke_group") or scenario["source_class"]),
        "source_name": str(scenario["source_name"]),
        "source_type": str(scenario["source_type"]),
        "platform": str(scenario.get("platform") or ""),
        "legal_basis": "INTERNAL_AUTHORIZED_SOURCE",
        "authorization_statement": (
            f"{scenario['source_name']}: local operator-owned loopback feed; bearer token required; "
            "used only for authorized smoke validation."
        ),
        "network_enabled": True,
        "run_type": "live_authorized_loopback_collection_smoke",
        "collected_count": len(records),
        "filtered_count": max(0, configured_count - len(records)),
        "duplicate_rate": duplicate_rate,
        "high_risk_candidate_count": high_risk,
        "failure_reason": None,
        "authorization_enforced": unauthorized_status == 401,
        "unauthorized_probe_status": unauthorized_status,
        "source_evidence": [_source_evidence_from_record(record, scenario=scenario) for record in records],
    }


def _source_evidence_from_record(record: dict[str, Any], *, scenario: dict[str, Any]) -> dict[str, Any]:
    raw_body = str(record.get("content_text") or record.get("raw_text") or record.get("full_text") or "")
    hydrated_body = str(record.get("full_article_body") or record.get("hydrated_body") or "")
    evidence_text = hydrated_body or raw_body
    url = str(record.get("source_url") or scenario.get("source_url") or "")
    if not url:
        url = str(_extract_first_url(raw_body) or record.get("raw_payload_uri") or "")
    fallback_groups = source_quota_groups_for_record(
        {
            "source_class": scenario.get("source_class"),
            "source_type": scenario.get("source_type"),
            "platform": scenario.get("platform"),
            "source_name": scenario.get("source_name"),
            "source_url": url,
        }
    )
    smoke_group = str(scenario.get("smoke_group") or next(iter(fallback_groups), "") or scenario.get("source_class") or "")
    return {
        "source": str(scenario.get("source_name") or record.get("source_name") or "unknown_source"),
        "source_name": str(scenario.get("source_name") or record.get("source_name") or "unknown_source"),
        "source_class": str(scenario.get("source_class") or record.get("source_class") or ""),
        "smoke_group": smoke_group,
        "source_type": str(scenario.get("source_type") or record.get("source_type") or ""),
        "platform": str(scenario.get("platform") or record.get("platform") or ""),
        "url": url,
        "source_url": url,
        "hydrated_body": hydrated_body,
        "raw_body": raw_body,
        "raw_snippet": raw_body[:500] or hydrated_body[:500],
        "crawl_time": str(record.get("crawl_time") or record.get("publish_time") or ""),
        "capture_snapshot_uri": str(record.get("capture_snapshot_uri") or ""),
        "raw_payload_uri": str(record.get("raw_payload_uri") or ""),
        "cleaning_reason": str(record.get("cleaning_reason") or "retained_after_authorized_keyword_filter"),
        "entity_source_snippets": _entity_source_snippets(record, evidence_text),
    }


def _source_evidence_by_group(source_reports: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = sorted({str(report.get("smoke_group") or report.get("source_class") or "") for report in source_reports})
    return {
        group: [
            evidence
            for report in source_reports
            if str(report.get("smoke_group") or report.get("source_class") or "") == group
            for evidence in report.get("source_evidence") or []
        ]
        for group in groups
        if group
    }


def _per_smoke_group_evidence(source_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = _source_evidence_by_group(source_reports)
    rows: list[dict[str, Any]] = []
    for group in sorted(grouped):
        reports = [
            report
            for report in source_reports
            if str(report.get("smoke_group") or report.get("source_class") or "") == group
        ]
        evidence = grouped[group]
        rows.append(
            {
                "smoke_group": group,
                "source_classes": sorted({str(report.get("source_class")) for report in reports if report.get("source_class")}),
                "configured_source_count": len(reports),
                "collected_count": sum(int(report.get("collected_count") or 0) for report in reports),
                "source_evidence_count": len(evidence),
                "hydrated_body_count": sum(1 for item in evidence if item.get("hydrated_body")),
                "raw_body_count": sum(1 for item in evidence if item.get("raw_body")),
                "snapshot_count": sum(1 for item in evidence if item.get("capture_snapshot_uri")),
                "raw_payload_count": sum(1 for item in evidence if item.get("raw_payload_uri")),
            }
        )
    return rows


def _extract_first_url(text: str) -> str:
    for token in text.split():
        if token.startswith(("http://", "https://")):
            return token.rstrip("，。,.")
    return ""


def _entity_source_snippets(record: dict[str, Any], text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    extraction_record = {**record, "content_text": text}
    rows: list[dict[str, Any]] = []
    for entity in AdvancedEntityExtractor().extract(extraction_record)[:5]:
        data = entity.model_dump() if hasattr(entity, "model_dump") else dict(entity)
        start = _safe_int(data.get("start_offset"))
        end = _safe_int(data.get("end_offset"))
        if start is None or end is None or start < 0 or end <= start or start >= len(text):
            snippet = text[:240]
        else:
            snippet = text[max(0, start - 40) : min(len(text), end + 40)]
        rows.append(
            {
                "entity_type": data.get("entity_type"),
                "raw_value": data.get("entity_value"),
                "normalized_value": data.get("normalized_value"),
                "source_snippet": snippet,
            }
        )
    if rows:
        return rows
    return []


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
