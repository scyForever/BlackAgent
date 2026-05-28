import json
from datetime import datetime, timedelta, timezone

from scripts.run_agent_cli import discover_source_config_path, load_local_corpus_records, parse_args
from src.enhancement.source_intake import MultimodalTextExtractor


def test_local_corpus_auto_seed_filters_today_fraud_records_and_normalizes_clean_text(tmp_path):
    now = datetime.now(timezone.utc)
    corpus = tmp_path / "corpus.jsonl"
    records = [
        {
            "clean_id": "recent-fraud",
            "clean_text": "私域导流拉群线索，落地 risk.example，联系 TG:test",
            "created_at": (now - timedelta(hours=1)).isoformat(),
            "risk_categories": ["诈骗引流"],
            "matched_themes": ["诈骗引流"],
            "risk_score": 0.9,
            "quality_score": 0.8,
        },
        {
            "clean_id": "old-fraud",
            "clean_text": "昨天以前的诈骗引流线索",
            "created_at": (now - timedelta(hours=30)).isoformat(),
            "risk_categories": ["诈骗引流"],
        },
        {
            "clean_id": "recent-other",
            "clean_text": "普通账号交易记录",
            "created_at": (now - timedelta(hours=1)).isoformat(),
            "risk_categories": ["账号交易"],
        },
    ]
    corpus.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8")

    loaded, context = load_local_corpus_records(
        "取一下当天诈骗引流相关的线索信息",
        corpus_path=corpus,
        limit=10,
    )

    assert context["mode"] == "local_corpus_auto_seed"
    assert context["matched_count"] == 1
    assert context["loaded_count"] == 1
    assert context["time_range_hours"] == 24
    assert loaded[0]["trace_id"] == "recent-fraud"
    assert loaded[0]["content_text"].startswith("私域导流拉群线索")
    assert loaded[0]["publish_time"] == records[0]["created_at"]
    assert loaded[0]["legal_basis"] == "PUBLIC_COMPLIANT_DATA"


def test_multimodal_extractor_accepts_cleaning_phase_clean_text():
    materialized = MultimodalTextExtractor().materialize(
        {
            "trace_id": "clean-1",
            "clean_text": "诈骗引流清洗语料，包含拉群和落地页。",
        }
    )

    assert "诈骗引流清洗语料" in materialized["content_text"]
    assert "clean_text" in materialized["multimodal_text_sources"]


def test_source_config_auto_discovery_prefers_blackgray_catalog(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "intel_sources.public.yaml").write_text(
        """
sources:
  - source_name: public
    source_url: https://example.com/public
""".strip(),
        encoding="utf-8",
    )
    (config_dir / "intel_sources.blackgray.yaml").write_text(
        """
sources:
  - source_name: blackgray
    query_url_template: https://example.com/search?q={query}
""".strip(),
        encoding="utf-8",
    )

    source_config_path, context = discover_source_config_path(config_dir=config_dir)

    assert source_config_path.endswith("intel_sources.blackgray.yaml")
    assert context["source_config_auto_discovered"] is True
    assert context["source_config_path"].endswith("intel_sources.blackgray.yaml")


def test_cli_max_sources_defaults_to_all_sources_semantics():
    args = parse_args(["--query", "取一下当天诈骗引流相关的线索信息"])

    assert args.max_sources is None
