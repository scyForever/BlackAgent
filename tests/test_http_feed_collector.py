import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from src.collector import HTTPFeedCollector, HTTPFeedConfig, NetworkCollectionDisabled, SourceAuthorizationError, load_source_catalog
from src.collector.base_collector import model_dump
from src.config_loader import Settings
from src.local_runtime import LocalAgentRuntime
from storage.sql_backend import connect
import src.collector.http_feed_collector as http_feed_collector_module


class FakeHTTPResponse:
    def __init__(self, body, content_type="application/json"):
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": content_type}
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def fake_urlopen(body, content_type="application/json"):
    def _open(request, timeout):
        assert timeout > 0
        assert request.full_url == "https://feed.example/intel.json"
        return FakeHTTPResponse(body, content_type)

    return _open


def test_http_feed_collector_fetches_authorized_json_rows():
    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/intel.json",
            source_name="public-feed",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
        ),
        opener=fake_urlopen(json.dumps({"items": [{"indicator": "risk.example", "threat": "malicious landing domain"}]})),
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert len(rows) == 1
    assert rows[0]["source_name"] == "public-feed"
    assert rows[0]["source_url"] == "https://feed.example/intel.json"
    assert rows[0]["legal_basis"] == "PUBLIC_COMPLIANT_DATA"
    assert "risk.example" in rows[0]["content_text"]
    assert rows[0]["content_hash"]
    assert rows[0]["last_seen_at"]
    assert rows[0]["last_cursor"] == "1"
    assert rows[0]["source_snapshot_id"].startswith("public-feed:")
    assert rows[0]["source_access_type"] == "public_compliant"
    assert rows[0]["source_class"] == "vertical_or_technical"
    assert rows[0]["collection_quality"]["quality_version"] == "collection_quality_v1"


def test_source_metadata_classifies_x_as_social_not_im():
    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://x.example/feed.json",
            source_name="x-feed",
            source_type="X",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("x.example",),
        ),
        opener=lambda request, timeout: FakeHTTPResponse(
            json.dumps({"items": [{"full_text": "X 招募 TG:core01 落地 https://risk.example/path"}]})
        ),
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert rows[0]["source_class"] == "social_or_forum"


def test_http_feed_collector_fetches_authorized_html_snapshot():
    html = """
    <html>
      <head>
        <title>贴吧兼职招募帖</title>
        <meta name="description" content="招募刷单兼职，联系 TG:core01" />
      </head>
      <body>
        <article>
          <p>落地页 https://risk.example/path</p>
          <a href="https://risk.example/path">立即联系</a>
        </article>
      </body>
    </html>
    """

    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://tieba.example/post.html",
            source_name="tieba-feed",
            source_type="Social",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            feed_format="html",
            network_enabled=True,
            allowed_domains=("tieba.example",),
        ),
        opener=lambda request, timeout: FakeHTTPResponse(html, "text/html; charset=utf-8"),
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert len(rows) == 1
    assert rows[0]["source_name"] == "tieba-feed"
    assert "贴吧兼职招募帖" in rows[0]["content_text"]
    assert "TG:core01" in rows[0]["content_text"]
    assert "https://risk.example/path" in rows[0]["content_text"]
    assert rows[0]["feed_row_index"] == 1


def test_http_feed_collector_filters_to_blackgray_rows_and_keeps_keyword_evidence():
    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/intel.json",
            source_name="public-feed",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
            include_keywords=("接码", "刷单"),
            exclude_keywords=("警方通报",),
            text_fields=("full_text",),
        ),
        opener=fake_urlopen(
            json.dumps(
                {
                    "items": [
                        {"full_text": "接码平台继续招人，支持刷单返佣"},
                        {"full_text": "警方通报：某地开展反诈宣传"},
                        {"full_text": "今天普通日常聊天，没有风险词"},
                    ]
                }
            )
        ),
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert len(rows) == 1
    assert rows[0]["content_text"] == "接码平台继续招人，支持刷单返佣"
    assert rows[0]["matched_keywords"] == ["接码", "刷单"]
    assert rows[0]["keyword_hit_count"] == 2
    assert rows[0]["relevance_version"] == "keyword_relevance_v6"


def test_http_feed_collector_matches_theme_synonyms_and_keeps_theme_evidence():
    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/intel.json",
            source_name="public-feed",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
            include_themes=("账号交易", "诈骗引流"),
            text_fields=("full_text",),
        ),
        opener=fake_urlopen(
            json.dumps(
                {
                    "items": [
                        {"full_text": "老号白号号商长期供货，支持私域拉新和高佣导流"},
                        {"full_text": "普通聊天，没有情报"},
                    ]
                }
            )
        ),
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert len(rows) == 1
    assert rows[0]["matched_themes"] == ["账号交易", "诈骗引流"]
    assert "老号" in rows[0]["matched_keywords"]
    assert "白号" in rows[0]["matched_keywords"]
    assert "私域" in rows[0]["matched_keywords"]
    assert "高佣" in rows[0]["matched_keywords"]


def test_http_feed_collector_splits_duckduckgo_search_snapshot_into_result_rows():
    html = """
    <html>
      <body>
        Title: site:tieba.baidu.com/p 接码 at DuckDuckGo
        Markdown Content:
        # site:tieba.baidu.com/p 接码 at DuckDuckGo
        ## [接码平台资源汇总](http://duckduckgo.com/l/?uddg=https%3A%2F%2Ftieba.baidu.com%2Fp%2F9669186844)
        [接码 平台 TG 招募](http://duckduckgo.com/l/?uddg=https%3A%2F%2Ftieba.baidu.com%2Fp%2F9669186844)
        ## [警方通报：反诈宣传](http://duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.example%2F1)
        [警方通报 反诈 提示](http://duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.example%2F1)
      </body>
    </html>
    """

    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/search.html",
            source_name="tieba-feed",
            source_type="Social",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            feed_format="html",
            network_enabled=True,
            allowed_domains=("feed.example",),
            include_keywords=("接码",),
            exclude_keywords=("警方通报",),
            query_term="加薇",
            query_term_stage="variant",
            query_variant_index=5,
        ),
        opener=lambda request, timeout: FakeHTTPResponse(html, "text/html; charset=utf-8"),
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert len(rows) == 1
    assert rows[0]["source_url"] == "https://tieba.baidu.com/p/9669186844"
    assert rows[0]["search_query_url"] == "https://feed.example/search.html"
    assert rows[0]["result_title"] == "接码平台资源汇总"
    assert rows[0]["query_term"] == "加薇"
    assert rows[0]["query_term_stage"] == "variant"
    assert rows[0]["query_variant_index"] == 5
    assert rows[0]["content_text"].startswith("接码平台资源汇总 接码 平台 TG 招募")


def test_http_feed_collector_drops_duckduckgo_block_page_instead_of_persisting_it():
    html = """
    <html>
      <body>
        Title: DuckDuckGo
        Markdown Content:
        Unfortunately, bots use DuckDuckGo too.
        Please complete the following challenge to confirm this search was made by a human.
      </body>
    </html>
    """

    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/search.html",
            source_name="ddg-blocked",
            source_type="Social",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            feed_format="html",
            network_enabled=True,
            allowed_domains=("feed.example",),
            include_keywords=("刷单", "卖号"),
        ),
        opener=lambda request, timeout: FakeHTTPResponse(html, "text/html; charset=utf-8"),
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert rows == []


def test_http_feed_collector_retries_http_429_before_succeeding():
    attempts = {"count": 0}
    sleeps: list[float] = []
    now = {"value": 0.0}

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    def _clock() -> float:
        return now["value"]

    def _open(request, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {"Retry-After": "1"},
                BytesIO(b""),
            )
        return FakeHTTPResponse(json.dumps({"items": [{"indicator": "risk.example", "threat": "telegram automation"}]}))

    collector = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/intel.json",
            source_name="retry-feed",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
            retry_attempts=1,
            retry_backoff_seconds=0.5,
        ),
        opener=_open,
        sleep=_sleep,
        monotonic=_clock,
    )

    rows = [model_dump(item) for item in collector.collect()]

    assert len(rows) == 1
    assert attempts["count"] == 2
    assert sleeps == [1.0]


def test_http_feed_collector_429_backoff_registers_host_delay_for_following_collectors():
    http_feed_collector_module._HOST_NEXT_ALLOWED_AT.clear()
    sleeps: list[float] = []
    now = {"value": 0.0}

    def _clock() -> float:
        return now["value"]

    collector_a = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/a.json",
            source_name="retry-a",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
            retry_attempts=2,
            retry_backoff_seconds=0.5,
        ),
        monotonic=_clock,
    )
    collector_b = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/b.json",
            source_name="retry-b",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
        ),
        sleep=lambda seconds: sleeps.append(seconds),
        monotonic=_clock,
    )

    delay = collector_a._retry_delay_seconds(  # noqa: SLF001 - unit-test host backoff registration
        HTTPError(
            collector_a.config.source_url,
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            BytesIO(b""),
        ),
        0,
    )
    collector_b._throttle_request_host()  # noqa: SLF001 - unit-test shared host backoff behavior

    assert delay == 2.0
    assert sleeps == [2.0]


def test_http_feed_collector_rate_limits_same_host_across_collectors():
    http_feed_collector_module._HOST_NEXT_ALLOWED_AT.clear()
    sleeps: list[float] = []
    now = {"value": 0.0}

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    def _clock() -> float:
        return now["value"]

    collector_a = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/a.json",
            source_name="feed-a",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
            rate_limit_per_minute=60,
        ),
        opener=lambda request, timeout: FakeHTTPResponse(json.dumps({"items": [{"indicator": "a"}]})),
        sleep=_sleep,
        monotonic=_clock,
    )
    collector_b = HTTPFeedCollector(
        HTTPFeedConfig(
            source_url="https://feed.example/b.json",
            source_name="feed-b",
            legal_basis="PUBLIC_COMPLIANT_DATA",
            network_enabled=True,
            allowed_domains=("feed.example",),
            rate_limit_per_minute=60,
        ),
        opener=lambda request, timeout: FakeHTTPResponse(json.dumps({"items": [{"indicator": "b"}]})),
        sleep=_sleep,
        monotonic=_clock,
    )

    assert len([model_dump(item) for item in collector_a.collect()]) == 1
    assert len([model_dump(item) for item in collector_b.collect()]) == 1
    assert sleeps == [1.0]


def test_http_feed_collector_requires_network_and_domain_authorization():
    config = HTTPFeedConfig(
        source_url="https://feed.example/intel.json",
        source_name="public-feed",
        legal_basis="PUBLIC_COMPLIANT_DATA",
        network_enabled=False,
    )
    with pytest.raises(NetworkCollectionDisabled):
        HTTPFeedCollector(config, opener=fake_urlopen("[]")).collect()

    blocked = HTTPFeedConfig(
        source_url="https://evil.example/intel.json",
        source_name="public-feed",
        legal_basis="PUBLIC_COMPLIANT_DATA",
        network_enabled=True,
        allowed_domains=("feed.example",),
    )
    with pytest.raises(SourceAuthorizationError):
        HTTPFeedCollector(blocked, opener=fake_urlopen("[]")).collect()


def test_source_collect_runtime_fetches_and_persists_real_feed_shape(monkeypatch, tmp_path):
    body = json.dumps({"items": [{"indicator": "risk.example", "threat": "landing domain"}]})

    def _open(request, timeout):
        assert request.full_url == "https://feed.example/intel.json"
        return FakeHTTPResponse(body)

    monkeypatch.setattr("src.collector.http_feed_collector.urllib_request.urlopen", _open)
    settings = Settings(
        network={"enabled": True, "allowed_domains": ["feed.example"], "max_records_per_fetch": 5},
        storage={"backend": "sql", "dsn": f"sqlite:///{(tmp_path / 'source.db').as_posix()}", "auto_create_schema": True},
    )
    runtime = LocalAgentRuntime(settings)
    try:
        payload = runtime.collect_source(
            {
            "source_url": "https://feed.example/intel.json",
            "source_name": "public-feed",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            },
            persist_raw=True,
        )
    finally:
        runtime.close()

    assert payload["fetched_count"] == 1
    assert payload["persisted_count"] == 1
    assert payload["network_attempted"] is True
    assert payload["raw_records"][0]["source_name"] == "public-feed"

    backend = connect(settings.storage.dsn)
    backend.create_schema()
    assert backend.list_raw()[0]["source_name"] == "public-feed"
    assert backend.list_audit(event_type="source_collect_completed")
    backend.close()


def test_load_source_catalog_normalizes_allowed_domains_and_text_fields(tmp_path):
    catalog_path = tmp_path / "source_catalog.yaml"
    catalog_path.write_text(
        """
sources:
  - source_name: tieba-feed
    source_url: https://tieba.example/post.html
    source_type: Social
    feed_format: html
    allowed_domain: tieba.example
    text_fields: title
    query_global_terms: [加v, 拉群]
    include_keywords: 接码
    include_themes: 账号交易
    exclude_keywords:
      - 警方通报
    exclude_themes:
      - 诈骗引流
    min_keyword_hits: 2
        """.strip(),
        encoding="utf-8",
    )

    sources = load_source_catalog(catalog_path)

    assert sources[0]["allowed_domains"] == ["tieba.example"]
    assert sources[0]["text_fields"] == ["title"]
    assert sources[0]["query_global_terms"] == ["加v", "拉群"]
    assert sources[0]["include_keywords"] == ["接码"]
    assert sources[0]["include_themes"] == ["账号交易"]
    assert sources[0]["exclude_keywords"] == ["警方通报"]
    assert sources[0]["exclude_themes"] == ["诈骗引流"]
    assert sources[0]["min_keyword_hits"] == 2
    assert sources[0]["feed_format"] == "html"


def test_load_source_catalog_expands_query_themes_into_multiple_search_sources(tmp_path):
    catalog_path = tmp_path / "source_catalog_expand.yaml"
    catalog_path.write_text(
        """
sources:
  - source_name: x-feed
    query_url_template: https://search.example/?q={query}
    query_seed_terms: [site:x.com]
    query_themes: [账号交易]
    query_term_limit: 2
    legal_basis: PUBLIC_COMPLIANT_DATA
    allowed_domain: search.example
    include_themes: [账号交易]
        """.strip(),
        encoding="utf-8",
    )

    sources = load_source_catalog(catalog_path)

    assert len(sources) == 2
    assert sources[0]["source_name"] == "x-feed"
    assert sources[0]["query_theme"] == "账号交易"
    assert sources[0]["query_term"] == "收号"
    assert sources[0]["query_term_stage"] == "core"
    assert sources[0]["search_query"] == "site:x.com 收号"
    assert sources[0]["source_url"] == "https://search.example/?q=site%3Ax.com%20%E6%94%B6%E5%8F%B7"
    assert sources[1]["query_term"] == "实名号"


def test_load_source_catalog_expands_global_terms_before_theme_callbacks_and_dedupes(tmp_path):
    catalog_path = tmp_path / "source_catalog_global_then_theme.yaml"
    catalog_path.write_text(
        """
sources:
  - source_name: tg-feed
    query_url_template: https://search.example/?q={query}
    query_seed_terms: [site:t.me/s]
    query_global_terms: [加v, 拉群]
    query_themes: [诈骗引流]
    query_term_limit: 2
    legal_basis: PUBLIC_COMPLIANT_DATA
    allowed_domain: search.example
    include_themes: [诈骗引流]
        """.strip(),
        encoding="utf-8",
    )

    sources = load_source_catalog(catalog_path)

    assert len(sources) == 3
    assert sources[0]["search_query"] == "site:t.me/s 加v"
    assert sources[0]["query_theme"] is None
    assert sources[0]["query_term"] == "加v"
    assert sources[0]["query_term_stage"] == "core"
    assert sources[1]["search_query"] == "site:t.me/s 拉群"
    assert sources[1]["query_theme"] is None
    assert sources[1]["query_term"] == "拉群"
    assert sources[2]["search_query"] == "site:t.me/s 私域导流"
    assert sources[2]["query_theme"] == "诈骗引流"
    assert sources[2]["query_term_stage"] == "core"


def test_load_source_catalog_can_append_second_wave_variant_queries(tmp_path):
    catalog_path = tmp_path / "source_catalog_variant_wave.yaml"
    catalog_path.write_text(
        """
sources:
  - source_name: tieba-feed
    query_url_template: https://search.example/?q={query}
    query_seed_terms: [site:tieba.baidu.com/p]
    query_themes: [诈骗引流]
    query_term_limit: 10
    legal_basis: PUBLIC_COMPLIANT_DATA
    allowed_domain: search.example
    include_themes: [诈骗引流]
        """.strip(),
        encoding="utf-8",
    )

    sources = load_source_catalog(catalog_path)

    assert len(sources) == 10
    assert sources[0]["query_term"] == "私域导流"
    assert sources[0]["query_term_stage"] == "core"
    assert sources[4]["query_term"] == "加薇"
    assert sources[4]["query_term_stage"] == "variant"
    assert sources[5]["query_term"] == "➕v"
    assert sources[5]["query_term_stage"] == "variant"
    assert sources[6]["query_term"] == "拉裙"
    assert sources[7]["query_term"] == "进裙"
    assert sources[8]["query_term"] == "加微"
    assert sources[9]["query_term"] == "加威"


def test_batch_source_collect_runtime_aggregates_multi_platform_sources(monkeypatch, tmp_path):
    bodies = {
        "https://tieba.example/post.html": (
            """
            <html><head><title>贴吧招募帖</title></head>
            <body><p>刷单兼职 TG:core01 落地 https://risk.example/path</p></body></html>
            """,
            "text/html; charset=utf-8",
        ),
        "https://telegram.example/feed.jsonl": (
            '{"message":"Telegram 群控脚本 TG:core01 落地 https://risk.example/path"}\n',
            "application/x-ndjson",
        ),
        "https://x.example/feed.json": (
            json.dumps({"items": [{"full_text": "X 频道继续招募 TG:core01 落地 https://risk.example/path"}]}),
            "application/json",
        ),
    }

    def _open(request, timeout):
        body, content_type = bodies[request.full_url]
        return FakeHTTPResponse(body, content_type)

    monkeypatch.setattr("src.collector.http_feed_collector.urllib_request.urlopen", _open)
    settings = Settings(
        network={"enabled": True, "max_records_per_fetch": 5},
        storage={"backend": "sql", "dsn": f"sqlite:///{(tmp_path / 'batch.db').as_posix()}", "auto_create_schema": True},
    )
    runtime = LocalAgentRuntime(settings)
    try:
        payload = runtime.collect_sources_batch(
            persist_raw=True,
            run_pipeline=True,
            sources=[
                {
                    "source_name": "tieba-feed",
                    "source_type": "Social",
                    "source_url": "https://tieba.example/post.html",
                    "feed_format": "html",
                    "legal_basis": "PUBLIC_COMPLIANT_DATA",
                    "allowed_domains": ["tieba.example"],
                },
                {
                    "source_name": "telegram-feed",
                    "source_type": "IM",
                    "source_url": "https://telegram.example/feed.jsonl",
                    "feed_format": "jsonl",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "allowed_domains": ["telegram.example"],
                    "text_fields": ["message"],
                },
                {
                    "source_name": "x-feed",
                    "source_type": "IM",
                    "source_url": "https://x.example/feed.json",
                    "feed_format": "json",
                    "legal_basis": "AUTHORIZED_PARTNER",
                    "allowed_domains": ["x.example"],
                    "text_fields": ["full_text"],
                },
            ],
        )
    finally:
        runtime.close()

    assert payload["status"] == "completed"
    assert payload["source_count"] == 3
    assert payload["succeeded_count"] == 3
    assert payload["failed_count"] == 0
    assert payload["fetched_count"] == 3
    assert payload["persisted_count"] == 3
    assert payload["pipeline_result"]["risk_clue_count"] >= 2

    backend = connect(settings.storage.dsn)
    backend.create_schema()
    assert len(backend.list_raw()) == 3
    assert backend.list_audit(event_type="batch_source_collect_completed")
    backend.close()
